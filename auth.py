import time
from flask import request, jsonify
from config import AUTH_TOKEN

# IP 维度的每日请求计数（存内存，重启清零）
_request_count: dict = {}


def check_limit(ip: str) -> bool:
    """检查该 IP 今日是否超出免费调用次数（上限 5 次）"""
    today = time.strftime("%Y-%m-%d")
    key = f"{ip}_{today}"
    cnt = _request_count.get(key, 0)
    if cnt >= 5:
        return False
    _request_count[key] = cnt + 1
    return True


def require_auth():
    """
    Bearer Token 鉴权。
    通过返回 None；鉴权失败返回 (Response, 401) 元组。
    用法：
        err = require_auth()
        if err: return err
    """
    token = request.headers.get("Authorization", "")
    if token != f"Bearer {AUTH_TOKEN}":
        return jsonify({"code": 401, "msg": "未授权，请提供有效的 Authorization Token"}), 401
    return None
