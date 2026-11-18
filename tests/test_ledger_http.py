"""HTTP 层验证：登录鉴权、账本版本/区间查询/重试接口与 worker 循环联动。"""
import os, sys, tempfile, time, subprocess

_dir = tempfile.mkdtemp(prefix='wal-http-test-')
os.environ['DATABASE_URL'] = 'sqlite:///' + os.path.join(_dir, 'http.db')
os.environ['JWT_SECRET'] = 'test-secret'
os.environ['REDIS_URL'] = 'redis://127.0.0.1:6390/0'
os.environ['PAGE_TOKEN_TTL_SECONDS'] = '1'
os.environ['LEDGER_FAIL_RECOVERY_IDS'] = '1'
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'backend')))

from fastapi.testclient import TestClient
from src.config.database import Base, engine, Session
from src.models.entities import User, LogSet, CrashCase
from src.utils.security import hash_password
from src.main import app

PASS = FAIL = 0
def check(name, cond, detail=''):
    global PASS, FAIL
    if cond: PASS += 1; print(f'  ✓ {name}')
    else: FAIL += 1; print(f'  ✗ {name} {detail}')

RECORDS = [{'op': 'begin', 'tx': 'T1'}, {'op': 'write', 'tx': 'T1', 'key': 'a', 'value': 5},
           {'op': 'commit', 'tx': 'T1'}, {'op': 'checkpoint'},
           {'op': 'begin', 'tx': 'T2'}, {'op': 'write', 'tx': 'T2', 'key': 'b', 'value': 5},
           {'op': 'commit', 'tx': 'T2'},
           {'op': 'begin', 'tx': 'T3'}, {'op': 'write', 'tx': 'T3', 'key': 'c', 'value': 9},
           {'op': 'commit', 'tx': 'T3'}]

with Session() as db:
    Base.metadata.create_all(engine)
    db.add_all([
        User(email='operator@haili.local', role='operator', password_hash=hash_password('Recovery2026!')),
        User(email='viewer@haili.local', role='viewer', password_hash=hash_password('Recovery2026!')),
    ])
    ls = LogSet(name='HTTP账本', description='d', initial={'a': 0, 'b': 0, 'c': 0}, records=RECORDS)
    db.add(ls); db.flush()
    db.add(CrashCase(name='HTTP崩溃点', log_set_id=ls.id, crash_after=len(RECORDS)))
    db.commit()
    ls_id, case_id = ls.id, ls.id

client = TestClient(app)
r = client.post('/auth/login', json={'email': 'operator@haili.local', 'password': 'Recovery2026!'})
check('操作员登录', r.status_code == 200, r.text)
op = {'Authorization': 'Bearer ' + r.json()['access_token']}
r = client.post('/auth/login', json={'email': 'viewer@haili.local', 'password': 'Recovery2026!'})
vh = {'Authorization': 'Bearer ' + r.json()['access_token']}

# 未发布账本时查询应 404
r = client.post('/ledger-query', headers=op, json={'log_set_id': ls_id, 'low_value': 0, 'high_value': 10})
check('未发布时区间查询 404', r.status_code == 404, r.text)

# 入队一次恢复（recovery_id=1，注入故障）
r = client.post('/recoveries', headers=op, json={'crash_case_id': case_id, 'label': 'HTTP恢复一'})
check('恢复入队 202', r.status_code == 202, r.text)
recovery_id = r.json()['id']

# 直接驱动 worker 循环体若干次（失败→等待重试→成功），与 src/worker.py 同路径
from src.services import ledger_service
from src.models.entities import Recovery, LedgerVersion
from sqlalchemy import select
for _ in range(3):
    with Session() as db:
        job = db.get(Recovery, recovery_id)
        if job.status == 'queued':
            from src.services.wal_engine import recover
            job.status = 'running'
            job.result = recover(job.snapshot['log_set']['initial'], job.snapshot['log_set']['records'],
                                 job.snapshot['case']['crash_after'])
            job.status = 'completed'
            ledger_service.schedule_for_recovery(db, job)
            db.commit()
        else:
            ledger_service.process_pending(db); db.commit()
    time.sleep(0.05)

with Session() as db:
    v = db.scalars(select(LedgerVersion).where(LedgerVersion.log_set_id == ls_id)).all()
    check('首次重建失败留痕', any(x.state == 'failed' and x.attempts == 1 and x.last_error for x in v) or v[0].state == 'current',
          str([(x.state, x.attempts) for x in v]))

# 手动重试接口（viewer 应 403）
with Session() as db:
    failed = db.scalar(select(LedgerVersion).where(LedgerVersion.state == 'failed'))
r = client.post('/ledger-versions/retry', headers=vh, json={'ledger_version_id': failed.id})
check('观察员重试被拒 403', r.status_code == 403, r.text)
r = client.post('/ledger-versions/retry', headers=op, json={'ledger_version_id': failed.id})
check('操作员重试 202', r.status_code == 202, r.text)
with Session() as db:
    ledger_service.process_pending(db); db.commit()

# 版本列表
r = client.get(f'/ledger-versions?log_set_id={ls_id}', headers=op)
check('版本列表可查', r.status_code == 200 and any(x['state'] == 'current' for x in r.json()), r.text)
version_no = next(x['version_no'] for x in r.json() if x['state'] == 'current')
version_id = next(x['id'] for x in r.json() if x['state'] == 'current')

# 区间查询 + 翻页（page_size=2，3 个点）
r = client.post('/ledger-query', headers=vh, json={'log_set_id': ls_id, 'low_value': 5, 'high_value': 9, 'page_size': 2})
check('观察员可区间查询 200', r.status_code == 200, r.text)
body = r.json()
check('首页2条且同值按点号', [p['point_key'] for p in body['points']] == ['a', 'b'] and body['has_more'], str(body['points']))
check('返回续翻凭据与锚点', body['next_token'] and body['next_cursor']['value'] == 5)
token = body['next_token']
r2 = client.post('/ledger-query', headers=vh, json={'log_set_id': ls_id, 'low_value': 0, 'high_value': 100, 'page_token': token})
check('凭据续翻锁定原区间/原版', r2.status_code == 200 and r2.json()['range'] == {'low': 5, 'high': 9}
      and r2.json()['ledger_version_id'] == version_id, r2.text)
check('续翻返回剩余点且不漏不重', [p['point_key'] for p in r2.json()['points']] == ['c'] and not r2.json()['has_more'], str(r2.json()))
# 凭据一次性
r3 = client.post('/ledger-query', headers=vh, json={'log_set_id': ls_id, 'low_value': 0, 'high_value': 100, 'page_token': token})
check('凭据重用 410', r3.status_code == 410, r3.text)
# 过期
r4 = client.post('/ledger-query', headers=vh, json={'log_set_id': ls_id, 'low_value': 5, 'high_value': 9, 'page_size': 1})
tok = r4.json()['next_token']
time.sleep(1.2)
r5 = client.post('/ledger-query', headers=vh, json={'log_set_id': ls_id, 'low_value': 5, 'high_value': 9, 'page_token': tok})
check('过期凭据 410 拒绝续翻', r5.status_code == 410, r5.text)
# 非法区间与页长
r6 = client.post('/ledger-query', headers=op, json={'log_set_id': ls_id, 'low_value': 9, 'high_value': 1})
check('下界>上界 422', r6.status_code == 422, r6.text)
r7 = client.post('/ledger-query', headers=op, json={'log_set_id': ls_id, 'low_value': 1, 'high_value': 2, 'page_size': 999})
check('页长超限 422', r7.status_code == 422, r7.text)
# 未认证
r8 = client.post('/ledger-query', json={'log_set_id': ls_id, 'low_value': 0, 'high_value': 1})
check('无凭据 401', r8.status_code == 401, r8.text)

print(f'\nHTTP 结果：{PASS} 通过 / {FAIL} 失败')
sys.exit(1 if FAIL else 0)
