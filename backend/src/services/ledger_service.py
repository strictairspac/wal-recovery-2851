"""随恢复发布的采样账本：持久索引、一次性切换、版本槽位与键集翻页凭据。

同一日志集的账本版本生命周期：
  pending   恢复完成后随恢复生成；等待重建或等待旧版槽位空出
  failed    重建失败：旧版（若有）保持可查，重试不重复发布（recovery_id 唯一）
  current   当前发布版；新账本与新索引在同一事务内一次性切换
  previous  旧版，至多保留 LEDGER_MAX_OLD_VERSIONS 份，凭据全部过期后槽位空出
"""
import secrets
from datetime import timedelta
from fastapi import HTTPException
from sqlalchemy import select, delete, and_, or_
from src.models.entities import Recovery, LedgerVersion, LedgerIndexPoint, LedgerPageToken
from src.models.entities import now
from src.config.settings import PAGE_TOKEN_TTL_SECONDS, LEDGER_MAX_OLD_VERSIONS, LEDGER_FAIL_RECOVERY_IDS

LEDGER_STATES = ('pending', 'failed', 'current', 'previous')


class LedgerBuildError(RuntimeError):
    """账本重建失败：触发回滚，保留旧版继续查询。"""


def schedule_for_recovery(db, recovery):
    """恢复成功后登记一个待发布账本版本；同一恢复重试不重复发布。"""
    existing = db.scalar(select(LedgerVersion).where(LedgerVersion.recovery_id == recovery.id))
    if existing is not None:
        return existing
    log_set_id = (recovery.snapshot or {}).get('log_set', {}).get('id')
    last = db.scalar(select(LedgerVersion).where(LedgerVersion.log_set_id == log_set_id)
                     .order_by(LedgerVersion.version_no.desc()))
    version_no = (last.version_no + 1) if last else 1
    obj = LedgerVersion(log_set_id=log_set_id, recovery_id=recovery.id, version_no=version_no, state='pending')
    db.add(obj)
    db.flush()
    return obj


def _active_versions(db, log_set_id):
    rows = db.scalars(select(LedgerVersion)
                      .where(LedgerVersion.log_set_id == log_set_id,
                             LedgerVersion.state.in_(('current', 'previous')))
                      .order_by(LedgerVersion.version_no.desc())).all()
    current = next((v for v in rows if v.state == 'current'), None)
    previous = [v for v in rows if v.state == 'previous']
    return current, previous


def _has_live_tokens(db, version_id, at):
    return db.scalar(select(LedgerPageToken.id).where(
        LedgerPageToken.ledger_version_id == version_id,
        LedgerPageToken.expires_at > at).limit(1)) is not None


def _build_index(db, version, recovery):
    """从恢复结果重建账本并生成持久索引；与版本切换处于同一事务，失败整体回滚。"""
    result = recovery.result or {}
    recovered = result.get('recovered')
    if not isinstance(recovered, dict) or not recovered:
        raise LedgerBuildError('恢复结果缺少可发布的账本数据')
    if result.get('invariant_passed') is not True:
        raise LedgerBuildError('恢复核对未通过，拒绝发布账本')
    # 演练故障注入：指定恢复的首次重建强制失败，重试后成功（验证失败保留旧版、重试不重复发布）
    version.attempts += 1
    if recovery.id in LEDGER_FAIL_RECOVERY_IDS and version.attempts == 1:
        raise LedgerBuildError('注入故障：账本持久索引重建失败')
    points = []
    for point_no, key in enumerate(sorted(recovered), start=1):
        value = recovered[key]
        if isinstance(value, bool) or not isinstance(value, int):
            raise LedgerBuildError(f'账本点 {key} 的值不是整数，拒绝发布')
        points.append(LedgerIndexPoint(ledger_version_id=version.id, point_no=point_no,
                                       point_key=key, value=value))
    db.add_all(points)
    version.ledger = recovered
    version.input_sha256 = result.get('input_sha256', '')
    version.result_sha256 = result.get('result_sha256', '')
    version.last_error = None
    return points


def _promote(db, version, at):
    """一次性切换：新账本+新索引已在同事务写入，此处仅原子调整版本状态。"""
    current, previous = _active_versions(db, version.log_set_id)
    if current is None:
        version.state = 'current'
        version.published_at = at
        return True
    # 槽位规则：最多保留一份旧版；旧版仍有有效凭据时，新版继续等候
    if len(previous) >= LEDGER_MAX_OLD_VERSIONS:
        if any(_has_live_tokens(db, old.id, at) for old in previous):
            return False
        for old in previous:
            _retire(db, old)
        previous = []
    current.state = 'previous'
    version.state = 'current'
    version.published_at = at
    return True


def _retire(db, version):
    """槽位空出：删除旧版索引与凭据（旧版不再可查，查询走新版）。"""
    db.execute(delete(LedgerIndexPoint).where(LedgerIndexPoint.ledger_version_id == version.id))
    db.execute(delete(LedgerPageToken).where(LedgerPageToken.ledger_version_id == version.id))
    db.delete(version)


def process_pending(db):
    """worker 周期调用：按版本号顺序推进所有待发布版本，返回本次处理摘要。"""
    at = now()
    summary = {'published': [], 'failed': [], 'waiting': []}
    pending = db.scalars(select(LedgerVersion).where(LedgerVersion.state == 'pending')
                         .order_by(LedgerVersion.id)).all()
    for version in pending:
        recovery = db.get(Recovery, version.recovery_id)
        if recovery is None or recovery.status != 'completed':
            continue
        # 已重建完成但在等候槽位的版本不重复重建，只重试一次性切换
        if version.ledger is None:
            try:
                _build_index(db, version, recovery)
            except LedgerBuildError as exc:
                version.state = 'failed'
                version.last_error = str(exc)
                summary['failed'].append(version.id)
                continue
        if _promote(db, version, at):
            summary['published'].append(version.id)
        else:
            # 重建已完成但旧版槽位被有效凭据占用：保持 pending（索引随版本保留但不对外可查），槽位空出后发布
            summary['waiting'].append(version.id)
    db.flush()
    return summary


def retry_failed(db, version_id):
    """手动重试失败版本：复位为 pending 由 worker 重建；唯一约束保证不重复发布。"""
    version = db.get(LedgerVersion, version_id)
    if version is None:
        raise HTTPException(404, '账本版本不存在')
    if version.state != 'failed':
        raise HTTPException(409, '仅重建失败的版本可以重试')
    version.state = 'pending'
    version.last_error = None
    db.flush()
    return version


def list_versions(db, log_set_id=None):
    stmt = select(LedgerVersion).order_by(LedgerVersion.log_set_id, LedgerVersion.version_no.desc())
    if log_set_id is not None:
        stmt = select(LedgerVersion).where(LedgerVersion.log_set_id == log_set_id).order_by(LedgerVersion.version_no.desc())
    return db.scalars(stmt).all()


def _resolve_version(db, log_set_id, version_id):
    if version_id is not None:
        version = db.get(LedgerVersion, version_id)
        if version is None or version.log_set_id != log_set_id or version.state not in ('current', 'previous'):
            raise HTTPException(404, '该日志集没有可查的此版账本')
        return version
    version = db.scalar(select(LedgerVersion).where(LedgerVersion.log_set_id == log_set_id,
                                                    LedgerVersion.state == 'current'))
    if version is None:
        raise HTTPException(404, '该日志集尚无已发布账本')
    return version


def _issue_token(db, version, low, high, page_size, next_value, next_point_no, at):
    token = LedgerPageToken(token=secrets.token_hex(16), ledger_version_id=version.id,
                            low_value=low, high_value=high, page_size=page_size,
                            next_value=next_value, next_point_no=next_point_no,
                            expires_at=at + timedelta(seconds=PAGE_TOKEN_TTL_SECONDS))
    db.add(token)
    db.flush()
    return token


def _rows(db, version_id, low, high, page_size, anchor_value=None, anchor_point=None):
    """走持久索引的键集查询：同值按点号升序，凭据续翻不漏点不重读。"""
    conds = [LedgerIndexPoint.ledger_version_id == version_id,
             LedgerIndexPoint.value >= low, LedgerIndexPoint.value <= high]
    if anchor_value is not None:
        conds.append(or_(LedgerIndexPoint.value > anchor_value,
                         and_(LedgerIndexPoint.value == anchor_value,
                              LedgerIndexPoint.point_no > anchor_point)))
    return db.scalars(select(LedgerIndexPoint).where(and_(*conds))
                      .order_by(LedgerIndexPoint.value, LedgerIndexPoint.point_no)
                      .limit(page_size + 1)).all()


def query_range(db, log_set_id, low, high, page_size, ledger_version_id=None, page_token=None):
    at = now()
    if low > high:
        raise HTTPException(422, '区间下界不能大于上界')
    if not 1 <= page_size <= 200:
        raise HTTPException(422, '每页条数须在1—200之间')
    if page_token:
        stored = db.scalar(select(LedgerPageToken).where(LedgerPageToken.token == page_token))
        if stored is None:
            raise HTTPException(404, '翻页凭据不存在或已作废')
        if stored.expires_at <= at:
            raise HTTPException(410, '翻页凭据已过期，请按新区间重新查询')
        if stored.consumed_at is not None:
            raise HTTPException(410, '翻页凭据已使用，不能重复续翻')
        # 已发出的凭据在有效期内始终查凭据绑定的原版，不受新版本切换影响
        version = db.get(LedgerVersion, stored.ledger_version_id)
        if version is None or version.state not in ('current', 'previous'):
            raise HTTPException(410, '凭据绑定的账本版本已下架，请重新查询')
        low, high = stored.low_value, stored.high_value
        page_size = stored.page_size
        anchor_value, anchor_point = stored.next_value, stored.next_point_no
        rows = _rows(db, version.id, low, high, page_size, anchor_value, anchor_point)
        stored.consumed_at = at
    else:
        version = _resolve_version(db, log_set_id, ledger_version_id)
        rows = _rows(db, version.id, low, high, page_size)
        anchor_value = anchor_point = None

    page = rows[:page_size]
    has_more = len(rows) > page_size
    next_token = None
    next_cursor = None
    if has_more:
        tail = page[-1]
        tok = _issue_token(db, version, low, high, page_size, tail.value, tail.point_no, at)
        next_token = tok.token
        next_cursor = {'value': tail.value, 'point_no': tail.point_no}
    # 顺手清理已过期凭据，让旧版槽位可以及时空出
    db.execute(delete(LedgerPageToken).where(LedgerPageToken.expires_at <= at))
    db.flush()
    return {
        'log_set_id': log_set_id,
        'ledger_version_id': version.id,
        'version_no': version.version_no,
        'version_state': version.state,
        'range': {'low': low, 'high': high},
        'page_size': page_size,
        'points': [{'point_no': p.point_no, 'point_key': p.point_key, 'value': p.value} for p in page],
        'next_token': next_token,
        'next_cursor': next_cursor,
        'has_more': has_more,
        'token_expires_in_seconds': PAGE_TOKEN_TTL_SECONDS,
    }
