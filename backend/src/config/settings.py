import os
DATABASE_URL = os.environ['DATABASE_URL']
REDIS_URL = os.getenv('REDIS_URL', 'redis://redis:6379/0')
JWT_SECRET = os.environ['JWT_SECRET']
# 翻页凭据有效期（秒）：已发出的凭据在该窗口内始终翻凭据绑定的原版账本
PAGE_TOKEN_TTL_SECONDS = int(os.getenv('PAGE_TOKEN_TTL_SECONDS', '600'))
# 最多保留一份旧版账本
LEDGER_MAX_OLD_VERSIONS = int(os.getenv('LEDGER_MAX_OLD_VERSIONS', '1'))
# 演练用故障注入：逗号分隔的恢复ID，其首次账本重建强制失败，重试可成功（不重复发布）
LEDGER_FAIL_RECOVERY_IDS = {int(x) for x in os.getenv('LEDGER_FAIL_RECOVERY_IDS', '').split(',') if x.strip()}
