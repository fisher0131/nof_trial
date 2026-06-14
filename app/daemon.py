from __future__ import annotations

import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

# 确保项目根目录在 sys.path 中，支持直接运行 `python -m app.daemon`
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.config import load_config
from app.exchange.hyperliquid_client import HyperliquidClient
from app.ipc import DEFAULT_IPC_ADDRESS, DaemonSharedState, IpcServer
from app.llm import create_llm_client
from app.strategy.llm_strategy import build_prompt, parse_decision
from app.utils.io import load_json_file, save_json_file
from app.utils.logger import setup_logger
from app.utils.snapshot import extract_position_qty, fetch_account_snapshot

GMT8 = timezone(timedelta(hours=8))
LIVE_RUNS_FILE = _ROOT / "live_runs.jsonl"
DAEMON_CONTROL_FILE = _ROOT / "daemon_control.json"
DAEMON_STATUS_FILE = _ROOT / "daemon_status.json"
DEFAULT_INTERVAL_SECONDS = 300
IDLE_SLEEP_SECONDS = 3


def save_live_run(run_record: dict[str, object]) -> Path:
    LIVE_RUNS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LIVE_RUNS_FILE.open("a", encoding="utf-8") as file:
        file.write(json.dumps(run_record, ensure_ascii=False) + "\n")
    return LIVE_RUNS_FILE


def load_control() -> dict[str, object]:
    raw = load_json_file(
        DAEMON_CONTROL_FILE,
        {
            "enabled": False,
            "interval_sec": DEFAULT_INTERVAL_SECONDS,
            "session_id": "",
            "updated_at": "",
        },
    )
    if "enabled" not in raw and "trading_enabled" in raw:
        raw = {
            "enabled": bool(raw.get("trading_enabled", False)),
            "interval_sec": int(raw.get("interval_sec", DEFAULT_INTERVAL_SECONDS) or DEFAULT_INTERVAL_SECONDS),
            "session_id": str(raw.get("session_id", "") or ""),
            "updated_at": str(raw.get("updated_at", "") or ""),
        }
        save_json_file(DAEMON_CONTROL_FILE, raw)
    return raw


def write_status(
    *,
    state: str,
    enabled: bool,
    interval_sec: int,
    session_id: str,
    started_at: str,
    next_run_at: str,
    last_cycle_at: str,
    last_error: str,
    last_snapshot: dict[str, object] | None,
    last_record: dict[str, object] | None,
) -> None:
    payload = {
        "pid": os.getpid(),
        "state": state,
        "enabled": enabled,
        "interval_sec": int(interval_sec),
        "session_id": session_id,
        "started_at": started_at,
        "heartbeat_at": datetime.now(GMT8).isoformat(),
        "next_run_at": next_run_at,
        "last_cycle_at": last_cycle_at,
        "last_error": last_error,
        "last_snapshot": last_snapshot,
        "last_record": last_record,
    }
    save_json_file(DAEMON_STATUS_FILE, payload)


def run_live_trade_step(
    ex: HyperliquidClient,
    llm_client: object,
    cfg,
) -> dict[str, object]:
    cycle_started_at = datetime.now(GMT8)
    cycle_started_ts = time.time()

    before_snapshot = fetch_account_snapshot(ex, cfg.exchange.symbol)

    ohlcv = ex.fetch_ohlcv(cfg.exchange.symbol, cfg.exchange.timeframe, cfg.exchange.max_candles)
    if not ohlcv:
        raise RuntimeError("未获取到行情数据")

    price = float(ohlcv[-1][4])
    prompt = build_prompt(
        cfg.exchange.symbol,
        ohlcv,
        cfg.prompt.template,
        cfg.prompt.kline_count,
        account_snapshot=before_snapshot,
    )
    raw = llm_client.decide(prompt)
    decision = parse_decision(raw)

    action = str(decision.get("action", "hold")).lower()
    model_position_pct = max(0.0, min(1.0, float(decision.get("position_pct", 0.0) or 0.0)))
    reason = str(decision.get("reason", ""))

    available_cash = float(before_snapshot["cash_usdc"])
    pos_qty = float(before_snapshot["position_qty"])

    executed = "none"
    amount = 0.0
    notional = 0.0
    order_result = None

    if action == "buy":
        target_amount = (available_cash / price) * model_position_pct if price > 0 else 0.0
        target_notional = target_amount * price
        if target_notional >= cfg.trading.min_trade_notional and target_amount > 0:
            amount = target_amount
            notional = target_notional
            order_result = ex.create_market_order(cfg.exchange.symbol, "buy", amount)
            executed = "buy"
    elif action == "sell" and pos_qty > 0:
        target_amount = min(pos_qty, pos_qty * model_position_pct)
        target_notional = target_amount * price
        if target_notional >= cfg.trading.min_trade_notional and target_amount > 0:
            amount = target_amount
            notional = target_notional
            order_result = ex.create_market_order(cfg.exchange.symbol, "sell", amount)
            executed = "sell"

    after_snapshot = fetch_account_snapshot(ex, cfg.exchange.symbol)
    cycle_ended_at = datetime.now(GMT8)

    before_net_value = float(before_snapshot.get("net_value", 0.0) or 0.0)
    after_net_value = float(after_snapshot.get("net_value", 0.0) or 0.0)
    pnl_usdc = after_net_value - before_net_value
    pnl_pct = (pnl_usdc / before_net_value) if before_net_value > 0 else 0.0

    return {
        "time": cycle_ended_at.isoformat(),
        "cycle_started_at": cycle_started_at.isoformat(),
        "cycle_ended_at": cycle_ended_at.isoformat(),
        "duration_sec": round(time.time() - cycle_started_ts, 4),
        "status": "success",
        "price": price,
        "action": action,
        "decision": decision,
        "reason": reason,
        "model_position_pct": model_position_pct,
        "confidence": float(decision.get("confidence", 0.0)),
        "executed": executed,
        "amount": amount,
        "notional": notional,
        "before_snapshot": before_snapshot,
        "snapshot": after_snapshot,
        "net_value": after_net_value,
        "pnl_usdc": pnl_usdc,
        "pnl_pct": pnl_pct,
        "prompt": prompt,
        "raw_output": raw,
        "order": order_result,
    }


def main() -> None:
    initial_cfg = load_config()
    logger = setup_logger(initial_cfg.app.log_level)

    daemon_started_at = datetime.now(GMT8).isoformat()
    exchange: HyperliquidClient | None = None
    llm_client = None
    runtime_signature = ""

    last_cycle_at = ""
    next_run_at = ""
    last_error = ""
    last_snapshot: dict[str, object] | None = None
    last_record: dict[str, object] | None = None
    prev_enabled = False
    prev_session_id = ""

    ipc_state = DaemonSharedState()
    ipc_state.update(
        pid=os.getpid(),
        started_at=daemon_started_at,
        interval_sec=DEFAULT_INTERVAL_SECONDS,
    )
    ipc_server = IpcServer(state=ipc_state)
    ipc_server.start()
    logger.info("IPC server running on %s:%d", *DEFAULT_IPC_ADDRESS)

    def sync_state(*, state: str, enabled: bool, interval_sec: int, session_id: str,
                    next_run_at: str = "", last_cycle_at: str = "", last_error: str = "",
                    last_snapshot: dict[str, object] | None = None,
                    last_record: dict[str, object] | None = None) -> None:
        payload = {
            "pid": os.getpid(),
            "state": state,
            "enabled": enabled,
            "interval_sec": int(interval_sec),
            "session_id": session_id,
            "started_at": daemon_started_at,
            "heartbeat_at": datetime.now(GMT8).isoformat(),
            "next_run_at": next_run_at,
            "last_cycle_at": last_cycle_at,
            "last_error": last_error,
            "last_snapshot": last_snapshot,
            "last_record": last_record,
        }
        ipc_state.update(**payload)
        save_json_file(DAEMON_STATUS_FILE, payload)

    logger.info("daemon 启动，等待控制指令 (IPC: %s:%d, file: %s)", *DEFAULT_IPC_ADDRESS, DAEMON_CONTROL_FILE)

    while True:
        control = load_control()
        enabled = bool(control.get("enabled", False))
        interval_sec = int(control.get("interval_sec", DEFAULT_INTERVAL_SECONDS) or DEFAULT_INTERVAL_SECONDS)
        interval_sec = max(5, interval_sec)
        session_id = str(control.get("session_id", "") or "")

        ipc_cmd = ipc_state.get_and_clear_command()
        if isinstance(ipc_cmd, dict):
            ipc_action = str(ipc_cmd.get("action", ""))
            if ipc_action == "start":
                enabled = True
                interval_sec = max(5, int(ipc_cmd.get("interval_sec", interval_sec) or interval_sec))
                session_id = str(ipc_cmd.get("session_id", session_id) or session_id)
            elif ipc_action == "stop":
                enabled = False

        if enabled and (not prev_enabled or session_id != prev_session_id):
            last_cycle_at = ""
            next_run_at = ""
            last_error = ""
            last_snapshot = None
            last_record = None
            sync_state(
                state="starting",
                enabled=True,
                interval_sec=interval_sec,
                session_id=session_id,
            )

        if (not enabled) and prev_enabled:
            last_cycle_at = ""
            next_run_at = ""
            last_error = ""
            last_snapshot = None
            last_record = None

        if not enabled:
            sync_state(
                state="idle",
                enabled=False,
                interval_sec=interval_sec,
                session_id=session_id,
                last_cycle_at=last_cycle_at,
                last_error=last_error,
                last_snapshot=last_snapshot,
                last_record=last_record,
            )
            prev_enabled = False
            prev_session_id = session_id
            time.sleep(IDLE_SLEEP_SECONDS)
            continue

        started_ts = time.time()
        cycle_started_at = datetime.now(GMT8).isoformat()

        sync_state(
            state="running (LLM思考中...)",
            enabled=True,
            interval_sec=interval_sec,
            session_id=session_id,
            next_run_at="calculating...",
            last_cycle_at=last_cycle_at,
            last_error=last_error,
            last_snapshot=last_snapshot,
            last_record=last_record,
        )

        try:
            cfg = load_config()
            new_signature = "|".join(
                [
                    str(cfg.exchange.api_key),
                    str(cfg.exchange.secret),
                    str(cfg.exchange.sandbox),
                    str(cfg.exchange.base_url),
                    str(cfg.exchange.account_address),
                    str(cfg.exchange.wallet_address),
                    str(cfg.exchange.market_order_slippage),
                    str(cfg.openai.provider),
                    str(cfg.openai.base_url),
                    str(cfg.openai.model),
                    str(cfg.openai.api_key),
                    str(cfg.openai.temperature),
                ]
            )

            if exchange is None or llm_client is None or runtime_signature != new_signature:
                exchange = HyperliquidClient(
                    api_key=cfg.exchange.api_key,
                    secret=cfg.exchange.secret,
                    sandbox=cfg.exchange.sandbox,
                    base_url=cfg.exchange.base_url,
                    account_address=cfg.exchange.account_address,
                    wallet_address=cfg.exchange.wallet_address,
                    market_order_slippage=cfg.exchange.market_order_slippage,
                )
                llm_client = create_llm_client(cfg.openai)
                exchange.connect()
                runtime_signature = new_signature
                logger.info("runtime 已刷新并连接交易所")

            record = run_live_trade_step(exchange, llm_client, cfg)
            record["session_id"] = session_id
            save_live_run(record)

            last_record = record
            last_snapshot = record.get("snapshot") if isinstance(record.get("snapshot"), dict) else last_snapshot
            last_cycle_at = str(record.get("cycle_ended_at", "") or cycle_started_at)
            next_run_at = datetime.now(GMT8).isoformat()
            last_error = ""

            logger.info(
                "cycle done | net=%.4f | action=%s | executed=%s | reason=%s",
                float(record.get("net_value", 0.0) or 0.0),
                str(record.get("action", "hold")),
                str(record.get("executed", "none")),
                str(record.get("reason", "")),
            )
        except Exception as exc:
            err_msg = str(exc)
            error_record = {
                "time": datetime.now(GMT8).isoformat(),
                "cycle_started_at": cycle_started_at,
                "cycle_ended_at": datetime.now(GMT8).isoformat(),
                "duration_sec": round(time.time() - started_ts, 4),
                "status": "error",
                "executed": "error",
                "error": err_msg,
                "session_id": session_id,
            }
            save_live_run(error_record)

            last_record = error_record
            last_error = err_msg
            last_cycle_at = str(error_record.get("cycle_ended_at", "") or datetime.now(GMT8).isoformat())
            logger.exception("cycle error: %s", exc)

            exchange = None
            llm_client = None
            runtime_signature = ""

        elapsed = time.time() - started_ts
        sleep_for = max(0, interval_sec - elapsed)
        next_run_at = (datetime.now(GMT8) + timedelta(seconds=sleep_for)).isoformat()
        sync_state(
            state="running",
            enabled=True,
            interval_sec=interval_sec,
            session_id=session_id,
            next_run_at=next_run_at,
            last_cycle_at=last_cycle_at,
            last_error=last_error,
            last_snapshot=last_snapshot,
            last_record=last_record,
        )
        prev_enabled = True
        prev_session_id = session_id
        time.sleep(sleep_for)


if __name__ == "__main__":
    main()
