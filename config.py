import os
from dotenv import load_dotenv
from volcenginesdkarkruntime import Ark

load_dotenv()

# ── 密钥与鉴权配置（全部从环境变量读取，禁止硬编码）──────────────────────────
API_KEY = os.environ.get("ARK_API_KEY")
if not API_KEY:
    raise RuntimeError("ARK_API_KEY 环境变量未设置，请在启动前配置该变量")

AUTH_TOKEN = os.environ.get("API_AUTH_TOKEN")
if not AUTH_TOKEN:
    raise RuntimeError("API_AUTH_TOKEN 环境变量未设置，请在启动前配置该变量")

# ── Ark 客户端（全局单例，供所有路由复用）─────────────────────────────────────
ark_client = Ark(
    base_url="https://ark.cn-beijing.volces.com/api/v3",
    api_key=API_KEY,
)

# ── Token 消耗记录与告警配置 ──────────────────────────────────────────────────
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TOKEN_LOG_FILE  = os.path.join(_BASE_DIR, "token_usage.log")
ALERT_LOG_FILE  = os.path.join(_BASE_DIR, "token_alerts.log")

SINGLE_CALL_TOKEN_LIMIT = int(os.environ.get("SINGLE_CALL_TOKEN_LIMIT", "5000"))
DAILY_TOKEN_LIMIT       = int(os.environ.get("DAILY_TOKEN_LIMIT",        "50000"))
INPUT_TOKEN_PRICE       = float(os.environ.get("INPUT_TOKEN_PRICE",      "0.0008"))
OUTPUT_TOKEN_PRICE      = float(os.environ.get("OUTPUT_TOKEN_PRICE",     "0.002"))
DAILY_COST_LIMIT        = float(os.environ.get("DAILY_COST_LIMIT",       "10.0"))
