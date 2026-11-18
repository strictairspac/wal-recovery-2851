from fastapi import APIRouter, Depends, Query
from src.config.database import get_db
from src.middlewares.auth import current_user, writable
from src.schemas.ledger import LedgerRangeQuery, LedgerRetry
from src.services import ledger_service
from src.controllers.actions import audit
from src.utils.serialize import record
router = APIRouter()

@router.get('/ledger-versions')
def versions(log_set_id: int | None = Query(default=None, gt=0),
             db=Depends(get_db), user=Depends(current_user)):
    return [record(v) for v in ledger_service.list_versions(db, log_set_id)]

@router.post('/ledger-query')
def query(data: LedgerRangeQuery, db=Depends(get_db), user=Depends(current_user)):
    # 按值区间查同一日志集已发布账本，走随恢复生成的持久索引，键集凭据续翻
    return ledger_service.query_range(db, data.log_set_id, data.low_value, data.high_value,
                                      data.page_size, data.ledger_version_id, data.page_token)

@router.post('/ledger-versions/retry', status_code=202)
def retry(data: LedgerRetry, db=Depends(get_db), user=Depends(writable)):
    obj = ledger_service.retry_failed(db, data.ledger_version_id)
    audit(db, user, f'ledger:retry:{obj.id}')
    db.commit()
    return record(obj)
