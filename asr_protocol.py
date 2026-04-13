"""
utils/asr_protocol.py
火山引擎 ASR 二进制帧协议封装
参考：https://www.volcengine.com/docs/6561/80818

协议头结构（4 字节）
  Byte 0: protocol_version(4bit) | header_size(4bit)
  Byte 1: message_type(4bit)     | message_type_specific_flags(4bit)
  Byte 2: serialization_method(4bit) | message_compression(4bit)
  Byte 3: reserved(8bit)
"""

import gzip
import json
import struct
from typing import Union

# ── 协议常量 ──────────────────────────────────────────────────────────────────

PROTOCOL_VERSION  = 0b0001   # 1
HEADER_SIZE       = 0b0001   # 1（单位：4字节，即头大小 = 4 字节）

# message_type
MSG_TYPE_FULL_CLIENT_REQUEST = 0b0001   # 完整客户端请求（含初始化帧）
MSG_TYPE_AUDIO_ONLY_REQUEST  = 0b0010   # 仅音频数据帧
MSG_TYPE_FULL_SERVER_RESPONSE = 0b1001  # 完整服务端响应
MSG_TYPE_SERVER_ACK           = 0b1011  # 服务端 ACK
MSG_TYPE_SERVER_ERROR         = 0b1111  # 服务端错误响应

# message_type_specific_flags for audio request
FLAG_NONE        = 0b0000   # 普通帧
FLAG_LAST_PACKET = 0b0010   # 最后一个音频帧（LAST_NO_SEQUENCE）

# serialization_method
SERIALIZATION_JSON  = 0b0001
SERIALIZATION_NONE  = 0b0000

# message_compression
COMPRESSION_NONE = 0b0000
COMPRESSION_GZIP = 0b0001


# ── 协议头构造 ────────────────────────────────────────────────────────────────

def build_asr_header(
    message_type: int,
    message_type_specific_flags: int = FLAG_NONE,
    serialization_method: int = SERIALIZATION_JSON,
    message_compression: int = COMPRESSION_NONE,
) -> bytes:
    """
    构造 4 字节 ASR 协议头。

    Args:
        message_type: 消息类型（4bit）
        message_type_specific_flags: 消息类型特定标志（4bit）
        serialization_method: 序列化方式（4bit）
        message_compression: 压缩方式（4bit）

    Returns:
        4 字节的协议头 bytes
    """
    byte0 = ((PROTOCOL_VERSION & 0x0F) << 4) | (HEADER_SIZE & 0x0F)
    byte1 = ((message_type & 0x0F) << 4) | (message_type_specific_flags & 0x0F)
    byte2 = ((serialization_method & 0x0F) << 4) | (message_compression & 0x0F)
    byte3 = 0x00  # reserved
    return struct.pack("4B", byte0, byte1, byte2, byte3)


# ── 初始化帧构造 ──────────────────────────────────────────────────────────────

def build_full_client_request(
    app_id: str,
    token: str,
    cluster: str,
    uid: str = "user",
    language: str = "zh-CN",
    audio_format: str = "raw",
    sample_rate: int = 16000,
    encoding: str = "pcm",
    result_type: str = "full",
) -> bytes:
    """
    构造 WebSocket 连接后第一帧发送的初始化帧（Full Client Request）。

    帧格式：4 字节协议头 + 4 字节 payload 长度（大端） + payload（JSON）

    Args:
        app_id:       火山 ASR APP ID
        token:        火山 ASR Access Token
        cluster:      集群名称（如 volcengine_input_common）
        uid:          用户标识，任意字符串
        language:     识别语言，默认 zh-CN
        audio_format: 音频格式，默认 raw
        sample_rate:  采样率，默认 16000
        encoding:     编码方式，默认 pcm
        result_type:  返回结果类型，默认 full（含 result.text）

    Returns:
        完整的初始化帧 bytes
        """
    payload_dict = {
        "app": {
            "appid":   app_id,
            "token":   token,
            "cluster": cluster,
        },
        "user": {
            "uid": uid,
        },
        "request": {
            "reqid":       uid,
            "nbest":       1,
            "result_type": result_type,
            "sequence":    1,
        },
        "audio": {
            "format":      audio_format,
            "rate":        sample_rate,
            "language":    language,
            "bits":        16,
            "channel":     1,
            "codec":       encoding,
        },
    }

    payload_bytes = json.dumps(payload_dict, ensure_ascii=False).encode("utf-8")
    header = build_asr_header(
        message_type=MSG_TYPE_FULL_CLIENT_REQUEST,
        message_type_specific_flags=FLAG_NONE,
        serialization_method=SERIALIZATION_JSON,
        message_compression=COMPRESSION_NONE,
    )
    # 4 字节大端表示 payload 长度
    payload_size = struct.pack(">I", len(payload_bytes))
    return header + payload_size + payload_bytes


# ── 音频数据帧构造 ─────────────────────────────────────────────────────────────

def build_audio_only_request(
    audio_bytes: bytes,
    is_last: bool = False,
) -> bytes:
    """
    构造音频数据帧（Audio Only Request）。

    帧格式：4 字节协议头 + 4 字节 payload 长度（大端） + payload（raw audio bytes）

    Args:
        audio_bytes: 原始 PCM 音频数据
        is_last:     是否为最后一个音频帧；
                     True → 设置 FLAG_LAST_PACKET，触发 ASR 最终识别

    Returns:
        完整的音频帧 bytes
    """
    flags = FLAG_LAST_PACKET if is_last else FLAG_NONE
    header = build_asr_header(
        message_type=MSG_TYPE_AUDIO_ONLY_REQUEST,
        message_type_specific_flags=flags,
        serialization_method=SERIALIZATION_NONE,
        message_compression=COMPRESSION_NONE,
    )
    payload_size = struct.pack(">I", len(audio_bytes))
    return header + payload_size + audio_bytes


# ── 服务端响应解析 ─────────────────────────────────────────────────────────────

def parse_asr_response(data: Union[bytes, bytearray]) -> dict:
    """
    解析火山 ASR 服务端返回的二进制响应帧，提取识别文字。

    响应帧格式：
        4 字节协议头
        [4 字节 payload size]（如果 header_size == 1）
        payload（JSON）

    Args:
        data: 服务端推送的二进制帧

    Returns:
        {
            "text":     str,   # 识别出的文字（无结果时为空字符串）
            "is_final": bool,  # 是否为最终结果
            "code":     int,   # 业务状态码，0 表示成功
            "sequence": int,   # 帧序号，负值表示最终帧
        }
    """
    result = {
        "text":     "",
        "is_final": False,
        "code":     0,
        "sequence": 0,
    }

    if not data or len(data) < 4:
        result["code"] = -1
        return result

    # 解析协议头
    byte0, byte1, byte2, byte3 = data[0], data[1], data[2], data[3]
    header_size_units  = byte0 & 0x0F          # 单位：4 字节
    message_type       = (byte1 >> 4) & 0x0F
    # message_type_flags = byte1 & 0x0F         # 暂未使用
    # serialization     = (byte2 >> 4) & 0x0F
    compression        = byte2 & 0x0F

    header_bytes_len = header_size_units * 4   # 实际头字节数
    if len(data) <= header_bytes_len:
        result["code"] = -2
        return result

    # 读取 payload size（4 字节大端，紧跟在 header 之后）
    offset = header_bytes_len
    if len(data) < offset + 4:
        result["code"] = -3
        return result

    payload_size = struct.unpack(">I", data[offset: offset + 4])[0]
    offset += 4

    if len(data) < offset + payload_size:
        # 帧不完整，尽量使用已有数据
        payload_bytes = data[offset:]
    else:
        payload_bytes = data[offset: offset + payload_size]

    # 处理服务端错误帧
    if message_type == MSG_TYPE_SERVER_ERROR:
        try:
            payload_str = payload_bytes.decode("utf-8", errors="replace")
            err_obj = json.loads(payload_str)
            result["code"] = err_obj.get("code", -999)
        except Exception:
            result["code"] = -999
        return result

    # 解压
    if compression == COMPRESSION_GZIP:
        try:
            payload_bytes = gzip.decompress(payload_bytes)
        except Exception as e:
            result["code"] = -4
            return result

    # 解析 JSON
    try:
        payload_str = payload_bytes.decode("utf-8", errors="replace")
        payload_obj = json.loads(payload_str)
    except Exception:
        result["code"] = -5
        return result

    # 提取业务码
    result["code"] = payload_obj.get("code", 0)

    # 提取序号（负值 = 最终帧）
    sequence = payload_obj.get("sequence", 0)
    result["sequence"] = sequence
    result["is_final"] = (sequence < 0)

    # 提取识别文字
    # 响应结构：{ "result": [ {"text": "...", ...} ], ... }
    try:
        results_list = payload_obj.get("result", [])
        if results_list and isinstance(results_list, list):
            result["text"] = results_list[0].get("text", "")
    except Exception:
        result["text"] = ""

    return result
