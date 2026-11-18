# 初始化结构
SQLAlchemy metadata.create_all首次建users、log_sets、crash_cases、recoveries、audits。log_sets.name、crash_cases.name唯一，关联使用外键，入队冻结日志和崩溃点快照。暂无在线schema升级器。

## 账本版本与持久索引（随恢复发布）
新增三表，均由 metadata.create_all 在空库首次初始化时创建；已有库需补建：
- ledger_versions：同一日志集的账本版本，状态 pending（等候重建/等候槽位）/ failed（重建失败，旧版可查）/ current / previous。(log_set_id, version_no) 唯一，recovery_id 唯一保证同一恢复重试不重复发布。
- ledger_index_points：随恢复生成的持久索引，每点含 point_no/point_key/value，索引 (ledger_version_id, value, point_no) 支撑按值区间键集翻页，同值按点号升序。
- ledger_page_tokens：已发出的翻页凭据，绑定版本、区间、页长与续翻锚点，记过期与消费时间；有效期内始终查原版，不漏点不重读，过期或重用返回410。

至多保留一份 previous；其仍有未过期凭据时新发布版本保持 pending，凭据过期槽位空出后由 worker 一次性切换（新账本与新索引同事务）。重建失败时事务回滚，保留旧版，可经 POST /ledger-versions/retry 重试。
