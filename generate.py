import json
import re

from flask import Blueprint, Response, jsonify, request, stream_with_context

from config import ark_client
from utils.auth import check_limit, require_auth
from utils.token_tracker import check_and_alert, log_token_usage

# 资料库模块（可选，未安装时降级为静态示例）
try:
    import corpus_manager
    _CORPUS_AVAILABLE = True
except ImportError:
    _CORPUS_AVAILABLE = False

generate_bp = Blueprint("generate", __name__)

# ── 合法枚举值 ────────────────────────────────────────────────────────────────
_VALID_DURATIONS = {"15秒", "30秒", "60秒"}
_VALID_STYLES    = {"干货", "情感", "搞笑", "带货", "探店"}


# ══════════════════════════════════════════════════════════════════════════════
# 工具函数
# ══════════════════════════════════════════════════════════════════════════════

def _parse_json_result(content: str):
    """尝试多种方式从模型返回内容中解析出 JSON 对象"""
    # 1. 直接解析
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        pass

    # 2. 去掉 markdown 代码块标记后再解析
    stripped = re.sub(r'^```(?:json)?\s*', '', content.strip(), flags=re.MULTILINE)
    stripped = re.sub(r'```\s*$', '', stripped.strip(), flags=re.MULTILINE)
    try:
        return json.loads(stripped.strip())
    except json.JSONDecodeError:
        pass

    # 3. 用正则提取第一个 {...} 块
    match = re.search(r'\{.*\}', content, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass

    return None


def _build_dynamic_examples(theme: str, style: str) -> str:
    """
    从资料库检索与当前 theme/style 最相似的爆款案例，拼成 prompt 片段。
    资料库为空或检索失败时返回空字符串（调用方降级用静态示例）。
    """
    if not _CORPUS_AVAILABLE:
        return ""
    try:
        results = corpus_manager.search_similar(theme=theme, style=style, top_k=3)
        if not results:
            return ""
        lines = ["# 真实爆款参考案例（来自微信公众号，与你的主题高度相关，请参考其语气和结构）\n"]
        for i, item in enumerate(results, 1):
            title   = item.get("title", "")
            content = item.get("content", "")
            body = content.split("\n", 1)[-1].strip() if "\n" in content else content
            lines.append(f"案例{i}：《{title}》")
            lines.append(f"{body[:300]}")
            lines.append("")
        return "\n".join(lines)
    except Exception as e:
        print(f"[CORPUS] 检索爆款案例失败（已降级为静态示例）: {e}")
        return ""


def _build_prompt(theme: str, duration: str, style: str, corpus_section: str = "") -> str:
    """拼装完整的生成 prompt"""
    return f"""# 角色设定
你是拥有10年经验的短视频爆款文案专家，曾操盘过500+百万播放量的口播视频。你深谙抖音/视频号的流量密码，擅长用口语化表达抓住用户注意力，精通"痛点共鸣+价值输出+行动号召"的爆款公式。

# 任务目标
根据用户提供的主题、时长、风格，生成高质量的口播脚本、爆款标题和热门标签。

# 写作技巧指南

## 1. 开头钩子（前3秒必须抓住注意力）
- 痛点提问："你是不是也经常..."
- 数据冲击："90%的人都不知道..."
- 反常识："别再XXX了，其实..."
- 身份共鸣："打工人注意了..."
- 结果前置："我用这个方法，3天就..."

## 2. 中间内容（价值交付）
- 用"我以前也...后来发现..."建立信任
- 分步骤/分要点，但用口语化连接（不要出现"第一、第二"）
- 加入具体细节和场景，增强真实感
- 适当使用感叹词和语气词（真的、绝了、太香了）

## 3. 结尾行动号召
- 引导互动："点赞收藏，免得找不到"
- 制造紧迫感："趁现在知道的人还不多..."
- 承诺价值："坚持一周，你会回来感谢我"

# Few-shot 示例

## 示例1：干货风格（60秒）
输入：主题=护肤顺序，时长=60秒，风格=干货
输出：
{{
  "script": "姐妹们注意了！今天分享一个我用了三年的护肤秘诀，真的绝了！以前我皮肤特别差，暗沉、长痘、毛孔粗大，什么护肤品都试过，就是没效果。后来我发现，问题出在护肤顺序上！90%的人都搞错了，难怪皮肤越来越差。正确的顺序应该是：先用温和的氨基酸洗面奶清洁，然后用保湿型化妆水轻拍三遍，让皮肤充分吸收水分，接着用精华液重点涂抹在问题区域，最后一定要用乳液锁住水分。记住这个顺序，坚持一周，你的皮肤一定会让你惊喜！",
  "titles": ["99%的人都不知道的护肤顺序", "皮肤差的姐妹一定要看", "用了三年的护肤秘诀公开", "别再乱涂护肤品了", "这样护肤皮肤越来越好"],
  "tags": ["护肤干货", "素颜技巧", "护肤顺序", "新手护肤", "变美秘籍"]
}}

## 示例2：带货风格（30秒）
输入：主题=蓝牙耳机推荐，时长=30秒，风格=带货
输出：
{{
  "script": "家人们，今天必须给你们安利这个我最近挖到的宝藏蓝牙耳机！说实话，一开始我也觉得几十块钱的耳机能好到哪去，结果用了一周直接真香！音质绝了，低音浑厚高音清晰，戴着跑步狂甩不掉，续航更是离谱，充一次电能用整整两天！关键是这个价格，一杯奶茶钱就能拿下，学生党打工人闭眼入！链接我放小黄车了，库存不多，赶紧冲！",
  "titles": ["百元内蓝牙耳机天花板", "学生党必入的平价好物", "用了就回不去的蓝牙耳机", "这个价格我真的会谢", "平价耳机中的战斗机"],
  "tags": ["蓝牙耳机推荐", "平价好物", "学生党必备", "数码好物", "性价比之王"]
}}

## 示例3：情感风格（15秒）
输入：主题=职场焦虑，时长=15秒，风格=情感
输出：
{{
  "script": "你是不是也经常这样？白天在公司强颜欢笑，晚上回到家却焦虑得睡不着。别怕，你不是一个人。记住，工作只是生活的一部分，别让它偷走你的快乐。今天下班，给自己买束花吧，你值得被温柔对待。",
  "titles": ["致每一个深夜焦虑的你", "打工人必看", "别让生活偷走你的快乐", "职场人的深夜emo", "今天你焦虑了吗"],
  "tags": ["职场焦虑", "情感共鸣", "打工人日常", "治愈系", "心理健康"]
}}

# 负面示例（避免以下问题）
❌ 不要写成书面语："首先，我们需要了解..." → ✅ 应该口语化："我跟你说啊..."
❌ 不要空洞说教："大家要好好学习" → ✅ 应该具体："我当年就是吃了没文化的亏..."
❌ 不要堆砌形容词："非常好特别棒超级厉害" → ✅ 应该用细节："用了一周，皮肤真的亮了一个度"
❌ 不要生硬推销："快来买吧" → ✅ 应该真诚分享："我自己用了三个月才敢推荐"

# 质量标准（生成前自检）
✅ 开头3秒内是否有钩子抓住注意力？
✅ 是否有具体的场景/细节/数据增强真实感？
✅ 语言是否足够口语化，像朋友聊天？
✅ 是否有明确的行动号召或情感共鸣点？
✅ 标题是否有痛点、悬念或数字？
✅ 标签是否精准、热门、不含#符号？
{corpus_section}
# 用户输入
主题：{theme}
时长：{duration}
风格：{style}

# 输出要求
只输出一个合法的 JSON 对象，不要有任何多余的文字、解释、代码块标记（不要用```包裹）。
JSON 结构必须严格如下：
{{
  "script": "口播脚本正文，口语化，自然流畅，符合{duration}时长，运用上述写作技巧",
  "titles": ["标题1", "标题2", "标题3", "标题4", "标题5"],
  "tags": ["标签1", "标签2", "标签3", "标签4", "标签5"]
}}

注意：
- script 是一段连续的口播正文，不分条，字数根据{duration}调整（15秒约80字，30秒约150字，60秒约300字）
- titles 必须是5个爆款标题，包含痛点/悬念/数字/反差等元素
- tags 必须是5个热门标签，精准匹配主题，不含 # 符号
- 整体风格必须符合{style}的特点""".strip()
def _validate_generate_params(data: dict):
    """
    校验生成接口的请求参数。
    返回 (theme, duration, style) 或抛出 ValueError。
    """
    theme    = (data.get("theme",    "") or "").strip()
    duration = (data.get("duration", "") or "").strip()
    style    = (data.get("style",    "") or "").strip()

    if not theme:
        raise ValueError("主题不能为空")
    if len(theme) > 50:
        raise ValueError("主题最多50个字符")
    if duration not in _VALID_DURATIONS:
        raise ValueError("时长参数不合法，可选值：15秒、30秒、60秒")
    if style not in _VALID_STYLES:
        raise ValueError("风格参数不合法，可选值：干货、情感、搞笑、带货、探店")

    return theme, duration, style


def _extract_token_usage(resp) -> dict:
    """从 chat.completions 响应中提取 token 用量（prompt_tokens / completion_tokens）"""
    usage = getattr(resp, "usage", None)
    input_tokens  = getattr(usage, "prompt_tokens",     0) if usage else 0
    output_tokens = getattr(usage, "completion_tokens", 0) if usage else 0
    return {
        "input_tokens":  input_tokens,
        "output_tokens": output_tokens,
        "total_tokens":  input_tokens + output_tokens,
    }


def _build_messages(history: list, new_user_text: str) -> list:
    """
    将前端维护的 **结构化** 历史记录 + 本轮新消息转换为 API 所需的消息列表。

    结构化历史条目格式（前端传入）：

      用户消息 · 首轮（type="initial"）：
        {"role": "user", "type": "initial",
         "theme": "...", "duration": "...", "style": "..."}

      用户消息 · 后续轮（type="refinement"）：
        {"role": "user", "type": "refinement",
         "feedback": "..."}

      AI 回复（type="script"）：
        {"role": "assistant", "type": "script",
         "content": "<raw JSON string>",
         "script": "...", "titles": [...], "tags": [...]}

    基于结构化字段按需重建 API 文本：
      - "initial"    → 仅携带主题/时长/风格，不重传完整 prompt（节省 token）
      - "refinement" → 携带修改意见 + 格式要求
      - fallback     → 直接使用 content 字段（兼容旧数据）

    滑动窗口：最多保留最近 MAX_HISTORY_TURNS 轮（每轮 2 条消息）。
    """
    MAX_HISTORY_TURNS = 5

    if not isinstance(history, list):
        history = []
    if len(history) > MAX_HISTORY_TURNS * 2:
        history = history[-(MAX_HISTORY_TURNS * 2):]

    messages = []
    for msg in history:
        role     = msg.get("role", "user")
        msg_type = msg.get("type", "")

        if role == "user":
            if msg_type == "initial":
                # 从结构化字段重建精简请求（无需重传千字 prompt）
                t = msg.get("theme",    "")
                d = msg.get("duration", "")
                s = msg.get("style",    "")
                text = f"请生成短视频脚本。\n主题：{t}\n时长：{d}\n风格：{s}"

            elif msg_type == "refinement":
                # 从结构化字段重建修改要求
                fb   = msg.get("feedback", "")
                text = (
                    f"请根据以下反馈修改脚本：\n{fb}\n\n"
                    f"要求：保持 JSON 格式，只返回修改后的完整 JSON 对象，不要有其他文字。"
                )

            else:
                # 兼容旧格式：直接使用 content 字段
                text = str(msg.get("content", ""))

            messages.append({"role": "user", "content": text})

        else:  # assistant / script
            # 使用 content（原始 JSON 字符串），结构化字段仅供前端展示
            content = str(msg.get("content", ""))
            messages.append({"role": "assistant", "content": content})

    # 追加本轮新消息
    messages.append({"role": "user", "content": new_user_text})
    return messages


# ══════════════════════════════════════════════════════════════════════════════
# 路由
# ══════════════════════════════════════════════════════════════════════════════
@generate_bp.route("/api/generate", methods=["POST"])
def generate():
    err = require_auth()
    if err:
        return err

    if not check_limit(request.remote_addr):
        return jsonify({"code": 403, "msg": "今日免费次数已用完", "script": "", "titles": [], "tags": ""})

    data = request.get_json(silent=True)
    if not data:
        return jsonify({"code": 400, "msg": "请求格式错误，需要 JSON 格式请求体"})

    try:
        theme, duration, style = _validate_generate_params(data)
    except ValueError as e:
        return jsonify({"code": 400, "msg": str(e)})

    dynamic_examples = _build_dynamic_examples(theme, style)
    corpus_section   = f"\n{dynamic_examples}\n" if dynamic_examples else ""
    prompt           = _build_prompt(theme, duration, style, corpus_section)

    try:
        resp = ark_client.chat.completions.create(
            model="doubao-seed-2-0-pro-260215",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=1024,
        )
        token_usage = _extract_token_usage(resp)
        print(f"[TOKEN_USAGE] input: {token_usage['input_tokens']}, "
              f"output: {token_usage['output_tokens']}, total: {token_usage['total_tokens']}")

        content = ""
        if resp.choices:
            content = resp.choices[0].message.content or ""

        if not content:
            print("[ERROR] 模型返回内容为空")
            return jsonify({"code": 500, "msg": "AI 服务返回内容为空，请稍后重试"})

    except Exception as e:
        print(f"[ERROR] 大模型调用失败: {e}")
        return jsonify({"code": 500, "msg": "AI 服务暂时不可用，请稍后重试"})

    result = _parse_json_result(content)
    if result is None:
        print(f"[ERROR] JSON 解析失败，原始内容: {content}")
        return jsonify({"code": 500, "msg": "内容生成格式异常，请重试"})

    cost   = log_token_usage(theme, duration, style, token_usage)
    alerts = check_and_alert(token_usage, cost, theme)
    print(f"[SUCCESS] 生成成功 | theme='{theme}' | tokens: {token_usage['total_tokens']} | cost: ¥{cost:.4f}")
    if alerts:
        print(f"[WARNING] 触发 {len(alerts)} 条告警")

    return jsonify({
        "code":   200,
        "msg":    "生成成功",
        "script": result.get("script", ""),
        "titles": result.get("titles", [])[:5],
        "tags":   result.get("tags", []),
    })


@generate_bp.route("/api/generate/stream", methods=["POST"])
def generate_stream():
    """
    流式输出接口（SSE），支持多轮会话。

    请求体新增字段：
        history  : list  可选，前端维护的历史消息列表
                         格式：[{"role": "user"/"assistant", "content": "string"}, ...]
        feedback : str   可选，后续轮次用户的修改意见

    响应新增字段（done 事件）：
        full_text : str  本轮模型完整输出文本，前端存入 history 后传回下一轮
    """
    print('1111111111111111')
    err = require_auth()
        if err:
        return err

    if not check_limit(request.remote_addr):
        return jsonify({"code": 403, "msg": "今日免费次数已用完", "script": "", "titles": [], "tags": ""})

    data = request.get_json(silent=True)
    if not data:
        return jsonify({"code": 400, "msg": "请求格式错误，需要 JSON 格式请求体"})
    
    try:
        theme, duration, style = _validate_generate_params(data)
    except ValueError as e:
        return jsonify({"code": 400, "msg": str(e)})

    # ── 多轮会话参数 ──────────────────────────────────────────────────────────
    history  = data.get("history",  []) or []
    feedback = (data.get("feedback", "") or "").strip()

    # 判断轮次：有历史记录且有反馈内容 → 后续轮次；否则 → 首轮
    if history and feedback:
        # 后续轮次：基于历史 + 用户反馈，无需重发完整 prompt
        new_user_text = (
            f"请根据以下反馈修改脚本：\n{feedback}\n\n"
            f"要求：保持与之前相同的 JSON 格式，只返回修改后的完整 JSON 对象，不要有任何其他文字。"
        )
        messages = _build_messages(history, new_user_text)
        print(f"[MULTI-TURN] 第 {len(history)//2 + 1} 轮 | feedback='{feedback[:30]}...' "
              f"| history_turns={len(history)//2}")
    else:
        # 首轮：使用完整 prompt（含写作技巧、示例等）
        prompt   = _build_prompt(theme, duration, style)
        messages = [{"role": "user", "content": prompt}]
        print(f"[FIRST-TURN] theme='{theme}' | duration={duration} | style={style}")
    def event_stream():
        full_response = ""  # 累积完整响应，随 done 事件一并返回给前端
        token_usage   = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
        try:
            stream = ark_client.chat.completions.create(
                model="doubao-seed-2-0-pro-260215",
                messages=messages,
                max_tokens=1024,
                stream=True,
            )
            for chunk in stream:
                if not chunk.choices:
                    # 最后一个 chunk 通常携带 usage（需开启 stream_options）
                    usage_chunk = _extract_token_usage(chunk)
                    if usage_chunk["total_tokens"]:
                        token_usage = usage_chunk
                    continue

                delta_content = chunk.choices[0].delta.content or ""
                finish_reason = chunk.choices[0].finish_reason

                if delta_content:
                    full_response += delta_content
                    yield f"data: {json.dumps({'text': delta_content}, ensure_ascii=False)}\n\n"

                if finish_reason == "stop":
                    # 部分 SDK 版本将 usage 附在最后一个有效 chunk 上
                    usage_chunk = _extract_token_usage(chunk)
                    if usage_chunk["total_tokens"]:
                        token_usage = usage_chunk

            # 流结束后统一记录用量并发送 done 事件
            if token_usage["total_tokens"]:
                print(f"[TOKEN_USAGE] input: {token_usage['input_tokens']}, "
                      f"output: {token_usage['output_tokens']}, total: {token_usage['total_tokens']}")
                cost   = log_token_usage(theme, duration, style, token_usage)
                alerts = check_and_alert(token_usage, cost, theme)
                print(f"[SUCCESS] 流式生成成功 | theme='{theme}' | "
                      f"tokens: {token_usage['total_tokens']} | cost: ¥{cost:.4f}")
                if alerts:
                    print(f"[WARNING] 触发 {len(alerts)} 条告警")
            else:
                print(f"[SUCCESS] 流式生成成功 | theme='{theme}'")

            # done 事件携带 full_text，前端存入历史后传回下一轮
            yield f"data: {json.dumps({'done': True, 'full_text': full_response}, ensure_ascii=False)}\n\n"

        except Exception as e:
            print(f"[ERROR] 流式生成失败: {e}")
            yield f"data: {json.dumps({'error': str(e)}, ensure_ascii=False)}\n\n"
            yield f"data: {json.dumps({'done': True, 'full_text': ''}, ensure_ascii=False)}\n\n"

    return Response(stream_with_context(event_stream()), mimetype="text/event-stream")
