"""
routes/voice.py
语音 ASR 相关路由：
  - /ws/asr          WebSocket 端点：前端音频 → 火山 ASR → 识别文字
  - /api/voice/parse HTTP POST 端点：豆包解析自然语言 → 表单字段
"""

import io
import json
import logging
import os
import re
import threading
import uuid
from typing import Optional

import websocket  # websocket-client
from flask import Blueprint, jsonify, request

from config import ark_client
from utils.asr_protocol import (
    build_audio_only_request,
    build_full_client_request,
    parse_asr_response,
)

logger = logging.getLogger(__name__)

voice_bp = Blueprint("voice", __name__)

# 火山 ASR WebSocket 接入地址
ASR_WS_URL = "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel"

# PCM 转换参数
_PCM_SAMPLE_RATE  = 16000
_PCM_CHANNELS     = 1
_PCM_SAMPLE_WIDTH = 2   # 16-bit


def _try_convert_to_pcm(raw_bytes: bytes) -> bytes:
    """
    尝试将任意格式（WebM / Ogg / Opus 等）音频转换为 16kHz 单声道 16-bit PCM。
    依赖 pydub（pip install pydub）及系统 ffmpeg。
    若 pydub 未安装或转换失败，则原样返回（降级处理）。

    Args:
        raw_bytes: 原始音频字节（来自浏览器 MediaRecorder）

    Returns:
        PCM 字节（转换成功）或原始字节（降级）
    """
    try:
        from pydub import AudioSegment  # type: ignore
    except ImportError:
        logger.warning(
            "[ASR] pydub 未安装，跳过音频格式转换，ASR 准确率可能下降。"
            "请执行：pip install pydub 并安装 ffmpeg"
        )
        return raw_bytes

    try:
        audio = AudioSegment.from_file(io.BytesIO(raw_bytes))
        audio = (
            audio
            .set_frame_rate(_PCM_SAMPLE_RATE)
            .set_channels(_PCM_CHANNELS)
            .set_sample_width(_PCM_SAMPLE_WIDTH)
        )
        pcm_buf = io.BytesIO()
        audio.export(pcm_buf, format="raw")
        pcm_bytes = pcm_buf.getvalue()
        logger.info(
            "[ASR] 音频格式转换成功：%d 字节 → %d 字节 PCM（16kHz/单声道/16bit）",
            len(raw_bytes), len(pcm_bytes),
        )
        return pcm_bytes
    except Exception as exc:
        logger.warning("[ASR] 音频格式转换失败，使用原始数据（降级）: %s", exc)
        return raw_bytes


# ══════════════════════════════════════════════════════════════════════════════
# 任务 8：/ws/asr  WebSocket 端点
# ══════════════════════════════════════════════════════════════════════════════

def register_ws_routes(sock):
    """
    将 WebSocket 路由注册到 flask-sock 实例。
    由 admin.py 在创建 Sock(app) 之后调用。
    """

    @sock.route("/ws/asr")
    def ws_asr(ws):
        """
        前端 WebSocket 端点，作为前端与火山 ASR 之间的中转代理。

        协议约定（前端 → 服务端）：
          - 二进制帧：WebM/Ogg 音频分片（浏览器 MediaRecorder 输出）
          - 文本帧 "END"：通知服务端音频结束，触发 PCM 转换后发送给 ASR

        协议约定（服务端 → 前端）：
          - JSON 字符串：{"text": "识别内容", "is_final": false/true, "code": 0}
          - JSON 字符串：{"error": "错误信息"} 发生异常时

        音频处理流程：
          1. 收集所有二进制帧到缓冲区
          2. 收到 "END" 后，将完整缓冲区从 WebM/Ogg → PCM（16kHz/单声道/16bit）
          3. 按 4096 字节分片流式发送给火山 ASR（声明 PCM 格式，格式声明与实际一致）
        """
        # 从环境变量读取 ASR 配置（允许运行期动态读取，支持热更新）
        app_id  = os.environ.get("ASR_APP_ID", "")
        token   = os.environ.get("ASR_ACCESS_TOKEN", "")
        cluster = os.environ.get("ASR_CLUSTER", "volcengine_input_common")

        if not app_id or not token:
            ws.send(json.dumps({"error": "ASR 服务未配置，请联系管理员"}, ensure_ascii=False))
            return

        uid = str(uuid.uuid4())
        asr_ws_holder: list = [None]
        asr_error: list = []

        # ── 火山 ASR 响应处理（在独立线程中运行） ────────────────────────────
        def on_asr_message(ws_conn, message):
            """接收火山 ASR 推送的响应，解析后转发给前端"""
            try:
                if isinstance(message, str):
                    return
                result = parse_asr_response(message)
                if result["code"] != 0:
                    logger.warning("[ASR] 服务端业务错误 code=%s", result["code"])
                    ws.send(json.dumps(
                        {"error": f"ASR 服务错误，code={result['code']}"},
                        ensure_ascii=False,
                    ))
                    return
                if result["text"] or result["is_final"]:
                    ws.send(json.dumps({
                        "text":     result["text"],
                        "is_final": result["is_final"],
                        "code":     result["code"],
                    }, ensure_ascii=False))
            except Exception as exc:
                logger.exception("[ASR] on_asr_message 异常: %s", exc)

        def on_asr_error(ws_conn, error):
            asr_error.append(str(error))
            logger.error("[ASR] 火山 ASR WebSocket 错误: %s", error)
            try:
                ws.send(json.dumps({"error": f"ASR 连接错误: {error}"}, ensure_ascii=False))
                          except Exception:
                pass

        def on_asr_close(ws_conn, close_status_code, close_msg):
            logger.info("[ASR] 火山 ASR 连接关闭 code=%s msg=%s", close_status_code, close_msg)

        def on_asr_open(ws_conn):
            logger.info("[ASR] 已连接火山 ASR")

        # ── 建立到火山 ASR 的 WebSocket 连接（后台线程） ─────────────────────
        asr_ws = websocket.WebSocketApp(
            ASR_WS_URL,
            on_open=on_asr_open,
            on_message=on_asr_message,
            on_error=on_asr_error,
            on_close=on_asr_close,
        )
        asr_ws_holder[0] = asr_ws

        asr_thread = threading.Thread(
            target=asr_ws.run_forever,
            kwargs={"ping_interval": 20, "ping_timeout": 10},
            daemon=True,
        )
        asr_thread.start()

        # 等待连接就绪（最多 5 秒）
        import time
        for _ in range(50):
            if asr_ws.sock and asr_ws.sock.connected:
                break
            time.sleep(0.1)

        if not (asr_ws.sock and asr_ws.sock.connected):
            ws.send(json.dumps({"error": "无法连接到 ASR 服务，请稍后重试"}, ensure_ascii=False))
            return

        # ── 主循环：收集前端音频分片，END 后转换格式并发给火山 ASR ──────────
        audio_buffer = bytearray()   # 收集所有音频分片
        _CHUNK_SIZE  = 4096          # 发送给 ASR 的分片大小（字节）

        def _flush_to_asr(pcm_bytes: bytes) -> None:
            """将 PCM 字节以分片方式发送给火山 ASR"""
            if not pcm_bytes:
                return

            # 首帧：发初始化帧（声明 PCM 格式，与实际音频匹配）
            init_frame = build_full_client_request(
                app_id=app_id,
                token=token,
                cluster=cluster,
                uid=uid,
                audio_format="raw",
                encoding="pcm",
                sample_rate=_PCM_SAMPLE_RATE,
            )
            asr_ws.send(init_frame, opcode=websocket.ABNF.OPCODE_BINARY)

            # 分片发送音频数据
            total = len(pcm_bytes)
            offset = 0
            while offset < total:
                chunk = pcm_bytes[offset: offset + _CHUNK_SIZE]
                offset += _CHUNK_SIZE
                is_last = (offset >= total)
                audio_frame = build_audio_only_request(chunk, is_last=is_last)
                asr_ws.send(audio_frame, opcode=websocket.ABNF.OPCODE_BINARY)

        try:
            while True:
                message = ws.receive()
                if message is None:
                    # 客户端断开，尝试发送剩余缓冲（如果有）
                    if audio_buffer:
                        pcm = _try_convert_to_pcm(bytes(audio_buffer))
                        _flush_to_asr(pcm)
                    break

                # 文本控制帧
                if isinstance(message, str):
                    if message.strip().upper() == "END":
                        # 收到结束信号：转换格式后整体发给 ASR
                        if audio_buffer:
                            logger.info(
                                "[ASR] 收到 END，开始转换音频 %d 字节 → PCM", len(audio_buffer)
                            )
                            pcm = _try_convert_to_pcm(bytes(audio_buffer))
                            _flush_to_asr(pcm)
                            audio_buffer.clear()
                        else:
                            # 无音频数据，发一个空的末帧触发最终识别
                            try:
                                last_frame = build_audio_only_request(b"", is_last=True)
                                asr_ws.send(last_frame, opcode=websocket.ABNF.OPCODE_BINARY)
                            except Exception as exc:
                                logger.warning("[ASR] 发送末帧失败: %s", exc)
                    continue

                # 二进制音频帧 —— 追加到缓冲区，等待 END 后统一转换
                if not isinstance(message, (bytes, bytearray)):
                    continue
                audio_buffer.extend(message)

        except Exception as exc:
            logger.exception("[ASR] ws_asr 主循环异常: %s", exc)
        finally:
            try:
                asr_ws.close()
            except Exception:
                pass
            logger.info("[ASR] /ws/asr 会话结束 uid=%s", uid)
          # ══════════════════════════════════════════════════════════════════════════════
# 任务 9：/api/voice/parse  豆包语义解析接口
# ══════════════════════════════════════════════════════════════════════════════

# 合法枚举值（与 generate.py 保持一致）
_VALID_DURATIONS = {"15秒", "30秒", "60秒"}
_VALID_STYLES    = {"干货", "情感", "搞笑", "带货", "探店"}

# 豆包解析 Prompt 模板
_PARSE_SYSTEM_PROMPT = """\
你是一个表单信息提取助手。用户会说一段自然语言描述短视频需求，你需要从中提取三个字段：
1. theme（视频主题）：字符串，最多50字
2. duration（视频时长）：只能是以下之一："15秒"、"30秒"、"60秒"
3. style（文案风格）：只能是以下之一："干货"、"情感"、"搞笑"、"带货"、"探店"

如果三个字段都能从用户输入中明确推断出来，输出以下 JSON（不要有多余文字）：
{"status":"ok","theme":"...","duration":"...","style":"..."}

如果任意字段无法确定，输出以下 JSON，并在 question 中给出一个简短的追问：
{"status":"ask","question":"..."}

注意：
- 输出必须是合法 JSON，不要用 markdown 代码块包裹
- duration 和 style 只能从给定枚举中选取，不要发明新值
- theme 需简洁，去掉"帮我""做一个"等无关词语
"""


def _call_doubao_parse(text: str) -> dict:
    """
    调用豆包大模型解析自然语言文本，返回表单字段或追问。
    返回格式：
      成功：{"status":"ok","theme":"...","duration":"...","style":"..."}
      追问：{"status":"ask","question":"..."}
      错误：{"status":"error","message":"..."}
    """
    try:
        resp = ark_client.responses.create(
            model="doubao-seed-2-0-pro-260215",
            input=[
                {"role": "system", "content": [{"type": "input_text", "text": _PARSE_SYSTEM_PROMPT}]},
                {"role": "user",   "content": [{"type": "input_text", "text": text}]},
            ],
            max_output_tokens=256,
        )
        content = ""
        for item in resp.output:
            if item.type == "message":
                content = item.content[0].text
                break
    except Exception as exc:
        logger.exception("[VOICE_PARSE] 豆包调用失败: %s", exc)
        return {"status": "error", "message": "AI 服务暂时不可用，请稍后重试"}

    # 尝试解析 JSON
    result = _safe_parse_json(content)
    if result is None:
        logger.warning("[VOICE_PARSE] JSON 解析失败，原始内容: %s", content)
        return {"status": "error", "message": "AI 返回格式异常，请重试"}

    status = result.get("status")
    if status == "ok":
        # 校验枚举值，非法时退化为追问
        duration = result.get("duration", "")
        style    = result.get("style", "")
        theme    = (result.get("theme", "") or "").strip()

        if duration not in _VALID_DURATIONS:
            return {"status": "ask", "question": f"请问您希望视频时长是多少？可选：{'、'.join(sorted(_VALID_DURATIONS))}"}
        if style not in _VALID_STYLES:
            return {"status": "ask", "question": f"请问您希望什么风格的视频？可选：{'、'.join(sorted(_VALID_STYLES))}"}
        if not theme:
            return {"status": "ask", "question": "请问您希望视频的主题是什么？"}

        return {"status": "ok", "theme": theme, "duration": duration, "style": style}

    elif status == "ask":
        question = (result.get("question") or "").strip()
        if not question:
            question = "请补充更多信息，以便我为您生成脚本。"
        return {"status": "ask", "question": question}

    return {"status": "error", "message": "AI 返回状态未知，请重试"}
def _safe_parse_json(content: str) -> Optional[dict]:
    """尝试多种方式从字符串中解析 JSON 对象"""
    # 1. 直接解析
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        pass

    # 2. 去掉 markdown 代码块标记
    stripped = re.sub(r"^```(?:json)?\s*", "", content.strip(), flags=re.MULTILINE)
    stripped = re.sub(r"```\s*$", "", stripped.strip(), flags=re.MULTILINE)
    try:
        return json.loads(stripped.strip())
    except json.JSONDecodeError:
        pass

    # 3. 用正则提取第一个 {...} 块
    match = re.search(r"\{.*\}", content, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass

    return None


@voice_bp.route("/api/voice/parse", methods=["POST"])
def voice_parse():
    """
    接收语音识别结果，调用豆包大模型解析出表单字段。

    请求体：
        {"text": "帮我做一个30秒的护肤干货视频"}

    响应体（解析成功）：
        {"status":"ok","theme":"护肤干货","duration":"30秒","style":"干货"}

    响应体（信息不足，需追问）：
        {"status":"ask","question":"请问您希望视频时长是多少秒？"}
    """
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"status": "error", "message": "请求格式错误，需要 JSON 格式请求体"}), 400

    text = (data.get("text") or "").strip()
    if not text:
        return jsonify({"status": "error", "message": "text 字段不能为空"}), 400

    if len(text) > 500:
        return jsonify({"status": "error", "message": "输入文本过长，请精简后重试"}), 400

    result = _call_doubao_parse(text)

    if result.get("status") == "error":
        return jsonify(result), 500

    return jsonify(result), 200
