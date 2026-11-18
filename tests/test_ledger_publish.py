"""账本发布与区间翻页的端到端验证（SQLite 本地库，不依赖 Docker/Redis）。

三次恢复验证：
  1. 同值跨页：多个采样点同值，小页长键集翻页不漏点、不重读、同值按点号排序
  2. 重建失败：注入故障使首次重建失败，旧版继续可查；重试后一次性切换且不重复发布
  3. 等候期间的新旧查询：旧版仍有有效凭据时第三版等候；凭据过期后槽位空出再发布
运行：.venv/bin/python -I tests/test_ledger_publish.py
"""
import os, sys, tempfile, datetime

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'backend')))
_db_dir = tempfile.mkdtemp(prefix='wal-ledger-test-')
os.environ['DATABASE_URL'] = 'sqlite:///' + os.path.join(_db_dir, 'test.db')
os.environ['JWT_SECRET'] = 'test-secret'
os.environ['REDIS_URL'] = 'redis://127.0.0.1:6390/0'  # 不可达，触发进程内降级锁
os.environ['PAGE_TOKEN_TTL_SECONDS'] = '2'
os.environ['LEDGER_FAIL_RECOVERY_IDS'] = '2'          # 第2次恢复首次重建强制失败

from sqlalchemy import select
from src.config.database import Base, engine, Session
from src.models.entities import User, LogSet, CrashCase, Recovery, LedgerVersion, LedgerIndexPoint, LedgerPageToken
from src.services.wal_engine import recover
from src.services import ledger_service
from src.utils.security import hash_password
from src.models.entities import now

PASS, FAIL = 0, 0
def check(name, cond, detail=''):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f'  ✓ {name}')
    else:
        FAIL += 1
        print(f'  ✗ {name} {detail}')

# 同值键：k01..k06 恢复后均为 50；另加两个异值键，验证值序+点号序
INITIAL = {f'k{i:02d}': 0 for i in range(1, 9)}
RECORDS = []
for i in range(1, 7):  # k01..k06 -> 50（同值，点号顺序需稳定）
    tx = f'T{i}'
    RECORDS += [{'op': 'begin', 'tx': tx},
                {'op': 'write', 'tx': tx, 'key': f'k{i:02d}', 'value': 50},
                {'op': 'commit', 'tx': tx}]
RECORDS += [{'op': 'checkpoint'},
            {'op': 'begin', 'tx': 'T7'}, {'op': 'write', 'tx': 'T7', 'key': 'k07', 'value': 90}, {'op': 'commit', 'tx': 'T7'},
            {'op': 'begin', 'tx': 'T8'}, {'op': 'write', 'tx': 'T8', 'key': 'k08', 'value': 10}, {'op': 'commit', 'tx': 'T8'}]

def make_recovery(db, log_set, crash_after, label):
    case = CrashCase(log_set_id=log_set.id, name=f'case-{label}', crash_after=crash_after)
    db.add(case); db.flush()
    job = Recovery(crash_case_id=case.id, label=label, status='queued',
                   snapshot={'log_set': {'id': log_set.id, 'name': log_set.name,
                                         'initial': log_set.initial, 'records': log_set.records},
                             'case': {'id': case.id, 'crash_after': crash_after}})
    db.add(job); db.flush()
    job.result = recover(log_set.initial, log_set.records, crash_after)
    job.status = 'completed'
    ledger_service.schedule_for_recovery(db, job)
    db.commit()
    return job

def versions_of(db, log_set_id):
    return db.scalars(select(LedgerVersion).where(LedgerVersion.log_set_id == log_set_id)
                      .order_by(LedgerVersion.version_no)).all()

def main():
    Base.metadata.create_all(engine)
    with Session() as db:
        db.add(User(email='operator@haili.local', role='operator', password_hash=hash_password('x')))
        log_set = LogSet(name='演练账本-同值翻页', description='6个同值点+2个异值点',
                         initial=INITIAL, records=RECORDS)
        db.add(log_set); db.commit()

        print('\n== 场景1：第一次恢复 → 同值跨页，不漏点不重读，同值按点号排序 ==')
        r1 = make_recovery(db, log_set, len(RECORDS), '恢复一')
        summary = ledger_service.process_pending(db); db.commit()
        vs = versions_of(db, log_set.id)
        check('首版直接发布为 current', len(vs) == 1 and vs[0].state == 'current', str([(v.version_no, v.state) for v in vs]))
        v1 = vs[0]
        pts = db.scalars(select(LedgerIndexPoint).where(LedgerIndexPoint.ledger_version_id == v1.id)
                         .order_by(LedgerIndexPoint.value, LedgerIndexPoint.point_no)).all()
        check('持久索引含全部8个采样点', len(pts) == 8)
        same_value = [p for p in pts if p.value == 50]
        check('同值点按点号升序', [p.point_no for p in same_value] == sorted(p.point_no for p in same_value)
              and [p.point_key for p in same_value] == [f'k{i:02d}' for i in range(1, 7)])
        check('整体按(值,点号)有序', [(p.value, p.point_no) for p in pts] == sorted((p.value, p.point_no) for p in pts))

        # 小页长 page_size=3 翻遍 [10,90]，验证键集分页完整性
        seen, token, pages = [], None, 0
        while True:
            resp = ledger_service.query_range(db, log_set.id, 10, 90, 3, page_token=token) if token \
                else ledger_service.query_range(db, log_set.id, 10, 90, 3)
            seen += [(p['value'], p['point_no']) for p in resp['points']]
            pages += 1
            token = resp['next_token']
            if not token:
                break
        expected = sorted((p.value, p.point_no) for p in pts)
        check('跨页拼回与持久索引完全一致（不漏点）', seen == expected, f'{seen} != {expected}')
        check('无重复点（不重读）', len(seen) == len(set(seen)))
        check('同值跨页发生（页数>=3）', pages >= 3, f'pages={pages}')

        # 凭据一次性：重复使用同一凭据必须拒绝
        db.rollback()
        reused = ledger_service.query_range(db, log_set.id, 10, 90, 3)
        t1 = reused['next_token']
        ledger_service.query_range(db, log_set.id, 10, 90, 3, page_token=t1)
        db.commit()
        try:
            ledger_service.query_range(db, log_set.id, 10, 90, 3, page_token=t1)
            check('重复使用凭据被拒绝', False)
        except Exception as e:
            check('重复使用凭据被拒绝', getattr(e, 'status_code', None) == 410, str(e))
        db.rollback()

        # 区间过滤：只查值=50
        resp50 = ledger_service.query_range(db, log_set.id, 50, 50, 20)
        check('区间只返回值50的6个同值点', len(resp50['points']) == 6 and all(p['value'] == 50 for p in resp50['points']))
        check('末页不再发凭据', resp50['next_token'] is None and resp50['has_more'] is False)

        print('\n== 场景2：第二次恢复重建失败 → 旧版保留可查，重试不重复发布 ==')
        r2 = make_recovery(db, log_set, len(RECORDS), '恢复二')  # recovery_id=2 注入故障
        s2 = ledger_service.process_pending(db); db.commit()
        vs = versions_of(db, log_set.id)
        v2 = next(v for v in vs if v.version_no == 2)
        check('故障注入使第二版 failed', 2 in s2['failed'] and v2.state == 'failed', v2.last_error or '')
        check('失败保留旧版 current 可查', any(v.state == 'current' and v.version_no == 1 for v in vs))
        check('失败版本不产生索引点', db.scalar(select(LedgerIndexPoint.id).where(LedgerIndexPoint.ledger_version_id == v2.id).limit(1)) is None)
        resp_old = ledger_service.query_range(db, log_set.id, 10, 90, 20)
        check('失败期间查询仍走第一版', resp_old['ledger_version_id'] == v1.id and len(resp_old['points']) == 8)

        # 重试不重复发布：同一 recovery_id 只有一条版本记录
        ledger_service.retry_failed(db, v2.id); db.commit()
        ledger_service.process_pending(db); db.commit()
        vs = versions_of(db, log_set.id)
        check('重试后仍只有两个版本（无重复发布）', [v.version_no for v in vs] == [1, 2])
        v2 = next(v for v in vs if v.version_no == 2)
        check('重试成功后第二版 current、第一版 previous（一次性切换）',
              v2.state == 'current' and next(v for v in vs if v.version_no == 1).state == 'previous')
        check('第二版索引点数正确', db.scalar(select(LedgerIndexPoint.id).where(LedgerIndexPoint.ledger_version_id == v2.id).limit(1)) is not None)
        check('旧版仍保留可查（一份旧版）',
              len([v for v in vs if v.state == 'previous']) == 1)

        print('\n== 场景3：第三版等候 → 凭据有效期内新旧可查，过期后空出槽位发布 ==')
        # 在旧版（v1=previous）上先发一个有效翻页凭据，占住旧版槽位
        held = ledger_service.query_range(db, log_set.id, 50, 50, 2, ledger_version_id=v1.id)
        held_token = held['next_token']
        check('旧版上成功发出有效凭据', held_token is not None and held['version_state'] == 'previous')

        r3 = make_recovery(db, log_set, len(RECORDS), '恢复三')
        s3 = ledger_service.process_pending(db); db.commit()
        vs = versions_of(db, log_set.id)
        v3 = next(v for v in vs if v.version_no == 3)
        check('旧版凭据有效时第三版保持 pending 等候', v3.state == 'pending' and 3 in s3['waiting'])
        check('等候期间当前版仍为第二版', next(v for v in vs if v.state == 'current').version_no == 2)
        check('等候期间旧版凭据仍查第一版（原版不漏不重）',
              ledger_service.query_range(db, log_set.id, 50, 50, 20, page_token=held_token)['ledger_version_id'] == v1.id)
        db.commit()
        # 新版未发布，默认查询仍走第二版
        check('等候期间默认查询走当前第二版',
              ledger_service.query_range(db, log_set.id, 10, 90, 20)['version_no'] == 2)

        # 等待凭据过期（TTL=2s）
        print('  … 等待翻页凭据过期')
        import time; time.sleep(2.2)
        try:
            ledger_service.query_range(db, log_set.id, 10, 90, 3, page_token=held_token)
            check('过期凭据拒绝续翻', False)
        except Exception as e:
            check('过期凭据拒绝续翻（410）', getattr(e, 'status_code', None) == 410, str(e))
        db.rollback()

        s4 = ledger_service.process_pending(db); db.commit()
        vs = versions_of(db, log_set.id)
        states = {v.version_no: v.state for v in vs}
        check('槽位空出后第三版发布为 current', 3 in s4['published'] and states.get(3) == 'current', str(states))
        check('第一版已下架，只剩第二版 previous + 第三版 current（至多一份旧版）',
              states == {2: 'previous', 3: 'current'}, str(states))
        check('下架版本的索引与凭据已清理',
              db.scalar(select(LedgerIndexPoint.id).where(LedgerIndexPoint.ledger_version_id == v1.id).limit(1)) is None
              and db.scalar(select(LedgerPageToken.id).where(LedgerPageToken.ledger_version_id == v1.id).limit(1)) is None)
        resp_new = ledger_service.query_range(db, log_set.id, 10, 90, 20)
        check('新查询默认走第三版且数据完整', resp_new['version_no'] == 3 and len(resp_new['points']) == 8)

        print(f'\n结果：{PASS} 通过 / {FAIL} 失败')
        return 1 if FAIL else 0

if __name__ == '__main__':
    sys.exit(main())
