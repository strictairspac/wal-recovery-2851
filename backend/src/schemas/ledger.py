from pydantic import BaseModel, Field

class LedgerRangeQuery(BaseModel):
    log_set_id: int = Field(gt=0)
    low_value: int = Field(ge=-1000000000, le=1000000000)
    high_value: int = Field(ge=-1000000000, le=1000000000)
    page_size: int = Field(default=20, ge=1, le=200)
    ledger_version_id: int | None = Field(default=None, gt=0)
    page_token: str | None = Field(default=None, min_length=8, max_length=64)

class LedgerRetry(BaseModel):
    ledger_version_id: int = Field(gt=0)
