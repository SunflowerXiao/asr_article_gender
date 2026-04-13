import csv
import os
import time

from config import (
    TOKEN_LOG_FILE,
    ALERT_LOG_FILE,
    SINGLE_CALL_TOKEN_LIMIT,
    DAILY_TOKEN_LIMIT,
    INPUT_TOKEN_PRICE,
    OUTPUT_TOKEN_PRICE,
    DAILY_COST_LIMIT,
)


def log_token_usage(theme: str, duration: str, style: str, token_usage: dict) -> float:
    """将本次调用的 token 消耗追加写入 CSV 日志，返回本次费用（元）"""
    today     = time.strftime("%Y-%m-%d")
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")

    cost = (
        token_usage["input_tokens"]  / 1000 * INPUT_TOKEN_PRICE
        + token_usage["output_tokens"] / 1000 * OUTPUT_TOKEN_PRICE
    )

    file_exists = os.path.exists(TOKEN_LOG_FILE)
    with open(TOKEN_LOG_FILE, "a", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow([
                "timestamp", "date", "theme", "duration", "style",
                "input_tokens", "output_tokens", "total_tokens", "cost_yuan",
            ])
        writer.writerow([
            timestamp, today, theme, duration, style,
            token_usage["input_tokens"],
            token_usage["output_tokens"],
            token_usage["total_tokens"],
            f"{cost:.4f}",
        ])

    return cost


def get_daily_token_usage() -> dict:
    """读取今日累计 token 消耗，返回统计字典"""
    today = time.strftime("%Y-%m-%d")
    total_input = total_output = 0
    total_cost  = 0.0

    if not os.path.exists(TOKEN_LOG_FILE):
        return {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "cost": 0.0}

    with open(TOKEN_LOG_FILE, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row.get("date") == today:
                total_input  += int(row.get("input_tokens",  0))
                total_output += int(row.get("output_tokens", 0))
                total_cost   += float(row.get("cost_yuan",   0))

    return {
        "input_tokens":  total_input,
        "output_tokens": total_output,
        "total_tokens":  total_input + total_output,
        "cost":          total_cost,
    }


def check_and_alert(token_usage: dict, cost: float, theme: str) -> list:
    """检查是否超阈值，超出则写告警日志并打印，返回告警列表"""
    alerts    = []
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")

    # 1. 单次 token 上限
    if token_usage["total_tokens"] > SINGLE_CALL_TOKEN_LIMIT:
        alerts.append(
            f"[{timestamp}] ⚠️ 单次调用 token 超限: "
            f"total={token_usage['total_tokens']} > limit={SINGLE_CALL_TOKEN_LIMIT}, "
            f"theme='{theme}'"
        )

    daily_usage = get_daily_token_usage()

    # 2. 每日累计 token 上限
    if daily_usage["total_tokens"] > DAILY_TOKEN_LIMIT:
        alerts.append(
            f"[{timestamp}] 🚨 今日累计 token 超限: "
            f"total={daily_usage['total_tokens']} > limit={DAILY_TOKEN_LIMIT}"
        )

    # 3. 每日费用上限
    if daily_usage["cost"] > DAILY_COST_LIMIT:
        alerts.append(
            f"[{timestamp}] 💰 今日费用超限: "
            f"cost={daily_usage['cost']:.2f}元 > limit={DAILY_COST_LIMIT}元"
        )

    if alerts:
        with open(ALERT_LOG_FILE, "a", encoding="utf-8") as f:
            for alert in alerts:
                f.write(alert + "\n")
        for alert in alerts:
            print(f"[ALERT] {alert}")

    return alerts
