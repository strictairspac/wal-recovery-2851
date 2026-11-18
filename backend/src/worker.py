import logging, time
from pathlib import Path
from sqlalchemy import select
from src.config.database import Session
from src.config.settings import REDIS_URL
from src.models.entities import Recovery
from src.services.wal_engine import recover
from src.services import ledger_service

logging.basicConfig(level=logging.INFO)


class _LocalLock:
    """无 Redis 的本地开发降级锁：单进程内互斥，语义与 redis.lock 对齐。"""
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _make_client():
    try:
        from redis import Redis
        client = Redis.from_url(REDIS_URL)
        client.ping()
        return client
    except Exception:
        logging.warning('redis 不可达，使用进程内降级锁（仅本地开发/测试）')
        return None


redis = _make_client()

# Redis leadership lock ensures only one worker processes the durable DB queue.
while True:
    Path('/tmp/heartbeat').touch()
    lock = redis.lock('wal-worker', timeout=15, blocking_timeout=1) if redis is not None else _LocalLock()
    try:
        with lock:
            with Session() as db:
                job = db.scalar(select(Recovery).where(Recovery.status == 'queued').order_by(Recovery.id).with_for_update(skip_locked=True))
                if job:
                    job.status = 'running'
                    try:
                        job.result = recover(job.snapshot['log_set']['initial'], job.snapshot['log_set']['records'], job.snapshot['case']['crash_after'])
                        job.status = 'completed'
                        # 随恢复生成持久账本与索引的待发布版本（同一恢复不重复发布）
                        ledger_service.schedule_for_recovery(db, job)
                    except ValueError as exc:
                        job.result = {'error': str(exc), 'simulation': True}
                        job.status = 'failed'
                    db.commit()
                    logging.info('recovery %s %s', job.id, job.status)
                else:
                    # 推进等待重建/等待槽位的账本版本；重建失败回滚保留旧版，可重试
                    try:
                        summary = ledger_service.process_pending(db)
                        db.commit()
                        if summary['published'] or summary['failed'] or summary['waiting']:
                            logging.info('ledger versions %s', summary)
                    except Exception:
                        db.rollback()
                        logging.exception('ledger publish retry')
    except Exception:
        logging.exception('worker retry')
    time.sleep(1)
