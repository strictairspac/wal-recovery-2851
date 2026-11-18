from datetime import datetime, timezone
from sqlalchemy import String, Integer, BigInteger, JSON, DateTime, ForeignKey, UniqueConstraint, Index
from sqlalchemy.orm import Mapped, mapped_column
from src.config.database import Base

def now(): return datetime.now(timezone.utc).replace(tzinfo=None)
class User(Base):
    __tablename__='users'
    id: Mapped[int]=mapped_column(primary_key=True)
    email: Mapped[str]=mapped_column(String(120),unique=True)
    role: Mapped[str]=mapped_column(String(20))
    password_hash: Mapped[str]=mapped_column(String(250))
class LogSet(Base):
    __tablename__='log_sets'
    id: Mapped[int]=mapped_column(primary_key=True)
    name: Mapped[str]=mapped_column(String(100),unique=True)
    initial: Mapped[dict]=mapped_column(JSON)
    records: Mapped[list]=mapped_column(JSON)
    description: Mapped[str]=mapped_column(String(500))
class CrashCase(Base):
    __tablename__='crash_cases'
    id: Mapped[int]=mapped_column(primary_key=True)
    log_set_id: Mapped[int]=mapped_column(ForeignKey('log_sets.id'))
    name: Mapped[str]=mapped_column(String(100),unique=True)
    crash_after: Mapped[int]=mapped_column(Integer)
class Recovery(Base):
    __tablename__='recoveries'
    id: Mapped[int]=mapped_column(primary_key=True)
    crash_case_id: Mapped[int]=mapped_column(ForeignKey('crash_cases.id'))
    label: Mapped[str]=mapped_column(String(120))
    status: Mapped[str]=mapped_column(String(20),default='queued')
    snapshot: Mapped[dict]=mapped_column(JSON)
    result: Mapped[dict | None]=mapped_column(JSON,nullable=True)
    created_at: Mapped[datetime]=mapped_column(DateTime,default=now)
class Audit(Base):
    __tablename__='audits'
    id: Mapped[int]=mapped_column(primary_key=True)
    actor: Mapped[str]=mapped_column(String(120))
    action: Mapped[str]=mapped_column(String(120))
    created_at: Mapped[datetime]=mapped_column(DateTime,default=now)

# 随恢复发布的账本版本：同一日志集只有一条 current，至多保留一条 previous
# pending=等待发布槽位或等待重建，failed=重建失败（保留旧版查询），current=当前版，previous=旧版
class LedgerVersion(Base):
    __tablename__='ledger_versions'
    id: Mapped[int]=mapped_column(primary_key=True)
    log_set_id: Mapped[int]=mapped_column(ForeignKey('log_sets.id'))
    recovery_id: Mapped[int]=mapped_column(ForeignKey('recoveries.id'),unique=True)
    version_no: Mapped[int]=mapped_column(Integer)
    state: Mapped[str]=mapped_column(String(20),default='pending')
    ledger: Mapped[dict]=mapped_column(JSON,nullable=True)
    input_sha256: Mapped[str]=mapped_column(String(64),default='')
    result_sha256: Mapped[str]=mapped_column(String(64),default='')
    published_at: Mapped[datetime|None]=mapped_column(DateTime,nullable=True)
    last_error: Mapped[str|None]=mapped_column(String(500),nullable=True)
    attempts: Mapped[int]=mapped_column(Integer,default=0)
    created_at: Mapped[datetime]=mapped_column(DateTime,default=now)
    __table_args__=(UniqueConstraint('log_set_id','version_no',name='uq_ledger_version_no'),
                    Index('ix_ledger_logset_state','log_set_id','state'))

# 随恢复生成的持久索引：恢复后账本逐点登记，按(值,点号)有序，区间与键集翻页都走它
class LedgerIndexPoint(Base):
    __tablename__='ledger_index_points'
    id: Mapped[int]=mapped_column(primary_key=True)
    ledger_version_id: Mapped[int]=mapped_column(ForeignKey('ledger_versions.id',ondelete='CASCADE'))
    point_no: Mapped[int]=mapped_column(Integer)
    point_key: Mapped[str]=mapped_column(String(50))
    value: Mapped[int]=mapped_column(BigInteger)
    __table_args__=(UniqueConstraint('ledger_version_id','point_no',name='uq_index_point_no'),
                    Index('ix_index_version_value_point','ledger_version_id','value','point_no'))

# 已发出的翻页凭据：绑定具体版本与查询区间，有效期内始终查原版，不漏点不重读；过期拒绝续翻
class LedgerPageToken(Base):
    __tablename__='ledger_page_tokens'
    id: Mapped[int]=mapped_column(primary_key=True)
    token: Mapped[str]=mapped_column(String(64),unique=True,index=True)
    ledger_version_id: Mapped[int]=mapped_column(ForeignKey('ledger_versions.id',ondelete='CASCADE'))
    low_value: Mapped[int]=mapped_column(BigInteger)
    high_value: Mapped[int]=mapped_column(BigInteger)
    page_size: Mapped[int]=mapped_column(Integer)
    next_value: Mapped[int|None]=mapped_column(BigInteger,nullable=True)
    next_point_no: Mapped[int|None]=mapped_column(Integer,nullable=True)
    expires_at: Mapped[datetime]=mapped_column(DateTime)
    consumed_at: Mapped[datetime|None]=mapped_column(DateTime,nullable=True)
    created_at: Mapped[datetime]=mapped_column(DateTime,default=now)
