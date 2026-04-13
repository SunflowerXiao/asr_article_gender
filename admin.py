import logging
import os

from flask import Flask, send_from_directory
from flask_cors import CORS
from flask_sock import Sock

from routes.generate import generate_bp
from routes.corpus import corpus_bp
from routes.voice import voice_bp

logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")

# ── ASR 环境变量（语音识别，缺失时仅 warning，不阻断启动）────────────────────
ASR_APP_ID       = os.environ.get("ASR_APP_ID", "")
ASR_ACCESS_TOKEN = os.environ.get("ASR_ACCESS_TOKEN", "")
ASR_CLUSTER      = os.environ.get("ASR_CLUSTER", "volcengine_input_common")

if not ASR_APP_ID or not ASR_ACCESS_TOKEN:
    logging.warning("[ASR] 环境变量 ASR_APP_ID / ASR_ACCESS_TOKEN 未配置，语音功能不可用")

app = Flask(__name__)
CORS(app)

# ── 初始化 WebSocket（flask-sock）──────────────────────────────────────────────
sock = Sock(app)

# ── 注册蓝图 ──────────────────────────────────────────────────────────────────
app.register_blueprint(generate_bp)
app.register_blueprint(corpus_bp)
app.register_blueprint(voice_bp)

# 将 sock 对象注入到 voice 蓝图，注册 WebSocket 路由
from routes.voice import register_ws_routes  # noqa: E402
register_ws_routes(sock)


# ── 首页 ──────────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return send_from_directory(os.path.dirname(__file__), "index.html")


if __name__ == "__main__":
    # debug 模式由环境变量 FLASK_DEBUG 控制，生产环境请勿设置为 true
    debug_mode = os.environ.get("FLASK_DEBUG", "false").lower() == "true"
    app.run(host="0.0.0.0", port=5000, debug=debug_mode)
