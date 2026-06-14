from __future__ import annotations

from typing import Any


def extract_position_qty(positions: list[dict]) -> float:
    pos_qty = 0.0
    for row in positions:
        if not isinstance(row, dict):
            continue

        contracts = row.get("contracts", row.get("positionAmt", 0))
        if not contracts:
            contracts = row.get("contractSize", 0)
        if not contracts:
            inner_pos = row.get("position", {})
            if isinstance(inner_pos, dict):
                contracts = inner_pos.get("szi", inner_pos.get("positionAmt", 0))

        try:
            pos_qty += abs(float(contracts or 0))
        except (TypeError, ValueError):
            continue

    return pos_qty


def fetch_account_snapshot(ex, symbol: str) -> dict[str, Any]:
    balance = ex.fetch_balance()
    positions = ex.fetch_positions(symbol)
    info = balance.get("info", {}) if isinstance(balance, dict) else {}
    margin_summary = info.get("marginSummary", {}) if isinstance(info, dict) else {}
    cross_margin_summary = info.get("crossMarginSummary", {}) if isinstance(info, dict) else {}

    return {
        "cash_usdc": float((balance.get("USDC", {}) or {}).get("free", 0) or 0),
        "position_value": float(margin_summary.get("totalNtlPos", 0) or 0),
        "net_value": float(margin_summary.get("accountValue", 0) or 0),
        "margin_used": float(margin_summary.get("totalMarginUsed", 0) or 0),
        "position_qty": extract_position_qty(positions),
        "positions": positions,
        "user_state": info,
        "cross_margin_summary": cross_margin_summary,
        "balance_datetime": balance.get("datetime", ""),
    }
