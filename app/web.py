from __future__ import annotations

import json
import sys
import time
import traceback
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# Ensure project root is importable no matter where streamlit starts from.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from app.config import LLMApiOption, OpenAIConfig, load_config, save_config
from app.daemon_runtime import (
    DEFAULT_DAEMON_INTERVAL_SEC,
    is_daemon_alive,
    load_daemon_status,
    parse_iso_datetime,
    start_daemon_process,
)
from app.exchange.hyperliquid_client import HYPERLIQUID_TESTNET_API_URL, HyperliquidClient
from app.ipc import DEFAULT_IPC_ADDRESS, ipc_request
from app.llm import create_llm_client
from app.strategy.llm_strategy import build_prompt, parse_decision
from app.utils.io import load_json_file, save_json_file
from app.utils.logger import setup_logger
from app.utils.snapshot import extract_position_qty, fetch_account_snapshot

GMT8 = timezone(timedelta(hours=8))
BACKTEST_RUNS_FILE = _ROOT / "backtest_runs.jsonl"
LIVE_RUNS_FILE = _ROOT / "live_runs.jsonl"
DAEMON_CONTROL_FILE = _ROOT / "daemon_control.json"
DAEMON_STATUS_FILE = _ROOT / "daemon_status.json"
HYPERLIQUID_MAINNET_API_URL = "https://api.hyperliquid.xyz"


def ms_to_gmt8(ts_ms: int | float) -> pd.Timestamp:
    return pd.to_datetime(ts_ms, unit="ms", utc=True).tz_convert(GMT8)


def file_signature(path: Path) -> tuple[bool, int, int]:
    if not path.exists():
        return (False, 0, 0)
    stat = path.stat()
    return (True, int(stat.st_size), int(stat.st_mtime_ns))


def load_recent_jsonl(path: Path, limit: int) -> list[dict[str, object]]:
    if not path.exists():
        return []

    rows: list[dict[str, object]] = []
    lines = path.read_text(encoding="utf-8").splitlines()
    for line in lines[-max(1, int(limit)) :]:
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            rows.append(parsed)
    return list(reversed(rows))


@st.cache_data(ttl=30)
def load_recent_backtest_runs_cached(
    limit: int,
    signature: tuple[bool, int, int],
) -> list[dict[str, object]]:
    del signature
    return load_recent_jsonl(BACKTEST_RUNS_FILE, limit)


def load_recent_backtest_runs(limit: int = 20) -> list[dict[str, object]]:
    return load_recent_backtest_runs_cached(limit, file_signature(BACKTEST_RUNS_FILE))


@st.cache_data(ttl=5)
def load_recent_live_runs_cached(
    limit: int,
    signature: tuple[bool, int, int],
) -> list[dict[str, object]]:
    del signature
    return load_recent_jsonl(LIVE_RUNS_FILE, limit)


def load_recent_live_runs(limit: int = 100) -> list[dict[str, object]]:
    return load_recent_live_runs_cached(limit, file_signature(LIVE_RUNS_FILE))


def save_backtest_run(run_record: dict[str, object]) -> Path:
    BACKTEST_RUNS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with BACKTEST_RUNS_FILE.open("a", encoding="utf-8") as file:
        file.write(json.dumps(run_record, ensure_ascii=False) + "\n")
    load_recent_backtest_runs_cached.clear()
    return BACKTEST_RUNS_FILE


def load_daemon_control() -> dict[str, object]:
    raw = load_json_file(
        DAEMON_CONTROL_FILE,
        {
            "enabled": False,
            "interval_sec": DEFAULT_DAEMON_INTERVAL_SEC,
            "session_id": "",
            "updated_at": "",
        },
    )
    if "enabled" not in raw and "trading_enabled" in raw:
        raw = {
            "enabled": bool(raw.get("trading_enabled", False)),
            "interval_sec": int(raw.get("interval_sec", DEFAULT_DAEMON_INTERVAL_SEC) or DEFAULT_DAEMON_INTERVAL_SEC),
            "session_id": str(raw.get("session_id", "") or ""),
            "updated_at": str(raw.get("updated_at", "") or ""),
        }
        save_json_file(DAEMON_CONTROL_FILE, raw)
    return raw


def save_daemon_control(payload: dict[str, object]) -> None:
    save_json_file(DAEMON_CONTROL_FILE, payload)


def reset_daemon_status(
    *,
    state: str,
    enabled: bool,
    interval_sec: int,
    session_id: str,
    pid: int = 0,
) -> None:
    save_json_file(
        DAEMON_STATUS_FILE,
        {
            "pid": int(pid),
            "state": state,
            "enabled": bool(enabled),
            "interval_sec": int(interval_sec),
            "session_id": session_id,
            "started_at": "",
            "heartbeat_at": datetime.now(GMT8).isoformat(),
            "next_run_at": "",
            "last_cycle_at": "",
            "last_error": "",
            "last_snapshot": None,
            "last_record": None,
        },
    )


def ensure_daemon_process_for_web() -> tuple[bool, str]:
    status = load_daemon_status()
    if is_daemon_alive(status):
        return True, ""
    return start_daemon_process()


@st.cache_data(ttl=5)
def build_live_session_views(
    logs: list[dict[str, object]],
    active_session_id: str,
    active_enabled: bool,
) -> list[dict[str, object]]:
    grouped: dict[str, list[dict[str, object]]] = {}
    for row in logs:
        sid = str(row.get("session_id", "") or "unknown")
        grouped.setdefault(sid, []).append(row)

    def to_dt(value: str) -> pd.Timestamp:
        return pd.to_datetime(value, errors="coerce")

    views: list[dict[str, object]] = []
    for sid, rows in grouped.items():
        rows_sorted = sorted(
            rows,
            key=lambda item: to_dt(str(item.get("cycle_started_at", "") or item.get("time", ""))),
        )

        first_row = rows_sorted[0]
        last_row = rows_sorted[-1]
        started_at = str(first_row.get("cycle_started_at", "") or first_row.get("time", ""))
        ended_at = str(last_row.get("cycle_ended_at", "") or last_row.get("time", ""))

        first_before = first_row.get("before_snapshot")
        first_after = first_row.get("snapshot")
        last_before = last_row.get("before_snapshot")
        last_after = last_row.get("snapshot")

        start_snapshot = (
            first_before if isinstance(first_before, dict) else first_after if isinstance(first_after, dict) else None
        )
        end_snapshot = (
            last_after if isinstance(last_after, dict) else last_before if isinstance(last_before, dict) else None
        )

        start_net = float(((start_snapshot or {}).get("net_value", 0.0)) or 0.0)
        end_net = float(((end_snapshot or {}).get("net_value", 0.0)) or 0.0)
        pnl_usdc = end_net - start_net
        pnl_pct = (pnl_usdc / start_net) if start_net > 0 else 0.0
        has_error = any(str(item.get("status", "success")) == "error" for item in rows_sorted)

        if active_enabled and sid == active_session_id:
            status = "running"
        elif has_error:
            status = "error"
        else:
            status = "ended"

        views.append(
            {
                "session_id": sid,
                "summary": {
                    "session_id": sid,
                    "status": status,
                    "started_at": started_at,
                    "ended_at": ended_at,
                    "duration_sec": max(0.0, (pd.to_datetime(ended_at) - pd.to_datetime(started_at)).total_seconds()),
                    "start_net_value": start_net,
                    "end_net_value": end_net,
                    "pnl_usdc": pnl_usdc,
                    "pnl_pct": pnl_pct,
                    "cycles_total": len(rows_sorted),
                    "cycles_success": sum(1 for item in rows_sorted if str(item.get("status", "success")) == "success"),
                    "buy_count": sum(1 for item in rows_sorted if str(item.get("executed", "none")) == "buy"),
                    "sell_count": sum(1 for item in rows_sorted if str(item.get("executed", "none")) == "sell"),
                    "total_notional": sum(float(item.get("notional", 0.0) or 0.0) for item in rows_sorted),
                    "total_cycle_pnl_usdc": sum(float(item.get("pnl_usdc", 0.0) or 0.0) for item in rows_sorted),
                },
                "logs": rows_sorted,
            }
        )

    views.sort(
        key=lambda item: to_dt(str((item.get("summary") or {}).get("started_at", ""))),
        reverse=True,
    )
    return views


@st.cache_resource
def get_config():
    return load_config()


@st.cache_resource
def get_llm_client_cached(
    provider: str,
    base_url: str,
    api_key: str,
    model: str,
    temperature: float,
):
    return create_llm_client(
        OpenAIConfig(
            provider=provider,
            base_url=base_url,
            api_key=api_key,
            model=model,
            temperature=temperature,
        )
    )


def get_llm():
    return get_llm_client_cached(
        str(cfg.openai.provider),
        str(cfg.openai.base_url),
        str(cfg.openai.api_key),
        str(cfg.openai.model),
        float(cfg.openai.temperature),
    )


def get_exchange() -> HyperliquidClient:
    if st.session_state.exchange is None:
        st.session_state.exchange = HyperliquidClient(
            api_key=cfg.exchange.api_key,
            secret=cfg.exchange.secret,
            sandbox=cfg.exchange.sandbox,
            base_url=cfg.exchange.base_url,
            account_address=cfg.exchange.account_address,
            wallet_address=cfg.exchange.wallet_address,
            market_order_slippage=cfg.exchange.market_order_slippage,
        )
    return st.session_state.exchange


def current_market_source_label() -> str:
    if cfg.exchange.sandbox:
        return f"{HYPERLIQUID_MAINNET_API_URL}（测试网下单 + 主网行情）"
    return cfg.exchange.base_url or HYPERLIQUID_MAINNET_API_URL


def mark_connected() -> None:
    st.session_state.connected = True


def refresh_balance_snapshot() -> None:
    ex = get_exchange()
    snapshot = fetch_account_snapshot(ex, cfg.exchange.symbol)
    mark_connected()
    st.session_state.balance_snapshot = snapshot
    st.session_state.balance_error = None


def refresh_manual_order_snapshot() -> None:
    ex = get_exchange()
    manual_ohlcv = ex.fetch_ohlcv(cfg.exchange.symbol, cfg.exchange.timeframe, 2)
    balance = ex.fetch_balance()
    positions = ex.fetch_positions(cfg.exchange.symbol)
    mark_connected()
    st.session_state.manual_order_snapshot = {
        "price": float(manual_ohlcv[-1][4]) if manual_ohlcv else 0.0,
        "cash_usdc": float((balance.get("USDC", {}) or {}).get("free", 0) or 0),
        "position_qty": extract_position_qty(positions),
        "positions": positions,
        "updated_at": datetime.now(GMT8).isoformat(),
    }
    st.session_state.manual_order_error = None


def compact_backtest_logs(logs: list[dict[str, object]]) -> list[dict[str, object]]:
    compact_logs: list[dict[str, object]] = []
    for row in logs:
        compact_logs.append(
            {
                "idx": int(row.get("idx", 0) or 0),
                "time": str(row.get("time", "")),
                "price": float(row.get("price", 0.0) or 0.0),
                "action": str(row.get("action", "hold")),
                "model_position_pct": float(row.get("model_position_pct", 0.0) or 0.0),
                "confidence": float(row.get("confidence", 0.0) or 0.0),
                "executed": str(row.get("executed", "none")),
                "amount": float(row.get("amount", 0.0) or 0.0),
                "notional": float(row.get("notional", 0.0) or 0.0),
                "cash": float(row.get("cash", 0.0) or 0.0),
                "position_qty": float(row.get("position_qty", 0.0) or 0.0),
                "position_value": float(row.get("position_value", 0.0) or 0.0),
                "equity": float(row.get("equity", 0.0) or 0.0),
                "reason": str(row.get("reason", "")),
                "raw_output": str(row.get("raw_output", "")),
                "prompt_preview": str(row.get("prompt", ""))[:200],
            }
        )
    return compact_logs


def init_session_state() -> None:
    defaults: dict[str, Any] = {
        "exchange": None,
        "connected": False,
        "llm_decision": None,
        "ohlcv_chart": None,
        "chart_fetch_meta": None,
        "balance_snapshot": None,
        "balance_error": None,
        "manual_order_snapshot": None,
        "manual_order_error": None,
        "bt_llm_logs": None,
        "bt_summary": None,
        "bt_ohlcv": None,
        "prompt_template_editor": cfg.prompt.template,
        "prompt_kline_count_editor": int(cfg.prompt.kline_count),
        "llm_base_url_editor": str(cfg.openai.base_url),
        "llm_api_key_editor": str(cfg.openai.api_key),
        "llm_model_editor": str(cfg.openai.model),
        "llm_temperature_editor": float(cfg.openai.temperature),
        "llm_test_result": None,
        "llm_saved_api_selector": ([item.name for item in cfg.openai.saved_apis] or [""])[0],
        "llm_new_api_name": "",
        "rt_auto_refresh": True,
        "rt_auto_refresh_sec": 5,
        "rt_last_autorefresh_count": -1,
        "rt_daemon_control_snapshot": None,
        "rt_history_limit": 300,
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value


_CSS = """
<style>
    /* === Global === */
    .stApp {
        background: #0a0c10;
        color: #e6edf3;
    }
    header[data-testid="stHeader"] {
        background: transparent;
    }

    /* === Typography — force all text light === */
    body, p, span, div, label, .stMarkdown, .stText, .stCaption {
        color: #e6edf3;
    }

    /* === Sidebar === */
    [data-testid="stSidebar"] {
        background: #0d1117;
        border-right: 1px solid #36404a;
        min-width: 260px;
    }
    [data-testid="stSidebar"] * {
        color: #e6edf3;
    }
    [data-testid="stSidebar"] .stMetric {
        background: transparent;
        padding: 0;
        margin-bottom: 4px;
    }
    [data-testid="stSidebar"] .stMetric label {
        font-size: 11px;
        text-transform: uppercase;
        letter-spacing: 0.5px;
        color: #a0aab4 !important;
    }
    [data-testid="stSidebar"] .stMetric [data-testid="stMetricValue"] {
        font-size: 14px;
        color: #f0f6fc !important;
    }
    [data-testid="stSidebar"] button {
        border-radius: 6px;
        font-size: 13px;
        font-weight: 500;
        transition: all 0.15s ease;
    }
    [data-testid="stSidebar"] .stButton > button {
        border: 1px solid #586069;
        background: #161b22;
        color: #e6edf3;
    }
    [data-testid="stSidebar"] .stButton > button:hover {
        border-color: #58a6ff;
        color: #58a6ff;
    }

    /* === Main content area === */
    .main [data-testid="stVerticalBlock"] {
        gap: 0.5rem;
    }

    /* === Headings === */
    h1, h2, h3, h4, h5, h6 {
        font-weight: 500 !important;
        letter-spacing: -0.3px;
        color: #ffffff !important;
    }
    h2 {
        font-size: 1.35rem !important;
    }
    h3 {
        font-size: 1.1rem !important;
    }

    /* === Metric cards === */
    [data-testid="stMetric"] {
        background: #0d1117;
        border: 1px solid #36404a;
        border-radius: 8px;
        padding: 12px 16px;
    }
    [data-testid="stMetric"] label {
        font-size: 11px;
        text-transform: uppercase;
        letter-spacing: 0.6px;
        color: #a0aab4 !important;
    }
    [data-testid="stMetric"] [data-testid="stMetricValue"] {
        font-size: 1.1rem;
        font-weight: 600;
        color: #f0f6fc !important;
    }
    [data-testid="stMetric"] [data-testid="stMetricDelta"] {
        font-size: 0.85rem;
    }

    /* === Buttons === */
    .stButton > button {
        border-radius: 6px;
        font-size: 13px;
        font-weight: 500;
        border: 1px solid #586069;
        background: #161b22 !important;
        color: #e6edf3 !important;
        transition: all 0.15s ease;
    }
    .stButton > button:hover {
        border-color: #58a6ff !important;
        color: #ffffff !important;
    }
    .stButton > button:active {
        background: #1a2332 !important;
    }

    /* Primary button */
    .stButton > button[kind="primary"] {
        background: #2ea043 !important;
        border-color: #2ea043 !important;
        color: #fff !important;
    }
    .stButton > button[kind="primary"]:hover {
        background: #3fb950 !important;
        border-color: #3fb950 !important;
    }

    /* Disabled button */
    .stButton > button:disabled {
        background: #0d1117 !important;
        border-color: #30363d !important;
        color: #6e7681 !important;
    }

    /* === Inputs — backgrounds dark, text light === */
    .stTextInput > div > div > input,
    .stTextArea textarea,
    .stNumberInput > div > div > input,
    .stSelectbox > div > div,
    .stDateInput > div > div > div > div > input {
        background: #0d1117 !important;
        border: 1px solid #586069 !important;
        border-radius: 6px !important;
        color: #e6edf3 !important;
    }
    .stTextInput > div > div > input:focus,
    .stTextArea textarea:focus,
    .stNumberInput > div > div > input:focus {
        border-color: #58a6ff !important;
        box-shadow: 0 0 0 2px rgba(88,166,255,0.25) !important;
    }
    .stTextInput > div > div > input::placeholder,
    .stTextArea textarea::placeholder,
    .stNumberInput > div > div > input::placeholder {
        color: #6e7681 !important;
    }

    /* Input labels (the text above the input box) */
    .stTextInput label, .stTextArea label, .stNumberInput label,
    .stSelectbox label, .stDateInput label, .stCheckbox label,
    .stRadio label {
        color: #e6edf3 !important;
    }

    /* === Selectbox dropdown === */
    .stSelectbox [data-baseweb="select"] [data-baseweb="tag"] {
        background: #0d1117 !important;
        color: #e6edf3 !important;
    }
    .stSelectbox [data-baseweb="popover"] {
        background: #161b22 !important;
    }
    .stSelectbox [data-baseweb="popover"] [role="option"] {
        color: #e6edf3 !important;
        background: #161b22 !important;
    }
    .stSelectbox [data-baseweb="popover"] [role="option"]:hover {
        background: #21262d !important;
    }

    /* === Radio buttons === */
    .stRadio [role="radiogroup"] {
        gap: 8px;
    }
    .stRadio label {
        color: #e6edf3 !important;
    }

    /* === Checkbox === */
    .stCheckbox label {
        color: #e6edf3 !important;
    }

    /* === Date input === */
    .stDateInput label {
        color: #e6edf3 !important;
    }

    /* === Dataframe / Table — dark background, light text === */
    [data-testid="stTable"], .stDataFrame {
        font-size: 12px;
    }
    .stDataFrame table {
        border-collapse: collapse;
    }
    .stDataFrame th {
        background: #161b22 !important;
        color: #e6edf3 !important;
        border-color: #30363d !important;
    }
    .stDataFrame td {
        background: #0d1117 !important;
        color: #e6edf3 !important;
        border-color: #21262d !important;
    }
    [data-testid="stTable"] th {
        background: #161b22 !important;
        color: #e6edf3 !important;
    }
    [data-testid="stTable"] td {
        background: #0d1117 !important;
        color: #e6edf3 !important;
    }

    /* === Expanders === */
    .streamlit-expanderHeader {
        font-size: 13px;
        font-weight: 500;
        color: #b0b8c0;
        background: transparent;
        border: 1px solid #36404a;
        border-radius: 6px;
        padding: 8px 12px;
    }
    .streamlit-expanderHeader:hover {
        color: #e6edf3;
        border-color: #586069;
    }
    .streamlit-expanderContent {
        background: transparent;
    }

    /* === Dividers === */
    hr {
        border-color: #36404a;
        margin: 1rem 0;
    }

    /* === Tabs === */
    .stTabs [data-baseweb="tab-list"] {
        gap: 4px;
        border-bottom: 1px solid #36404a;
        background: transparent;
    }
    .stTabs [data-baseweb="tab"] {
        font-size: 13px;
        font-weight: 500;
        color: #a0aab4;
        background: transparent;
        border-radius: 6px 6px 0 0;
        padding: 8px 16px;
        margin-bottom: -1px;
    }
    .stTabs [data-baseweb="tab"]:hover {
        color: #e6edf3;
    }
    .stTabs [aria-selected="true"] {
        color: #58a6ff !important;
        border-bottom: 2px solid #58a6ff !important;
        background: transparent !important;
    }

    /* === Status colors === */
    .running-label {color: #3fb950; font-weight: 600;}
    .stopped-label {color: #a0aab4; font-weight: 600;}
    .error-label {color: #f85149; font-weight: 600;}
    .offline-label {color: #a0aab4; font-weight: 600;}

    /* === Cards === */
    .card {
        background: #0d1117;
        border: 1px solid #36404a;
        border-radius: 8px;
        padding: 16px;
        margin-bottom: 12px;
    }

    /* === Info / Success / Warning / Error alerts — dark version === */
    .stAlert {
        border-radius: 6px;
        font-size: 13px;
        color: #e6edf3 !important;
    }
    div[data-testid="stAlert"] {
        color: #e6edf3 !important;
    }

    /* === Code blocks === */
    .stCodeBlock, .stCodeBlock pre, .stCodeBlock code {
        background: #161b22 !important;
        color: #e6edf3 !important;
        border: 1px solid #30363d !important;
    }

    /* === JSON blocks === */
    .stJson {
        background: #0d1117 !important;
        color: #e6edf3 !important;
    }

    /* === Tooltips / captions === */
    .stCaption {
        font-size: 11px;
        color: #8b949e;
    }

    /* === Progress bar === */
    .stProgress > div > div {
        background-color: #21262d !important;
    }
    .stProgress > div > div > div {
        background-color: #58a6ff !important;
    }

    /* === Spinner === */
    .stSpinner > div {
        border-top-color: #58a6ff !important;
    }

    /* === Plotly chart container === */
    .js-plotly-plot {
        border-radius: 8px;
        overflow: hidden;
    }

    /* === Deep-level baseweb overrides for tooltips, menus, etc. === */
    [data-baseweb="popover"] {
        background: #161b22 !important;
    }
    [data-baseweb="popover"] * {
        color: #e6edf3 !important;
    }
    [data-baseweb="tooltip"] [data-baseweb="tooltip-body"] {
        background: #21262d !important;
        color: #e6edf3 !important;
    }
</style>
"""


def inject_css() -> None:
    st.markdown(_CSS, unsafe_allow_html=True)


def sidebar_metric(label: str, value: str, color: str = "") -> None:
    if color:
        st.markdown(
            f'<div style="margin-bottom:10px"><span style="font-size:10px;text-transform:uppercase;letter-spacing:0.5px;color:#a0aab4">{label}</span><br><span style="font-size:14px;font-weight:500;color:{color}">{value}</span></div>',
            unsafe_allow_html=True,
        )
    else:
        st.metric(label, value)


def metric_card_row(cols: list, items: list[tuple[str, str, str | None]]) -> None:
    """items: list of (label, value, delta_or_None)"""
    for col, (label, value, delta) in zip(cols, items):
        with col:
            st.metric(label, value, delta=delta)


# ── Page Config ──────────────────────────────────────────────

st.set_page_config(
    page_title="LLM 交易机器人",
    page_icon="",
    layout="wide",
)

cfg = get_config()
setup_logger(cfg.app.log_level)
init_session_state()
inject_css()
daemon_autostart_ok, daemon_autostart_message = ensure_daemon_process_for_web()

is_rt_auto_refresh = False
if bool(st.session_state.rt_auto_refresh) and hasattr(st, "autorefresh"):
    refresh_count = st.autorefresh(
        interval=max(1, int(st.session_state.rt_auto_refresh_sec)) * 1000,
        key="rt_daemon_autorefresh",
    )
    previous_count = int(st.session_state.rt_last_autorefresh_count)
    is_rt_auto_refresh = previous_count >= 0 and refresh_count > previous_count
    st.session_state.rt_last_autorefresh_count = refresh_count
else:
    st.session_state.rt_last_autorefresh_count = -1

# ── Sidebar ───────────────────────────────────────────────────

with st.sidebar:
    st.markdown(
        '<div style="font-size:18px;font-weight:600;letter-spacing:-0.5px;color:#ffffff;margin-bottom:8px">LLM 交易机器人</div>',
        unsafe_allow_html=True,
    )

    if daemon_autostart_message and not daemon_autostart_ok:
        st.warning(daemon_autostart_message)

    is_sandbox = cfg.exchange.sandbox
    mode_color = "#3fb950" if is_sandbox else "#f85149"
    mode_text = "测试网" if is_sandbox else "主网"
    st.markdown(
        f'<div style="margin-bottom:8px"><span style="font-size:10px;text-transform:uppercase;letter-spacing:0.5px;color:#a0aab4">模式</span><br><span style="font-size:14px;font-weight:600;color:{mode_color}">{mode_text}</span></div>',
        unsafe_allow_html=True,
    )
    if not is_sandbox:
        st.error("主网模式：使用真实资金")

    sidebar_metric("交易对", cfg.exchange.symbol)
    sidebar_metric("周期", cfg.exchange.timeframe)
    sidebar_metric("模型", cfg.openai.model)

    conn_status = "已连接" if st.session_state.connected else "未连接"
    conn_color = "#3fb950" if st.session_state.connected else "#a0aab4"
    sidebar_metric("交易所", conn_status, conn_color)

    if st.button("连接", key="sidebar_connect", width="stretch"):
        with st.spinner("连接中..."):
            try:
                ex = get_exchange()
                ex.connect()
                mark_connected()
                st.success("已连接")
                st.rerun()
            except Exception as exc:
                st.error(str(exc))

# ── Main Content ──────────────────────────────────────────────

st.markdown(
    '<div style="font-size:1.4rem;font-weight:600;letter-spacing:-0.5px;color:#ffffff;margin-bottom:4px">仪表盘</div>',
    unsafe_allow_html=True,
)

if not st.session_state.connected:
    st.info("交易所未连接，交易功能将在首次请求时自动连接")

tab_balance, tab_chart, tab_llm, tab_backtest, tab_prompt = st.tabs(
    ["账户", "图表", "交易", "回测", "设置"]
)

# ── Tab: Balance ──────────────────────────────────────────────

with tab_balance:
    st.subheader("账户余额")

    if st.button("刷新", key="btn_balance"):
        with st.spinner("获取中..."):
            try:
                refresh_balance_snapshot()
            except Exception as exc:
                st.session_state.balance_error = f"{exc}"

    snapshot = st.session_state.balance_snapshot
    if isinstance(snapshot, dict):
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("现金 (USDC)", f"{float(snapshot.get('cash_usdc', 0.0) or 0.0):.4f}")
        c2.metric("仓位价值", f"{float(snapshot.get('position_value', 0.0) or 0.0):.4f}")
        c3.metric("净值", f"{float(snapshot.get('net_value', 0.0) or 0.0):.4f}")
        c4.metric("已用保证金", f"{float(snapshot.get('margin_used', 0.0) or 0.0):.4f}")
        st.caption(f"更新时间：{snapshot.get('balance_datetime', '')}")

        st.divider()
        col1, col2 = st.columns(2)
        with col1:
            st.caption("全仓保证金摘要")
            st.json({k: v for k, v in dict(snapshot.get("cross_margin_summary", {})).items()})
        with col2:
            st.caption("持仓")
            positions = snapshot.get("positions", [])
            if positions:
                st.dataframe(pd.json_normalize(positions), width="stretch")
            else:
                st.info("暂无持仓")

        with st.expander("原始数据", expanded=False):
            st.json(snapshot.get("user_state", {}))
    elif st.session_state.balance_error:
        st.error(f"获取失败：{st.session_state.balance_error}")
    else:
        st.info("点击刷新加载账户数据")

# ── Tab: Chart ────────────────────────────────────────────────

with tab_chart:
    st.subheader(f"{cfg.exchange.symbol} 图表")

    ctrl1, ctrl2, ctrl3 = st.columns([2, 2, 2])
    with ctrl1:
        timeframes = ["1m", "5m", "15m", "1h", "4h", "1d"]
        tf = st.selectbox("周期", timeframes, index=timeframes.index(cfg.exchange.timeframe))
    with ctrl2:
        n_candles = st.number_input("K线数", min_value=20, max_value=500, value=cfg.exchange.max_candles, step=10)
    with ctrl3:
        st.write("")
        st.write("")
        fetch_btn = st.button("拉取", width="stretch", key="btn_chart")

    if fetch_btn:
        with st.spinner("获取中..."):
            try:
                ex = get_exchange()
                ohlcv = ex.fetch_ohlcv(cfg.exchange.symbol, tf, int(n_candles))
                mark_connected()
                st.session_state.ohlcv_chart = ohlcv
                st.session_state.chart_fetch_meta = {
                    "timeframe": tf,
                    "candles": int(n_candles),
                    "source": current_market_source_label(),
                }
                if not ohlcv:
                    st.warning("无数据返回")
                else:
                    st.success(f"已加载 {len(ohlcv)} 根 K 线")
            except Exception as exc:
                st.error(f"拉取失败：{exc}")
                st.code(traceback.format_exc())

    with st.expander("调试", expanded=False):
        meta = st.session_state.chart_fetch_meta or {}
        st.write(f"来源：`{meta.get('source', current_market_source_label())}`")
        if st.session_state.ohlcv_chart is not None:
            st.write(f"长度：`{len(st.session_state.ohlcv_chart)}`")
            if st.session_state.ohlcv_chart:
                st.write(f"首条：`{st.session_state.ohlcv_chart[0]}`")
                st.write(f"末条：`{st.session_state.ohlcv_chart[-1]}`")

    if st.session_state.ohlcv_chart:
        try:
            ohlcv = st.session_state.ohlcv_chart
            df = pd.DataFrame(ohlcv, columns=["ts", "open", "high", "low", "close", "volume"])
            df["time"] = pd.to_datetime(df["ts"], unit="ms", utc=True).dt.tz_convert(GMT8)

            fig = go.Figure()
            fig.add_trace(
                go.Candlestick(
                    x=df["time"], open=df["open"], high=df["high"],
                    low=df["low"], close=df["close"], name="K线",
                    increasing_line_color="#3fb950", decreasing_line_color="#f85149",
                )
            )
            fig.add_trace(
                go.Bar(
                    x=df["time"], y=df["volume"], name="成交量",
                    marker_color="rgba(48,54,61,0.6)", yaxis="y2",
                )
            )
            fig.update_layout(
                xaxis_rangeslider_visible=False,
                yaxis=dict(title="价格", gridcolor="#21262d", zerolinecolor="#21262d"),
                yaxis2=dict(overlaying="y", side="right", showgrid=False, title="成交量"),
                legend=dict(orientation="h", y=1.02, font=dict(color="#8b949e")),
                plot_bgcolor="#0d1117", paper_bgcolor="#0a0c10",
                font=dict(color="#8b949e"),
                height=520,
                margin=dict(l=0, r=0, t=40, b=0),
            )
            st.plotly_chart(fig, width="stretch")

            latest = df.iloc[-1]
            m1, m2, m3, m4 = st.columns(4)
            m1.metric("收盘", f"{latest['close']:.2f}")
            m2.metric("最高", f"{latest['high']:.2f}")
            m3.metric("最低", f"{latest['low']:.2f}")
            m4.metric("成交量", f"{latest['volume']:.4f}")
        except Exception as exc:
            st.error(f"图表渲染失败：{exc}")
            fallback_df = pd.DataFrame(
                st.session_state.ohlcv_chart, columns=["ts", "open", "high", "low", "close", "volume"],
            )
            fallback_df["time"] = pd.to_datetime(fallback_df["ts"], unit="ms", utc=True).dt.tz_convert(GMT8)
            st.line_chart(fallback_df.set_index("time")["close"])
    elif st.session_state.ohlcv_chart is not None:
        st.warning("无数据 — 尝试其他周期或减少 K 线数")
    else:
        st.info("点击拉取加载图表数据")

# ── Tab: Trade ────────────────────────────────────────────────

with tab_llm:
    st.subheader("LLM 决策")
    if not cfg.exchange.sandbox:
        st.warning("主网模式：请仔细确认后再下单")

    st.caption(f"行情来源：`{current_market_source_label()}`")

    if st.button("获取决策", width="content", key="btn_llm"):
        with st.spinner("正在获取行情数据并请求 LLM..."):
            try:
                ex = get_exchange()
                ohlcv = ex.fetch_ohlcv(cfg.exchange.symbol, cfg.exchange.timeframe, cfg.exchange.max_candles)
                account_snapshot = fetch_account_snapshot(ex, cfg.exchange.symbol)
                mark_connected()

                prompt = build_prompt(
                    cfg.exchange.symbol, ohlcv, cfg.prompt.template,
                    cfg.prompt.kline_count, account_snapshot=account_snapshot,
                )
                raw = get_llm().decide(prompt)
                decision = parse_decision(raw)
                decision["_price"] = float(ohlcv[-1][4])
                decision["_raw"] = raw
                decision["_prompt"] = prompt
                decision["_account_snapshot"] = account_snapshot
                st.session_state.llm_decision = decision
            except Exception as exc:
                st.error(f"决策失败：{exc}")
                st.code(traceback.format_exc())

    if st.session_state.llm_decision:
        decision = st.session_state.llm_decision
        action = str(decision.get("action", "hold")).lower()
        model_position_pct = max(0.0, min(1.0, float(decision.get("position_pct", 0.0) or 0.0)))

        c1, c2, c3, c4 = st.columns(4)
        action_display = {"buy": "买入", "sell": "卖出", "hold": "持仓不动"}.get(action, action.upper())
        c1.metric("信号", f"{action_display}")
        c2.metric("仓位比例", f"{model_position_pct:.0%}")
        c3.metric("置信度", f"{float(decision.get('confidence', 0.0) or 0.0):.0%}")
        c4.metric("价格", f"{float(decision.get('_price', 0.0) or 0.0):.2f}")
        st.info(str(decision.get("reason", "")))

        with st.expander("Prompt 与原始输出", expanded=False):
            inp_col, out_col = st.columns(2)
            with inp_col:
                st.caption("Prompt")
                st.text_area("_", value=str(decision.get("_prompt", "")), height=200,
                             disabled=True, key="prompt_display", label_visibility="collapsed")
            with out_col:
                st.caption("原始输出")
                st.text_area("_", value=str(decision.get("_raw", "")), height=200,
                             disabled=True, key="raw_output_display", label_visibility="collapsed")

        if action != "hold":
            price = float(decision.get("_price", 0.0) or 0.0)
            account_snapshot = decision.get("_account_snapshot", {})
            available_usdc = float((account_snapshot or {}).get("cash_usdc", 0.0) or 0.0)
            position_qty = float((account_snapshot or {}).get("position_qty", 0.0) or 0.0)
            base_symbol = cfg.exchange.symbol.split("/")[0]

            if action == "buy":
                amount = (available_usdc / price) * model_position_pct if price > 0 else 0.0
            else:
                amount = position_qty * model_position_pct

            amount = max(0.0, float(amount or 0.0))
            notional = amount * price

            action_cn = "买入" if action == "buy" else "卖出"
            st.divider()
            st.write(f"订单：**{action_cn}** {amount:.6f} {base_symbol}  ≈ **{notional:.2f} USDC**")

            if amount <= 0:
                st.warning("无效仓位大小")
            elif notional < cfg.trading.min_trade_notional:
                st.warning(f"名义金额 {notional:.2f} USDC 低于最低 {cfg.trading.min_trade_notional} USDC")
            else:
                if st.button(f"确认{action_cn} {amount:.6f}", type="primary", key="btn_confirm_order"):
                    with st.spinner("提交中..."):
                        try:
                            order = get_exchange().create_market_order(cfg.exchange.symbol, action, amount)
                            mark_connected()
                            st.success("订单已提交")
                            st.json(order)
                            st.session_state.llm_decision = None
                        except Exception as exc:
                            st.error(f"下单失败：{exc}")
                            st.code(traceback.format_exc())
        else:
            st.success("LLM 建议持仓不动")

    st.divider()
    st.subheader("手动下单")
    st.caption("仅在点击按钮时调用 API")

    mc0, _ = st.columns([2, 4])
    with mc0:
        if st.button("刷新参考数据", key="btn_refresh_manual_snapshot", width="stretch"):
            with st.spinner("刷新中..."):
                try:
                    refresh_manual_order_snapshot()
                except Exception as exc:
                    st.session_state.manual_order_error = f"{exc}"

    manual_side = st.radio("方向", options=["buy", "sell"], horizontal=True, key="manual_order_side",
                           format_func=lambda x: "买入" if x == "buy" else "卖出")
    manual_mode = st.radio("模式", options=["By Quantity", "By USDC"], horizontal=True, key="manual_order_mode",
                           format_func=lambda x: "按数量" if x == "By Quantity" else "按金额")

    manual_snapshot = st.session_state.manual_order_snapshot if isinstance(st.session_state.manual_order_snapshot, dict) else {}
    manual_price = float(manual_snapshot.get("price", 0.0) or 0.0)
    cash_usdc = float(manual_snapshot.get("cash_usdc", 0.0) or 0.0)
    position_qty = float(manual_snapshot.get("position_qty", 0.0) or 0.0)
    manual_base = cfg.exchange.symbol.split("/")[0]

    if manual_mode == "By Quantity":
        base_amount = float(
            st.number_input(f"数量 ({manual_base})", min_value=0.0, value=0.001,
                            step=0.001, format="%.6f", key="manual_order_base_amount")
        )
    else:
        usdc_notional = float(
            st.number_input("名义金额 (USDC)", min_value=0.0,
                            value=float(max(cfg.trading.min_trade_notional, 10.0)),
                            step=1.0, format="%.2f", key="manual_order_usdc_notional")
        )
        base_amount = (usdc_notional / manual_price) if manual_price > 0 else 0.0

    manual_notional = base_amount * manual_price if manual_price > 0 else 0.0
    mc1, mc2, mc3 = st.columns(3)
    mc1.metric("价格", f"{manual_price:.2f}" if manual_price > 0 else "-")
    mc2.metric("预估金额", f"{manual_notional:.2f}")
    mc3.metric("预估数量", f"{base_amount:.6f}")

    updated_at = str(manual_snapshot.get("updated_at", "") or "")
    if updated_at:
        st.caption(f"现金：{cash_usdc:.4f} USDC | 可用：{position_qty:.6f} {manual_base} | {updated_at}")
    else:
        st.caption("刷新参考数据以进行预检查")

    manual_invalid_reason = ""
    if base_amount <= 0:
        manual_invalid_reason = "数量必须大于 0"
    elif manual_notional < cfg.trading.min_trade_notional:
        manual_invalid_reason = f"低于最低 {cfg.trading.min_trade_notional} USDC"
    elif manual_side == "buy" and manual_snapshot and manual_notional > cash_usdc:
        manual_invalid_reason = "现金不足"
    elif manual_side == "sell" and manual_snapshot and base_amount > position_qty:
        manual_invalid_reason = "持仓不足"

    if st.session_state.manual_order_error:
        st.warning(f"预检查受限：{st.session_state.manual_order_error}")
    elif manual_invalid_reason:
        st.warning(manual_invalid_reason)

    if st.button("提交手动订单", key="btn_manual_test_order", type="primary"):
        if manual_invalid_reason:
            st.error(manual_invalid_reason)
        else:
            with st.spinner("提交中..."):
                try:
                    order = get_exchange().create_market_order(cfg.exchange.symbol, manual_side, base_amount)
                    mark_connected()
                    st.success("订单已提交")
                    st.json(order)
                except Exception as exc:
                    st.error(f"下单失败：{exc}")
                    st.code(traceback.format_exc())

    st.divider()
    st.subheader("后台交易控制")
    st.caption("通过 IPC 与后台进程通信")

    def _ipc_status() -> dict[str, object] | None:
        try:
            resp = ipc_request({"action": "status"}, address=DEFAULT_IPC_ADDRESS, timeout=1.0)
            if resp.get("ok"):
                return resp
        except Exception:
            pass
        return None

    def _ipc_start(interval_sec: int, session_id: str) -> bool:
        try:
            resp = ipc_request(
                {"action": "start", "interval_sec": interval_sec, "session_id": session_id},
                address=DEFAULT_IPC_ADDRESS, timeout=2.0,
            )
            return bool(resp.get("ok"))
        except Exception:
            return False

    def _ipc_stop() -> bool:
        try:
            resp = ipc_request({"action": "stop"}, address=DEFAULT_IPC_ADDRESS, timeout=2.0)
            return bool(resp.get("ok"))
        except Exception:
            return False

    if not cfg.exchange.sandbox:
        st.warning("自动交易需要测试网模式")
    else:
        if is_rt_auto_refresh and isinstance(st.session_state.rt_daemon_control_snapshot, dict):
            daemon_status = st.session_state.rt_daemon_control_snapshot
        else:
            ipc_resp = _ipc_status()
            if ipc_resp:
                st.caption("状态来源: IPC")
                daemon_status = {k: v for k, v in ipc_resp.items() if k != "ok"}
            else:
                st.caption("状态来源: 文件（IPC 不可用）")
                daemon_control = load_daemon_control()
                daemon_status = load_daemon_status()
                daemon_status["enabled"] = bool(daemon_control.get("enabled", False))
                daemon_status["session_id"] = str(daemon_control.get("session_id", "") or "")
            st.session_state.rt_daemon_control_snapshot = daemon_status

        daemon_alive = is_daemon_alive(daemon_status)

        c1, c2, c3 = st.columns([2, 2, 2])
        with c1:
            st.number_input(
                "间隔 (秒)", min_value=5, max_value=3600, step=5,
                key="rt_interval_sec",
                value=int(daemon_status.get("interval_sec", DEFAULT_DAEMON_INTERVAL_SEC) or DEFAULT_DAEMON_INTERVAL_SEC),
                disabled=bool(daemon_status.get("enabled", False)),
            )
        with c2:
            st.write("")
            st.write("")
            if st.button("启动", key="btn_start_rt", width="stretch",
                         disabled=bool(daemon_status.get("enabled", False))):
                session_id = f"rt-{int(time.time() * 1000)}"
                interval_sec = int(st.session_state.rt_interval_sec)

                # 文件写入（向后兼容）
                reset_daemon_status(
                    state="starting", enabled=True,
                    interval_sec=interval_sec,
                    session_id=session_id, pid=int(daemon_status.get("pid", 0) or 0),
                )
                save_daemon_control({
                    "enabled": True,
                    "interval_sec": interval_sec,
                    "session_id": session_id,
                    "updated_at": datetime.now(GMT8).isoformat(),
                })

                ok, msg = (True, "")
                if not daemon_alive:
                    ok, msg = start_daemon_process()
                elif not _ipc_start(interval_sec, session_id):
                    st.caption("IPC 发送失败，将通过文件同步")

                if ok:
                    st.success("后台进程已启动")
                    if msg:
                        st.caption(msg)
                else:
                    st.error(msg)

                st.session_state.rt_daemon_control_snapshot = {
                    "enabled": True, "interval_sec": interval_sec,
                    "session_id": session_id,
                    "updated_at": datetime.now(GMT8).isoformat(),
                }
                st.rerun()
        with c3:
            st.write("")
            st.write("")
            if st.button("停止", key="btn_stop_rt", width="stretch",
                         disabled=not bool(daemon_status.get("enabled", False))):
                # IPC 优先
                if not _ipc_stop():
                    st.caption("IPC 发送失败，将通过文件同步")

                # 文件写入（向后兼容）
                reset_daemon_status(
                    state="stopping", enabled=False,
                    interval_sec=int(daemon_status.get("interval_sec", DEFAULT_DAEMON_INTERVAL_SEC) or DEFAULT_DAEMON_INTERVAL_SEC),
                    session_id=str(daemon_status.get("session_id", "") or ""),
                    pid=int(daemon_status.get("pid", 0) or 0),
                )
                save_daemon_control({
                    "enabled": False,
                    "interval_sec": int(daemon_status.get("interval_sec", DEFAULT_DAEMON_INTERVAL_SEC) or DEFAULT_DAEMON_INTERVAL_SEC),
                    "session_id": str(daemon_status.get("session_id", "") or ""),
                    "updated_at": datetime.now(GMT8).isoformat(),
                })
                st.success("已发送停止信号")
                st.session_state.rt_daemon_control_snapshot = None
                st.rerun()

        refresh_col, _ = st.columns([2, 4])
        with refresh_col:
            if st.button("刷新状态", key="btn_refresh_rt_snapshot", width="stretch"):
                st.session_state.rt_daemon_control_snapshot = None
                st.rerun()

        auto_c1, auto_c2 = st.columns([2, 2])
        with auto_c1:
            st.checkbox("自动刷新", key="rt_auto_refresh")
        with auto_c2:
            st.number_input("间隔 (秒)", min_value=1, max_value=60, step=1,
                           key="rt_auto_refresh_sec", disabled=not bool(st.session_state.rt_auto_refresh))

        status_col1, status_col2, status_col3 = st.columns(3)
        status_label = "运行中" if bool(daemon_status.get("enabled", False)) else "已停止"
        if bool(daemon_status.get("enabled", False)) and not daemon_alive:
            status_label = "进程离线"
        status_col1.metric("自动交易", status_label)

        state_info = str(daemon_status.get("state", "offline") or "offline")
        if daemon_status.get("last_error"):
            state_info = f"{state_info} (错误)"
        status_col2.metric("守护进程", state_info)

        next_run_dt = parse_iso_datetime(str(daemon_status.get("next_run_at", "") or ""))
        if next_run_dt:
            left_sec = max(0, int((next_run_dt - datetime.now(GMT8)).total_seconds()))
            status_col3.metric("下次执行", f"{left_sec}s")
        else:
            status_col3.metric("下次执行", "-")

        s_meta1, s_meta2, s_meta3 = st.columns(3)
        s_meta1.metric("进程ID", str(int(daemon_status.get("pid", 0) or 0)))
        s_meta2.metric("心跳", str(daemon_status.get("heartbeat_at", "")))
        s_meta3.metric("会话", str(daemon_status.get("session_id", "")))

        if daemon_status.get("last_error"):
            st.warning(f"最近错误：{daemon_status.get('last_error', '')}")

        snapshot = daemon_status.get("last_snapshot") if isinstance(daemon_status.get("last_snapshot"), dict) else None
        if snapshot:
            s1, s2, s3, s4 = st.columns(4)
            s1.metric("现金", f"{float(snapshot.get('cash_usdc', 0.0) or 0.0):.4f}")
            s2.metric("持仓量", f"{float(snapshot.get('position_qty', 0.0) or 0.0):.6f}")
            s3.metric("仓位价值", f"{float(snapshot.get('position_value', 0.0) or 0.0):.4f}")
            s4.metric("净值", f"{float(snapshot.get('net_value', 0.0) or 0.0):.4f}")
            st.caption(f"更新时间：{snapshot.get('balance_datetime', '')}")
            with st.expander("持仓详情", expanded=False):
                positions_rows = snapshot.get("positions", [])
                if positions_rows:
                    st.dataframe(pd.json_normalize(positions_rows), width="stretch")
                else:
                    st.info("暂无持仓")
        else:
            st.info("暂无快照，启动自动交易后将在此显示")

        last_decision = daemon_status.get("last_record") if isinstance(daemon_status.get("last_record"), dict) else None
        if last_decision:
            action_cn = {"buy": "买入", "sell": "卖出", "hold": "持仓不动"}.get(
                str(last_decision.get("action", "hold")).lower(),
                str(last_decision.get("action", "hold")).upper(),
            )
            executed_cn = {"buy": "买入", "sell": "卖出", "none": "未执行"}.get(
                str(last_decision.get("executed", "none")).lower(),
                str(last_decision.get("executed", "none")).upper(),
            )
            st.divider()
            st.write("**最近决策**")
            d1, d2, d3, d4 = st.columns(4)
            d1.metric("时间", str(last_decision.get("time", "")))
            d2.metric("动作", action_cn)
            d3.metric("执行", executed_cn)
            d4.metric("金额", f"{float(last_decision.get('notional', 0.0) or 0.0):.2f}")
            st.caption(f"理由：{str(last_decision.get('reason', ''))}")

        st.divider()
        st.write("**会话历史**")
        hc1, hc2 = st.columns([2, 2])
        with hc1:
            history_limit = st.number_input("记录数", min_value=20, max_value=5000, step=20, key="rt_history_limit")
        with hc2:
            st.write("")
            st.write("")
            st.button("刷新", key="btn_refresh_rt_history", width="stretch")

        if is_rt_auto_refresh:
            st.caption("自动刷新已激活 — 历史记录暂停")
        else:
            history_rows = load_recent_live_runs(limit=int(history_limit))
            session_views = build_live_session_views(
                history_rows,
                str(daemon_status.get("session_id", "") or ""),
                bool(daemon_status.get("enabled", False)),
            )

            if session_views:
                rounds_df = pd.DataFrame([
                    {
                        "session": str(view["summary"].get("session_id", ""))[:12],
                        "status": view["summary"].get("status", ""),
                        "start": str(view["summary"].get("started_at", ""))[:19],
                        "end": str(view["summary"].get("ended_at", ""))[:19],
                        "dur_s": float(view["summary"].get("duration_sec", 0.0) or 0.0),
                        "start_net": float(view["summary"].get("start_net_value", 0.0) or 0.0),
                        "end_net": float(view["summary"].get("end_net_value", 0.0) or 0.0),
                        "pnl": float(view["summary"].get("pnl_usdc", 0.0) or 0.0),
                        "pnl_pct(%)": float(view["summary"].get("pnl_pct", 0.0) or 0.0) * 100,
                        "cycles": int(view["summary"].get("cycles_total", 0) or 0),
                        "buys": int(view["summary"].get("buy_count", 0) or 0),
                        "sells": int(view["summary"].get("sell_count", 0) or 0),
                    }
                    for view in session_views
                ])
                st.dataframe(rounds_df, width="stretch")

                for idx, view in enumerate(session_views, start=1):
                    summary = view["summary"]
                    session_logs = view["logs"]
                    pnl_str = f"{float(summary.get('pnl_pct', 0.0) or 0.0) * 100:+.2f}%"
                    round_title = f"#{idx} | {pnl_str} | {summary.get('status', '')}"
                    with st.expander(round_title, expanded=False):
                        r1, r2, r3, r4 = st.columns(4)
                        r1.metric("起始净值", f"{float(summary.get('start_net_value', 0.0) or 0.0):.4f}")
                        r2.metric("结束净值", f"{float(summary.get('end_net_value', 0.0) or 0.0):.4f}")
                        r3.metric("盈亏 (USDC)", f"{float(summary.get('pnl_usdc', 0.0) or 0.0):+.4f}")
                        r4.metric("收益率", f"{float(summary.get('pnl_pct', 0.0) or 0.0) * 100:+.2f}%")
                        st.caption(
                            f"{summary.get('started_at', '')} → {summary.get('ended_at', '')} | "
                            f"周期数: {int(summary.get('cycles_total', 0) or 0)}"
                        )

                        decision_df = pd.DataFrame([
                            {
                                "time": str(row.get("time", ""))[:19],
                                "status": row.get("status", "success"),
                                "action": row.get("action", "hold"),
                                "executed": row.get("executed", "none"),
                                "pos_pct": row.get("model_position_pct", 0.0),
                                "conf": row.get("confidence", 0.0),
                                "price": row.get("price", 0.0),
                                "amount": row.get("amount", 0.0),
                                "notional": row.get("notional", 0.0),
                                "pnl": row.get("pnl_usdc", 0.0),
                                "net": (row.get("snapshot") or {}).get("net_value", 0.0),
                                "reason": str(row.get("reason", ""))[:60],
                            }
                            for row in session_logs
                        ])
                        st.dataframe(decision_df, width="stretch")

                        with st.expander("Prompt / 输出详情", expanded=False):
                            for log_index, row in enumerate(session_logs, start=1):
                                log_action = str(row.get("action", "hold")).upper()
                                title = f"#{log_index} | {str(row.get('time', ''))[:19]} | {log_action} | {row.get('executed', 'none')}"
                                with st.expander(title, expanded=False):
                                    left, right = st.columns(2)
                                    with left:
                                        st.caption("Prompt")
                                        st.text_area(f"rt_{idx}_{log_index}_p", value=str(row.get("prompt", row.get("prompt_preview", ""))), height=180, disabled=True, label_visibility="collapsed")
                                    with right:
                                        st.caption("原始输出")
                                        st.text_area(f"rt_{idx}_{log_index}_r", value=str(row.get("raw_output", "")), height=180, disabled=True, label_visibility="collapsed")
            else:
                st.info("暂无会话历史")

# ── Tab: Backtest ─────────────────────────────────────────────

with tab_backtest:
    st.subheader("回测")

    col1, col2 = st.columns(2)
    with col1:
        init_usdc = st.number_input("初始资金 (USDC)", min_value=100.0, value=float(cfg.backtest.initial_usdc), step=100.0)
        timeframes = ["1m", "5m", "15m", "1h", "4h", "1d"]
        bt_tf = st.selectbox("周期", timeframes, index=timeframes.index(cfg.exchange.timeframe), key="bt_tf")
        bt_candles = st.number_input("K线数", min_value=50, max_value=1000, value=max(cfg.exchange.max_candles, 200), step=50, key="bt_candles")
    with col2:
        st.caption("自定义范围（留空则拉取最新 N 根 K 线）")
        start_dt = st.date_input("开始日期", value=None, key="bt_start")
        end_dt = st.date_input("结束日期", value=None, key="bt_end")

    if st.button("运行回测", width="content", key="btn_backtest"):
        with st.spinner("回测运行中..."):
            try:
                start_ms = (
                    int(datetime.combine(start_dt, datetime.min.time(), tzinfo=GMT8).timestamp() * 1000)
                    if isinstance(start_dt, date) else cfg.backtest.start_ms
                )
                end_ms = (
                    int(datetime.combine(end_dt, datetime.min.time(), tzinfo=GMT8).timestamp() * 1000)
                    if isinstance(end_dt, date) else cfg.backtest.end_ms
                )

                ex = get_exchange()
                if start_ms or end_ms:
                    ohlcv = ex.fetch_ohlcv_range(cfg.exchange.symbol, bt_tf, start_ms, end_ms, limit=int(bt_candles))
                else:
                    ohlcv = ex.fetch_ohlcv(cfg.exchange.symbol, bt_tf, int(bt_candles))
                mark_connected()

                required_klines = max(1, int(cfg.prompt.kline_count))
                if len(ohlcv) < required_klines:
                    st.warning(f"数据不足：需要 {required_klines} 根 K 线")
                    st.session_state.bt_summary = None
                    st.session_state.bt_llm_logs = None
                    st.session_state.bt_ohlcv = None
                else:
                    llm_client = get_llm()
                    cash = float(init_usdc)
                    position = 0.0
                    trades = 0
                    llm_logs: list[dict[str, object]] = []

                    start_idx = max(0, required_klines - 1)
                    total_steps = len(ohlcv) - start_idx
                    progress = st.progress(0)

                    for step_no, candle_index in enumerate(range(start_idx, len(ohlcv)), start=1):
                        window = ohlcv[max(0, candle_index - (required_klines - 1)) : candle_index + 1]
                        price = float(ohlcv[candle_index][4])
                        ts = int(ohlcv[candle_index][0])
                        ts_dt = ms_to_gmt8(ts)

                        prompt = build_prompt(
                            cfg.exchange.symbol, window, cfg.prompt.template,
                            cfg.prompt.kline_count,
                            account_snapshot={
                                "cash_usdc": cash, "position_qty": position,
                                "position_value": position * price,
                                "net_value": cash + position * price,
                            },
                        )
                        raw = llm_client.decide(prompt)
                        decision = parse_decision(raw)
                        action = str(decision.get("action", "hold")).lower()
                        model_position_pct = max(0.0, min(1.0, float(decision.get("position_pct", 0.0) or 0.0)))

                        executed = "none"
                        amount = 0.0
                        notional = 0.0

                        if action == "buy":
                            target_amount = (cash / price) * model_position_pct if price > 0 else 0.0
                            target_notional = target_amount * price
                            if target_notional >= cfg.trading.min_trade_notional and cash > 0:
                                amount = min(target_amount, cash / price)
                                if amount > 0:
                                    notional = amount * price
                                    cash -= notional
                                    position += amount
                                    executed = "buy"
                                    trades += 1
                        elif action == "sell" and position > 0:
                            target_amount = min(position, position * model_position_pct)
                            target_notional = target_amount * price
                            if target_notional >= cfg.trading.min_trade_notional and target_amount > 0:
                                amount = target_amount
                                notional = amount * price
                                position -= amount
                                cash += notional
                                executed = "sell"
                                trades += 1

                        equity = cash + position * price
                        position_value = position * price
                        llm_logs.append({
                            "idx": step_no, "time": str(ts_dt), "price": price,
                            "action": action, "model_position_pct": model_position_pct,
                            "confidence": float(decision.get("confidence", 0.0)),
                            "executed": executed, "amount": amount, "notional": notional,
                            "cash": cash, "position_qty": position,
                            "position_value": position_value, "equity": equity,
                            "reason": str(decision.get("reason", "")),
                            "prompt": prompt, "raw_output": raw,
                        })
                        progress.progress(step_no / max(total_steps, 1))

                    final_price = float(ohlcv[-1][4])
                    final_equity = cash + position * final_price
                    pnl = final_equity - float(init_usdc)
                    pnl_pct = (pnl / float(init_usdc) * 100) if init_usdc else 0.0

                    st.session_state.bt_summary = {
                        "ending_cash": cash, "ending_position_qty": position,
                        "ending_position_value": position * final_price,
                        "final_equity": final_equity, "pnl": pnl, "pnl_pct": pnl_pct,
                        "trades": trades, "candles": len(ohlcv),
                    }
                    st.session_state.bt_llm_logs = llm_logs
                    st.session_state.bt_ohlcv = ohlcv

                    run_record = {
                        "saved_at": datetime.now(GMT8).isoformat(),
                        "symbol": cfg.exchange.symbol, "timeframe": bt_tf,
                        "candles": int(len(ohlcv)),
                        "range": {"start_ms": int(start_ms), "end_ms": int(end_ms)},
                        "prompt_config": {"template": cfg.prompt.template, "kline_count": int(cfg.prompt.kline_count)},
                        "summary": st.session_state.bt_summary,
                        "decision_logs": compact_backtest_logs(llm_logs),
                    }
                    saved_path = save_backtest_run(run_record)
                    st.caption(f"已保存：{saved_path}")
            except Exception as exc:
                st.error(f"回测失败：{exc}")
                st.code(traceback.format_exc())

    summary = st.session_state.bt_summary
    logs = st.session_state.bt_llm_logs
    if summary and logs:
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("现金", f"{float(summary['ending_cash']):.2f}")
        m2.metric("仓位价值", f"{float(summary['ending_position_value']):.2f}")
        pnl_delta = f"{float(summary['pnl']):+.2f} ({float(summary['pnl_pct']):+.1f}%)"
        m3.metric("净值", f"{float(summary['final_equity']):.2f}", delta=pnl_delta)
        m4.metric("持仓量", f"{float(summary['ending_position_qty']):.6f}")
        m5, m6 = st.columns(2)
        m5.metric("交易次数", int(summary["trades"]))
        m6.metric("决策数/K线数", f"{len(logs)} / {int(summary['candles'])}")

        chart_df = pd.DataFrame(logs)
        chart_df["time"] = pd.to_datetime(chart_df["time"], utc=True).dt.tz_convert(GMT8)
        chart_df["equity_curve"] = chart_df["equity"].astype(float)

        bt_ohlcv = st.session_state.bt_ohlcv
        if bt_ohlcv:
            k_df = pd.DataFrame(bt_ohlcv, columns=["ts", "open", "high", "low", "close", "volume"])
            k_df["time"] = pd.to_datetime(k_df["ts"], unit="ms", utc=True).dt.tz_convert(GMT8)

            fig2 = go.Figure()
            fig2.add_trace(go.Candlestick(
                x=k_df["time"], open=k_df["open"], high=k_df["high"],
                low=k_df["low"], close=k_df["close"], name="K线",
                increasing_line_color="#3fb950", decreasing_line_color="#f85149",
                yaxis="y",
            ))
            fig2.add_trace(go.Scatter(
                x=chart_df["time"], y=chart_df["equity_curve"],
                mode="lines", name="权益曲线",
                line=dict(color="#4dabf7", width=2.5), yaxis="y2",
            ))

            buy_df = chart_df[chart_df["executed"] == "buy"]
            sell_df = chart_df[chart_df["executed"] == "sell"]
            if not buy_df.empty:
                fig2.add_trace(go.Scatter(
                    x=buy_df["time"], y=buy_df["equity_curve"],
                    mode="markers", name="买入",
                    marker=dict(color="#3fb950", size=8, symbol="triangle-up"),
                    customdata=buy_df[["price", "amount", "notional"]],
                    hovertemplate="时间: %{x}<br>买入<br>权益: %{y:.2f}<br>价格: %{customdata[0]:.2f}<br>数量: %{customdata[1]:.6f}<br>金额: %{customdata[2]:.2f}<extra></extra>",
                    yaxis="y2",
                ))
            if not sell_df.empty:
                fig2.add_trace(go.Scatter(
                    x=sell_df["time"], y=sell_df["equity_curve"],
                    mode="markers", name="卖出",
                    marker=dict(color="#f85149", size=8, symbol="triangle-down"),
                    customdata=sell_df[["price", "amount", "notional"]],
                    hovertemplate="时间: %{x}<br>卖出<br>权益: %{y:.2f}<br>价格: %{customdata[0]:.2f}<br>数量: %{customdata[1]:.6f}<br>金额: %{customdata[2]:.2f}<extra></extra>",
                    yaxis="y2",
                ))

            fig2.add_shape(type="line", xref="paper", x0=0, x1=1,
                           yref="y2", y0=float(init_usdc), y1=float(init_usdc),
                           line=dict(color="#484f58", dash="dot"))
            fig2.add_annotation(xref="paper", x=1, yref="y2", y=float(init_usdc),
                                text="初始资金", showarrow=False, xanchor="left",
                                yanchor="bottom", font=dict(color="#484f58"))
            fig2.update_layout(
                xaxis=dict(title="时间", rangeslider=dict(visible=False), gridcolor="#21262d"),
                yaxis=dict(title="价格", side="left", showgrid=True, gridcolor="#21262d"),
                yaxis2=dict(title="权益", overlaying="y", side="right", showgrid=False),
                legend=dict(orientation="h", y=1.02, x=0, font=dict(color="#8b949e")),
                plot_bgcolor="#0d1117", paper_bgcolor="#0a0c10",
                font=dict(color="#8b949e"),
                height=520, margin=dict(l=0, r=0, t=30, b=0),
                hovermode="x unified",
            )
            st.plotly_chart(fig2, width="stretch")

        with st.expander("决策日志", expanded=False):
            decision_df = pd.DataFrame([
                {
                    "idx": row["idx"], "time": str(row["time"])[:19], "price": row["price"],
                    "action": row["action"], "pos_pct": row.get("model_position_pct", 0.0),
                    "conf": row["confidence"], "executed": row["executed"],
                    "amount": row["amount"], "notional": row["notional"],
                    "cash": row["cash"], "pos_qty": row["position_qty"],
                    "pos_val": row["position_value"], "equity": row["equity"],
                }
                for row in logs
            ])
            st.dataframe(decision_df, width="stretch")

            for row in logs:
                title = f"#{row['idx']} | {str(row['time'])[:19]} | {str(row['action']).upper()} | {row['executed']}"
                with st.expander(title, expanded=False):
                    left, right = st.columns(2)
                    with left:
                        st.caption("Prompt")
                        st.text_area(f"bt_p_{row['idx']}", value=str(row["prompt"]), height=220, disabled=True, label_visibility="collapsed")
                    with right:
                        st.caption("原始输出")
                        st.text_area(f"bt_r_{row['idx']}", value=str(row["raw_output"]), height=220, disabled=True, label_visibility="collapsed")

    with st.expander("已保存的回测", expanded=False):
        if is_rt_auto_refresh:
            st.caption("自动刷新已激活 — 已保存暂停显示")
        else:
            recent_runs = load_recent_backtest_runs(limit=20)
            if not recent_runs:
                st.info("暂无保存的回测记录")
            else:
                rows = []
                for run in recent_runs:
                    summary_data = run.get("summary", {}) if isinstance(run, dict) else {}
                    prompt_cfg = run.get("prompt_config", {}) if isinstance(run, dict) else {}
                    rows.append({
                        "saved_at": str(run.get("saved_at", ""))[:19],
                        "symbol": run.get("symbol", ""),
                        "timeframe": run.get("timeframe", ""),
                        "candles": run.get("candles", 0),
                        "trades": summary_data.get("trades", 0),
                        "pnl": summary_data.get("pnl", 0.0),
                        "pnl_pct": summary_data.get("pnl_pct", 0.0),
                        "kline_count": prompt_cfg.get("kline_count", ""),
                    })
                st.dataframe(pd.DataFrame(rows), width="stretch")
                st.caption(f"文件：{BACKTEST_RUNS_FILE}")

# ── Tab: Settings ─────────────────────────────────────────────

with tab_prompt:
    st.subheader("LLM 设置")
    st.caption("配置 API 地址、模型与 Prompt 模板")

    pending_editor = st.session_state.pop("llm_editor_pending", None)
    if isinstance(pending_editor, dict):
        st.session_state.llm_base_url_editor = str(pending_editor.get("base_url", st.session_state.llm_base_url_editor))
        st.session_state.llm_api_key_editor = str(pending_editor.get("api_key", st.session_state.llm_api_key_editor))
        st.session_state.llm_model_editor = str(pending_editor.get("model", st.session_state.llm_model_editor))
        st.session_state.llm_temperature_editor = float(pending_editor.get("temperature", st.session_state.llm_temperature_editor))

    pending_selector = st.session_state.pop("llm_saved_api_selector_pending", None)
    if isinstance(pending_selector, str) and pending_selector.strip():
        st.session_state.llm_saved_api_selector = pending_selector

    if st.session_state.pop("llm_new_api_name_pending_clear", False):
        st.session_state.llm_new_api_name = ""

    saved_api_names = [item.name for item in cfg.openai.saved_apis]
    if saved_api_names:
        apply_col, _ = st.columns([2, 3])
        with apply_col:
            selected_saved_api = st.selectbox("已保存的 API", saved_api_names, key="llm_saved_api_selector")
            if st.button("应用", key="btn_apply_saved_api", width="stretch"):
                target = next((item for item in cfg.openai.saved_apis if item.name == selected_saved_api), None)
                if target is None:
                    st.error("未找到该 API 配置")
                else:
                    st.session_state.llm_editor_pending = {
                        "base_url": str(target.base_url), "api_key": str(target.api_key),
                        "model": str(target.model), "temperature": float(target.temperature),
                    }
                    st.session_state.llm_saved_api_selector_pending = str(target.name)
                    st.rerun()
    else:
        st.caption("暂无保存的 API，请在下方配置并添加")

    llm_col1, llm_col2 = st.columns(2)
    with llm_col1:
        st.text_input("接口地址", key="llm_base_url_editor")
        st.text_input("API Key", key="llm_api_key_editor", type="password")
    with llm_col2:
        st.text_input("模型", key="llm_model_editor")
        st.number_input("温度", min_value=0.0, max_value=2.0, step=0.1, key="llm_temperature_editor")

    add_col1, add_col2 = st.columns([2, 1])
    with add_col1:
        st.text_input("新 API 名称", key="llm_new_api_name", placeholder="例如：deepseek-main")
    with add_col2:
        st.write("")
        st.write("")
        if st.button("保存为新配置", key="btn_add_saved_api", width="stretch"):
            new_name = str(st.session_state.llm_new_api_name).strip()
            api_key = str(st.session_state.llm_api_key_editor).strip()
            base_url = str(st.session_state.llm_base_url_editor).strip()
            model_name = str(st.session_state.llm_model_editor).strip()
            temperature = float(st.session_state.llm_temperature_editor)

            if not new_name:
                st.error("名称不能为空")
            elif any(item.name == new_name for item in cfg.openai.saved_apis):
                st.error("名称已存在")
            elif not api_key or not base_url or not model_name:
                st.error("请填写所有字段")
            else:
                cfg.openai.saved_apis.append(
                    LLMApiOption(name=new_name, base_url=base_url, api_key=api_key, model=model_name, temperature=temperature)
                )
                save_config(cfg)
                get_config.clear()
                st.session_state.llm_saved_api_selector_pending = new_name
                st.session_state.llm_new_api_name_pending_clear = True
                st.success(f"已保存：{new_name}")
                st.rerun()

    b1, b2, b3 = st.columns([1, 1, 1])
    with b1:
        if st.button("保存配置", key="btn_save_llm_cfg_tab", width="stretch"):
            api_key = str(st.session_state.llm_api_key_editor).strip()
            base_url = str(st.session_state.llm_base_url_editor).strip()
            model_name = str(st.session_state.llm_model_editor).strip()

            if not api_key:
                st.error("API Key 不能为空")
            elif not base_url:
                st.error("接口地址不能为空")
            elif not model_name:
                st.error("模型不能为空")
            else:
                cfg.openai.base_url = base_url
                cfg.openai.api_key = api_key
                cfg.openai.model = model_name
                cfg.openai.temperature = float(st.session_state.llm_temperature_editor)
                save_config(cfg)
                get_config.clear()
                st.success("已保存")
                st.rerun()
    with b2:
        if st.button("重置", key="btn_reset_llm_cfg_tab", width="stretch"):
            st.session_state.llm_editor_pending = {
                "base_url": str(cfg.openai.base_url), "api_key": str(cfg.openai.api_key),
                "model": str(cfg.openai.model), "temperature": float(cfg.openai.temperature),
            }
            st.rerun()
    with b3:
        if st.button("测试连接", key="btn_test_llm_cfg_tab", width="stretch"):
            api_key = str(st.session_state.llm_api_key_editor).strip()
            base_url = str(st.session_state.llm_base_url_editor).strip()
            model_name = str(st.session_state.llm_model_editor).strip()
            provider_name = str(cfg.openai.provider).strip().lower()

            if not api_key:
                st.error("API Key 不能为空")
            elif not base_url:
                st.error("接口地址不能为空")
            elif not model_name:
                st.error("模型不能为空")
            else:
                try:
                    temp_llm = get_llm_client_cached(provider_name, base_url, api_key, model_name, float(st.session_state.llm_temperature_editor))
                    raw = temp_llm.decide("Return ONLY JSON: {\"action\":\"hold\",\"position_pct\":0,\"confidence\":1,\"reason\":\"ok\"}")
                    parsed = parse_decision(raw)
                    st.session_state.llm_test_result = {"ok": True, "provider": provider_name, "model": model_name, "raw": raw, "parsed": parsed}
                    st.success("连接成功")
                except Exception as exc:
                    st.session_state.llm_test_result = {"ok": False, "provider": provider_name, "model": model_name, "error": str(exc)}
                    st.error(f"连接失败：{exc}")

    test_result = st.session_state.llm_test_result
    if isinstance(test_result, dict):
        with st.expander("测试结果", expanded=False):
            st.write(f"model={test_result.get('model', '')} | ok={test_result.get('ok', False)}")
            if test_result.get("ok"):
                st.json(test_result.get("parsed", {}))
                st.text_area("_", value=str(test_result.get("raw", "")), height=160, disabled=True, key="llm_test_raw_output")
            else:
                st.error(str(test_result.get("error", "未知错误")))

    st.caption(f"当前：模型={cfg.openai.model} | 接口={cfg.openai.base_url}")
    st.divider()
    st.subheader("Prompt 模板")
    st.caption("可用变量：{symbol}, {ohlcv_csv}, {account_snapshot_json}")

    st.number_input("K 线数量", min_value=1, max_value=500, step=1, key="prompt_kline_count_editor")
    st.text_area("模板", key="prompt_template_editor", height=320)

    c1, c2 = st.columns([1, 1])
    with c1:
        if st.button("保存模板", key="btn_save_prompt_cfg_tab", width="stretch"):
            template = str(st.session_state.prompt_template_editor)
            if "{symbol}" not in template or "{ohlcv_csv}" not in template:
                st.error("模板必须包含 {symbol} 和 {ohlcv_csv}")
            else:
                cfg.prompt.template = template
                cfg.prompt.kline_count = int(st.session_state.prompt_kline_count_editor)
                save_config(cfg)
                get_config.clear()
                st.success("已保存")
                st.rerun()
    with c2:
        if st.button("重置模板", key="btn_reset_prompt_cfg_tab", width="stretch"):
            st.session_state.prompt_template_editor = cfg.prompt.template
            st.session_state.prompt_kline_count_editor = int(cfg.prompt.kline_count)
            st.rerun()

    st.divider()
    st.caption(f"当前：K线数量={int(cfg.prompt.kline_count)}")
    st.text_area("预览（只读）", value=str(cfg.prompt.template), disabled=True, height=200)
