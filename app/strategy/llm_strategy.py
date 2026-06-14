from __future__ import annotations

import json
import re
from typing import Any

def build_prompt(
    symbol: str,
    ohlcv: list[list[float]],
    template: str | None = None,
    kline_count: int = 20,
    account_snapshot: dict[str, Any] | None = None,
) -> str:
    window_size = max(1, int(kline_count))
    last = ohlcv[-window_size:]
    lines = ["timestamp,open,high,low,close,volume"]
    for row in last:
        lines.append(",".join([str(int(row[0]))] + [str(x) for x in row[1:]]))

    ohlcv_csv = "\n".join(lines)
    account_snapshot_json = json.dumps(account_snapshot or {}, ensure_ascii=False)
    prompt_template = template or (
        "You are a trading assistant. Return ONLY a pure JSON object. "
        "NO markdown formatting, NO explanations. Fields must be: "
        "action (buy/sell/hold), position_pct (0-1), confidence (0-1), reason (string). "
        "position_pct means proportion of available capital to use for buy, "
        "or proportion of current position to sell. "
        "Symbol: {symbol}. Recent OHLCV:\n{ohlcv_csv}\n"
        "Current account snapshot JSON:\n{account_snapshot_json}"
    )

    values = {
        "symbol": symbol,
        "ohlcv_csv": ohlcv_csv,
        "account_snapshot_json": account_snapshot_json,
    }

    class _SafeDict(dict):
        def __missing__(self, key: str) -> str:
            return "{" + key + "}"

    rendered = prompt_template.format_map(_SafeDict(values))
    if "{account_snapshot_json}" not in prompt_template:
        rendered += f"\n\nCurrent account snapshot JSON:\n{account_snapshot_json}"
    return rendered

def parse_decision(text: str) -> dict[str, Any]:
    # 1. 尝试使用正则提取花括号内的 JSON 内容
    match = re.search(r'\{.*\}', text, re.DOTALL)
    if match:
        clean_text = match.group(0)
    else:
        clean_text = text

    try:
        obj = json.loads(clean_text)
        raw_position_pct = obj.get("position_pct", obj.get("position_ratio", obj.get("amount", 0.0)))
        position_pct = float(raw_position_pct or 0.0)
        if position_pct < 0:
            position_pct = 0.0
        if position_pct > 1:
            position_pct = 1.0
        return {
            "action": obj.get("action", "hold"),
            "position_pct": position_pct,
            "confidence": float(obj.get("confidence", 0.0)),
            "reason": str(obj.get("reason", "")),
        }
    except json.JSONDecodeError as e:
        # 2. 遇到解析失败时，返回安全的默认策略，保证系统不中断
        print(f"JSON 解析失败。模型原始返回: {text}\n错误信息: {e}")
        return {
            "action": "hold",
            "position_pct": 0.0,
            "confidence": 0.0,
            "reason": "解析异常，执行默认安全策略",
        }
