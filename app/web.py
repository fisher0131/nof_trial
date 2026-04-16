from __future__ import annotations

import json
import os
import subprocess
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
from app.exchange.hyperliquid_client import HYPERLIQUID_TESTNET_API_URL, HyperliquidClient
from app.llm import create_llm_client
from app.strategy.llm_strategy import build_prompt, parse_decision
from app.utils.logger import setup_logger

GMT8 = timezone(timedelta(hours=8))
BACKTEST_RUNS_FILE = _ROOT / "backtest_runs.jsonl"
LIVE_RUNS_FILE = _ROOT / "live_runs.jsonl"
DAEMON_CONTROL_FILE = _ROOT / "daemon_control.json"
DAEMON_STATUS_FILE = _ROOT / "daemon_status.json"
DEFAULT_DAEMON_INTERVAL_SEC = 300
HYPERLIQUID_MAINNET_API_URL = "https://api.hyperliquid.xyz"


def ms_to_gmt8(ts_ms: int | float) -> pd.Timestamp:
    return pd.to_datetime(ts_ms, unit="ms", utc=True).tz_convert(GMT8)


def load_json_file(path: Path, default: dict[str, object]) -> dict[str, object]:
    if not path.exists():
        return dict(default)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return dict(default)


def save_json_file(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


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


def load_daemon_status() -> dict[str, object]:
    return load_json_file(
        DAEMON_STATUS_FILE,
        {
            "pid": 0,
            "state": "offline",
            "enabled": False,
            "interval_sec": DEFAULT_DAEMON_INTERVAL_SEC,
            "session_id": "",
            "started_at": "",
            "heartbeat_at": "",
            "next_run_at": "",
            "last_cycle_at": "",
            "last_error": "",
            "last_snapshot": None,
            "last_record": None,
        },
    )


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


def is_pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        if os.name != "nt":
            return False
        try:
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                check=False,
                capture_output=True,
                text=True,
            )
        except Exception:
            return False
        output = (result.stdout or "").strip().lower()
        return bool(output) and "no tasks are running" not in output and str(pid) in output


def parse_iso_datetime(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=GMT8)
    return parsed


def is_daemon_alive(status: dict[str, object]) -> bool:
    pid = int(status.get("pid", 0) or 0)
    heartbeat = parse_iso_datetime(str(status.get("heartbeat_at", "") or ""))
    interval_sec = int(status.get("interval_sec", DEFAULT_DAEMON_INTERVAL_SEC) or DEFAULT_DAEMON_INTERVAL_SEC)
    if heartbeat is None:
        return False
    age = (datetime.now(GMT8) - heartbeat).total_seconds()
    allowed_age = max(15, interval_sec * 2 + 30)
    return is_pid_alive(pid) and age <= allowed_age


def start_daemon_process() -> tuple[bool, str]:
    command = [sys.executable, "-m", "app.daemon"]
    try:
        if os.name == "nt":
            creation_flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
            subprocess.Popen(
                command,
                cwd=str(_ROOT),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=creation_flags,
            )
        else:
            subprocess.Popen(
                command,
                cwd=str(_ROOT),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        return True, "daemon 进程已启动"
    except Exception as exc:
        return False, f"启动 daemon 失败: {exc}"


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


def extract_position_qty(positions: list[dict[str, object]]) -> float:
    pos_qty = 0.0
    for row in positions:
        contracts = row.get("contracts", row.get("positionAmt", 0)) if isinstance(row, dict) else 0
        if not contracts and isinstance(row, dict):
            contracts = row.get("contractSize", 0)
        if not contracts and isinstance(row, dict):
            inner_pos = row.get("position", {})
            if isinstance(inner_pos, dict):
                contracts = inner_pos.get("szi", inner_pos.get("positionAmt", 0))
        try:
            pos_qty += abs(float(contracts or 0))
        except (TypeError, ValueError):
            continue
    return pos_qty


def fetch_account_snapshot(ex: HyperliquidClient) -> dict[str, object]:
    balance = ex.fetch_balance()
    positions = ex.fetch_positions(cfg.exchange.symbol)
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


def current_market_source_label() -> str:
    if cfg.exchange.sandbox:
        return f"{HYPERLIQUID_MAINNET_API_URL}（测试网下单 + 主网行情）"
    return cfg.exchange.base_url or HYPERLIQUID_MAINNET_API_URL


def mark_connected() -> None:
    st.session_state.connected = True


def refresh_balance_snapshot() -> None:
    ex = get_exchange()
    snapshot = fetch_account_snapshot(ex)
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


st.set_page_config(
    page_title="LLM Trading Bot",
    page_icon="🤖",
    layout="wide",
)

cfg = get_config()
setup_logger(cfg.app.log_level)
init_session_state()

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

with st.sidebar:
    st.title("🤖 LLM Trading Bot")
    st.divider()

    mode_label = "🟢 测试网 (Sandbox)" if cfg.exchange.sandbox else "🔴 主网 (真实资金)"
    st.metric("运行模式", mode_label)
    if not cfg.exchange.sandbox:
        st.error("⚠️ 当前为主网模式，操作将使用真实资金！")

    st.metric("交易对", cfg.exchange.symbol)
    st.metric("K线周期", cfg.exchange.timeframe)
    st.metric("LLM 模型", cfg.openai.model)
    st.divider()

    conn_status = "✅ 已连接" if st.session_state.connected else "❌ 未连接"
    st.metric("交易所连接", conn_status)

    if st.button("🔌 连接交易所", width="stretch"):
        with st.spinner("正在连接..."):
            try:
                ex = get_exchange()
                ex.connect()
                mark_connected()
                st.success("连接成功！")
                st.rerun()
            except Exception as exc:
                st.error(f"连接失败：{exc}")

st.title("LLM Trading Bot Dashboard")
if not st.session_state.connected:
    st.info("👈 当前未连接交易所。你仍可先配置 LLM，交易相关操作在首次成功请求后会自动建立连接。")

tab_balance, tab_chart, tab_llm, tab_backtest, tab_prompt = st.tabs(
    ["💰 账户余额", "📈 K线图表", "🤖 LLM 决策下单", "🔬 历史回测", "🧩 全局Prompt设置"]
)

with tab_balance:
    st.subheader("账户余额")
    if st.button("🔄 刷新余额", key="btn_balance"):
        with st.spinner("查询中..."):
            try:
                refresh_balance_snapshot()
            except Exception as exc:
                st.session_state.balance_error = f"{exc}"

    snapshot = st.session_state.balance_snapshot
    if isinstance(snapshot, dict):
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("现金 (USDC)", f"{float(snapshot.get('cash_usdc', 0.0) or 0.0):.4f}")
        c2.metric("持仓价值 (USDC)", f"{float(snapshot.get('position_value', 0.0) or 0.0):.4f}")
        c3.metric("净值 (USDC)", f"{float(snapshot.get('net_value', 0.0) or 0.0):.4f}")
        c4.metric("已用保证金", f"{float(snapshot.get('margin_used', 0.0) or 0.0):.4f}")
        st.caption("现金口径说明：使用 spot 可用购买力（USDC.free）")

        st.divider()
        col1, col2 = st.columns(2)
        with col1:
            st.caption("全仓账户摘要（user_state）")
            st.json({k: v for k, v in dict(snapshot.get("cross_margin_summary", {})).items()})
        with col2:
            st.caption("持仓列表")
            positions = snapshot.get("positions", [])
            if positions:
                st.dataframe(pd.json_normalize(positions), width="stretch")
            else:
                st.info("当前无持仓")

        with st.expander("🐛 原始 user_state（点击展开）", expanded=False):
            st.json(snapshot.get("user_state", {}))
        st.caption(f"查询时间 (GMT+8): {snapshot.get('balance_datetime', '')}")
    elif st.session_state.balance_error:
        st.error(f"查询失败：{st.session_state.balance_error}")
    else:
        st.info("点击“刷新余额”后显示账户快照")

with tab_chart:
    st.subheader(f"{cfg.exchange.symbol} K线图表")
    ctrl1, ctrl2, ctrl3 = st.columns([2, 2, 2])
    with ctrl1:
        timeframes = ["1m", "5m", "15m", "1h", "4h", "1d"]
        tf = st.selectbox("周期", timeframes, index=timeframes.index(cfg.exchange.timeframe))
    with ctrl2:
        n_candles = st.number_input(
            "K线数量",
            min_value=20,
            max_value=500,
            value=cfg.exchange.max_candles,
            step=10,
        )
    with ctrl3:
        st.write("")
        st.write("")
        fetch_btn = st.button("📥 拉取 K 线", width="stretch", key="btn_chart")

    if fetch_btn:
        with st.spinner("拉取中..."):
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
                    st.warning("API 返回了空数据，请检查交易对和周期是否正确")
                else:
                    st.success(f"✅ 已拉取 {len(ohlcv)} 根 K 线")
            except Exception as exc:
                st.error(f"拉取失败：{exc}")
                st.code(traceback.format_exc())

    with st.expander("🐛 调试信息", expanded=False):
        meta = st.session_state.chart_fetch_meta or {}
        st.write(f"**行情数据来源:** `{meta.get('source', current_market_source_label())}`")
        st.write(f"**session_state.ohlcv_chart 类型:** `{type(st.session_state.ohlcv_chart)}`")
        if st.session_state.ohlcv_chart is not None:
            st.write(f"**数据长度:** `{len(st.session_state.ohlcv_chart)}`")
            if st.session_state.ohlcv_chart:
                st.write(f"**首行数据:** `{st.session_state.ohlcv_chart[0]}`")
                st.write(f"**末行数据:** `{st.session_state.ohlcv_chart[-1]}`")
        else:
            st.write("ohlcv_chart 为 None（尚未拉取）")

    if st.session_state.ohlcv_chart:
        try:
            ohlcv = st.session_state.ohlcv_chart
            df = pd.DataFrame(ohlcv, columns=["ts", "open", "high", "low", "close", "volume"])
            df["time"] = pd.to_datetime(df["ts"], unit="ms", utc=True).dt.tz_convert(GMT8)

            fig = go.Figure()
            fig.add_trace(
                go.Candlestick(
                    x=df["time"],
                    open=df["open"],
                    high=df["high"],
                    low=df["low"],
                    close=df["close"],
                    name="K线",
                )
            )
            fig.add_trace(
                go.Bar(
                    x=df["time"],
                    y=df["volume"],
                    name="成交量",
                    marker_color="rgba(100,149,237,0.35)",
                    yaxis="y2",
                )
            )
            fig.update_layout(
                xaxis_rangeslider_visible=False,
                yaxis=dict(title="价格"),
                yaxis2=dict(overlaying="y", side="right", showgrid=False, title="成交量"),
                legend=dict(orientation="h", y=1.02),
                height=520,
                margin=dict(l=0, r=0, t=40, b=0),
            )
            st.plotly_chart(fig, width="stretch")

            latest = df.iloc[-1]
            m1, m2, m3, m4 = st.columns(4)
            m1.metric("最新收盘价", f"{latest['close']:.2f}")
            m2.metric("最高价", f"{latest['high']:.2f}")
            m3.metric("最低价", f"{latest['low']:.2f}")
            m4.metric("成交量", f"{latest['volume']:.4f}")
        except Exception as exc:
            st.error(f"图表渲染失败：{exc}")
            st.code(traceback.format_exc())
            st.caption("降级显示：收盘价折线图")
            fallback_df = pd.DataFrame(
                st.session_state.ohlcv_chart,
                columns=["ts", "open", "high", "low", "close", "volume"],
            )
            fallback_df["time"] = pd.to_datetime(fallback_df["ts"], unit="ms", utc=True).dt.tz_convert(GMT8)
            st.line_chart(fallback_df.set_index("time")["close"])
    elif st.session_state.ohlcv_chart is not None:
        st.warning("返回数据为空，请尝试更换周期或减少 K 线数量后重新拉取")
    else:
        st.info("点击上方 **📥 拉取 K 线** 按钮加载图表")

with tab_llm:
    st.subheader("LLM 决策下单")
    if not cfg.exchange.sandbox:
        st.warning("⚠️ 主网模式：确认前请仔细核对，下单后不可撤销！")

    st.caption(f"📡 行情数据来源: `{current_market_source_label()}`")

    if st.button("🧠 获取 LLM 决策", width="content", key="btn_llm"):
        with st.spinner("拉取 K 线并询问 LLM..."):
            try:
                ex = get_exchange()
                ohlcv = ex.fetch_ohlcv(cfg.exchange.symbol, cfg.exchange.timeframe, cfg.exchange.max_candles)
                account_snapshot = fetch_account_snapshot(ex)
                mark_connected()

                prompt = build_prompt(
                    cfg.exchange.symbol,
                    ohlcv,
                    cfg.prompt.template,
                    cfg.prompt.kline_count,
                    account_snapshot=account_snapshot,
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
        color = {"buy": "🟢", "sell": "🔴", "hold": "🟡"}.get(action, "⚪")
        model_position_pct = max(0.0, min(1.0, float(decision.get("position_pct", 0.0) or 0.0)))

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("操作建议", f"{color} {action.upper()}")
        c2.metric("模型仓位比例", f"{model_position_pct:.0%}")
        c3.metric("置信度", f"{float(decision.get('confidence', 0.0) or 0.0):.0%}")
        c4.metric("参考价格", f"{float(decision.get('_price', 0.0) or 0.0):.2f}")
        st.info(f"**理由：** {decision.get('reason', '')}")

        with st.expander("🔍 模型输入 / 输出（点击展开）"):
            inp_col, out_col = st.columns(2)
            with inp_col:
                st.subheader("📥 模型输入 (Prompt)")
                st.text_area(
                    "Prompt 内容",
                    value=str(decision.get("_prompt", "")),
                    height=200,
                    disabled=True,
                    key="prompt_display",
                    label_visibility="collapsed",
                )
            with out_col:
                st.subheader("📤 模型原始输出")
                st.text_area(
                    "模型输出内容",
                    value=str(decision.get("_raw", "")),
                    height=200,
                    disabled=True,
                    key="raw_output_display",
                    label_visibility="collapsed",
                )

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

            st.divider()
            st.write(f"**预计下单：** `{action.upper()}` {amount:.6f} {base_symbol}  ≈ **{notional:.2f} USDC**")

            if amount <= 0:
                st.warning("模型给出的仓位比例无效或可用仓位不足，无法下单")
            elif notional < cfg.trading.min_trade_notional:
                st.warning(f"名义金额 {notional:.2f} USDC 低于最小下单额 {cfg.trading.min_trade_notional} USDC，无法下单")
            else:
                confirm_label = f"✅ 确认{'买入' if action == 'buy' else '卖出'} {amount:.6f}"
                if st.button(confirm_label, type="primary", key="btn_confirm_order"):
                    with st.spinner("提交订单..."):
                        try:
                            order = get_exchange().create_market_order(cfg.exchange.symbol, action, amount)
                            mark_connected()
                            st.success("订单已提交！")
                            st.json(order)
                            st.session_state.llm_decision = None
                        except Exception as exc:
                            st.error(f"下单失败：{exc}")
                            st.code(traceback.format_exc())
        else:
            st.success("LLM 建议持仓观望，无需操作")

    st.divider()
    st.subheader("手动指令下单（测试）")
    st.caption("仅在点击“刷新手动参考数据”或“提交订单”时访问交易所，切换标签页不会触发 API 请求。")

    mc0, _ = st.columns([2, 4])
    with mc0:
        if st.button("🔄 刷新手动参考数据", key="btn_refresh_manual_snapshot", width="stretch"):
            with st.spinner("刷新手动下单参考数据..."):
                try:
                    refresh_manual_order_snapshot()
                except Exception as exc:
                    st.session_state.manual_order_error = f"{exc}"

    manual_side = st.radio("方向", options=["buy", "sell"], horizontal=True, key="manual_order_side")
    manual_mode = st.radio("下单模式", options=["按币数量", "按USDC金额"], horizontal=True, key="manual_order_mode")

    manual_snapshot = st.session_state.manual_order_snapshot if isinstance(st.session_state.manual_order_snapshot, dict) else {}
    manual_price = float(manual_snapshot.get("price", 0.0) or 0.0)
    cash_usdc = float(manual_snapshot.get("cash_usdc", 0.0) or 0.0)
    position_qty = float(manual_snapshot.get("position_qty", 0.0) or 0.0)
    manual_base = cfg.exchange.symbol.split("/")[0]

    if manual_mode == "按币数量":
        base_amount = float(
            st.number_input(
                f"数量 ({manual_base})",
                min_value=0.0,
                value=0.001,
                step=0.001,
                format="%.6f",
                key="manual_order_base_amount",
            )
        )
    else:
        usdc_notional = float(
            st.number_input(
                "金额 (USDC)",
                min_value=0.0,
                value=float(max(cfg.trading.min_trade_notional, 10.0)),
                step=1.0,
                format="%.2f",
                key="manual_order_usdc_notional",
            )
        )
        base_amount = (usdc_notional / manual_price) if manual_price > 0 else 0.0

    manual_notional = base_amount * manual_price if manual_price > 0 else 0.0
    mc1, mc2, mc3 = st.columns(3)
    mc1.metric("参考价格", f"{manual_price:.2f}" if manual_price > 0 else "-")
    mc2.metric("预计成交额 (USDC)", f"{manual_notional:.2f}")
    mc3.metric("预计数量", f"{base_amount:.6f} {manual_base}")

    updated_at = str(manual_snapshot.get("updated_at", "") or "")
    if updated_at:
        st.caption(f"可用现金: {cash_usdc:.4f} USDC | 可卖数量: {position_qty:.6f} {manual_base} | 更新时间: {updated_at}")
    else:
        st.caption("先刷新一次手动参考数据，再按余额/仓位做预检查。")

    manual_invalid_reason = ""
    if base_amount <= 0:
        manual_invalid_reason = "下单数量必须大于 0"
    elif manual_notional < cfg.trading.min_trade_notional:
        manual_invalid_reason = f"预计成交额低于最小下单额 {cfg.trading.min_trade_notional} USDC"
    elif manual_side == "buy" and manual_snapshot and manual_notional > cash_usdc:
        manual_invalid_reason = "可用现金不足"
    elif manual_side == "sell" and manual_snapshot and base_amount > position_qty:
        manual_invalid_reason = "可卖持仓数量不足"

    if st.session_state.manual_order_error:
        st.warning(f"读取账户信息失败（仅影响预检查）：{st.session_state.manual_order_error}")
    elif manual_invalid_reason:
        st.warning(manual_invalid_reason)

    if st.button("🧪 提交手动测试订单", key="btn_manual_test_order", type="primary"):
        if manual_invalid_reason:
            st.error(f"无法下单：{manual_invalid_reason}")
        else:
            with st.spinner("提交手动测试订单..."):
                try:
                    order = get_exchange().create_market_order(cfg.exchange.symbol, manual_side, base_amount)
                    mark_connected()
                    st.success("手动测试订单已提交")
                    st.json(order)
                except Exception as exc:
                    st.error(f"手动下单失败：{exc}")
                    st.code(traceback.format_exc())

    st.divider()
    st.subheader("实时测试盘交易")
    st.caption("前端只读写 daemon 控制文件；自动刷新时仅读取 daemon_status.json。")

    if not cfg.exchange.sandbox:
        st.warning("实时自动交易仅允许在测试网模式下启用")
    else:
        if is_rt_auto_refresh and isinstance(st.session_state.rt_daemon_control_snapshot, dict):
            daemon_control = st.session_state.rt_daemon_control_snapshot
        else:
            daemon_control = load_daemon_control()
            st.session_state.rt_daemon_control_snapshot = daemon_control
        daemon_status = load_daemon_status()
        daemon_alive = is_daemon_alive(daemon_status)

        c1, c2, c3 = st.columns([2, 2, 2])
        with c1:
            st.number_input(
                "决策间隔（秒）",
                min_value=5,
                max_value=3600,
                step=5,
                key="rt_interval_sec",
                value=int(daemon_control.get("interval_sec", DEFAULT_DAEMON_INTERVAL_SEC) or DEFAULT_DAEMON_INTERVAL_SEC),
                disabled=bool(daemon_control.get("enabled", False)),
            )
        with c2:
            st.write("")
            st.write("")
            if st.button(
                "▶️ 开始实时交易",
                key="btn_start_rt",
                width="stretch",
                disabled=bool(daemon_control.get("enabled", False)),
            ):
                session_id = f"rt-{int(time.time() * 1000)}"
                reset_daemon_status(
                    state="starting",
                    enabled=True,
                    interval_sec=int(st.session_state.rt_interval_sec),
                    session_id=session_id,
                    pid=int(daemon_status.get("pid", 0) or 0),
                )
                save_daemon_control(
                    {
                        "enabled": True,
                        "interval_sec": int(st.session_state.rt_interval_sec),
                        "session_id": session_id,
                        "updated_at": datetime.now(GMT8).isoformat(),
                    }
                )
                st.session_state.rt_daemon_control_snapshot = {
                    "enabled": True,
                    "interval_sec": int(st.session_state.rt_interval_sec),
                    "session_id": session_id,
                    "updated_at": datetime.now(GMT8).isoformat(),
                }
                ok, msg = (True, "")
                if not daemon_alive:
                    ok, msg = start_daemon_process()
                if ok:
                    st.success("已下发启动指令，后台自动交易将持续运行")
                    if msg:
                        st.caption(msg)
                else:
                    st.error(msg)
                st.rerun()
        with c3:
            st.write("")
            st.write("")
            if st.button(
                "⏹️ 结束实时交易",
                key="btn_stop_rt",
                width="stretch",
                disabled=not bool(daemon_control.get("enabled", False)),
            ):
                reset_daemon_status(
                    state="stopping",
                    enabled=False,
                    interval_sec=int(daemon_control.get("interval_sec", DEFAULT_DAEMON_INTERVAL_SEC) or DEFAULT_DAEMON_INTERVAL_SEC),
                    session_id=str(daemon_control.get("session_id", "") or ""),
                    pid=int(daemon_status.get("pid", 0) or 0),
                )
                save_daemon_control(
                    {
                        "enabled": False,
                        "interval_sec": int(daemon_control.get("interval_sec", DEFAULT_DAEMON_INTERVAL_SEC) or DEFAULT_DAEMON_INTERVAL_SEC),
                        "session_id": str(daemon_control.get("session_id", "") or ""),
                        "updated_at": datetime.now(GMT8).isoformat(),
                    }
                )
                st.session_state.rt_daemon_control_snapshot = {
                    "enabled": False,
                    "interval_sec": int(daemon_control.get("interval_sec", DEFAULT_DAEMON_INTERVAL_SEC) or DEFAULT_DAEMON_INTERVAL_SEC),
                    "session_id": str(daemon_control.get("session_id", "") or ""),
                    "updated_at": datetime.now(GMT8).isoformat(),
                }
                st.success("已下发停止指令，daemon 将在当前周期结束后停止自动交易")
                st.rerun()

        refresh_col, _ = st.columns([2, 4])
        with refresh_col:
            if st.button("🔄 刷新 daemon 状态", key="btn_refresh_rt_snapshot", width="stretch"):
                st.session_state.rt_daemon_control_snapshot = load_daemon_control()
                st.rerun()

        auto_c1, auto_c2 = st.columns([2, 2])
        with auto_c1:
            st.checkbox("自动刷新 daemon 状态", key="rt_auto_refresh")
        with auto_c2:
            st.number_input(
                "刷新间隔(秒)",
                min_value=1,
                max_value=60,
                step=1,
                key="rt_auto_refresh_sec",
                disabled=not bool(st.session_state.rt_auto_refresh),
            )

        status_col1, status_col2, status_col3 = st.columns(3)
        status_label = "运行中" if bool(daemon_control.get("enabled", False)) else "已停止"
        if bool(daemon_control.get("enabled", False)) and not daemon_alive:
            status_label = "已下发启动但进程离线"
        status_col1.metric("自动交易状态", status_label)

        state_info = str(daemon_status.get("state", "offline") or "offline")
        if daemon_status.get("last_error"):
            state_info = f"{state_info} | 最近错误"
        status_col2.metric("daemon 状态", state_info)

        next_run_dt = parse_iso_datetime(str(daemon_status.get("next_run_at", "") or ""))
        if next_run_dt:
            left_sec = max(0, int((next_run_dt - datetime.now(GMT8)).total_seconds()))
            status_col3.metric("下次决策倒计时", f"{left_sec}s")
        else:
            status_col3.metric("下次决策倒计时", "-")

        s_meta1, s_meta2, s_meta3 = st.columns(3)
        s_meta1.metric("daemon PID", str(int(daemon_status.get("pid", 0) or 0)))
        s_meta2.metric("心跳时间", str(daemon_status.get("heartbeat_at", "")))
        s_meta3.metric("会话ID", str(daemon_status.get("session_id", "")))

        if daemon_status.get("last_error"):
            st.warning(f"最近一次异常：{daemon_status.get('last_error', '')}")

        snapshot = daemon_status.get("last_snapshot") if isinstance(daemon_status.get("last_snapshot"), dict) else None
        if snapshot:
            s1, s2, s3, s4 = st.columns(4)
            s1.metric("当前现金 (USDC)", f"{float(snapshot.get('cash_usdc', 0.0) or 0.0):.4f}")
            s2.metric("当前持仓数量", f"{float(snapshot.get('position_qty', 0.0) or 0.0):.6f}")
            s3.metric("当前持仓价值 (USDC)", f"{float(snapshot.get('position_value', 0.0) or 0.0):.4f}")
            s4.metric("当前净值 (USDC)", f"{float(snapshot.get('net_value', 0.0) or 0.0):.4f}")
            st.caption("现金口径说明：使用 spot 可用购买力（USDC.free）")
            st.caption(f"账户更新时间: {snapshot.get('balance_datetime', '')}")
            with st.expander("当前持仓明细", expanded=False):
                positions_rows = snapshot.get("positions", [])
                if positions_rows:
                    st.dataframe(pd.json_normalize(positions_rows), width="stretch")
                else:
                    st.info("当前无持仓")
        else:
            st.info("尚无账户快照，开始实时交易或等待 daemon 首轮执行后会显示。")

        last_decision = daemon_status.get("last_record") if isinstance(daemon_status.get("last_record"), dict) else None
        if last_decision:
            st.divider()
            st.write("**最近一次决策**")
            d1, d2, d3, d4 = st.columns(4)
            d1.metric("时间", str(last_decision.get("time", "")))
            d2.metric("动作", str(last_decision.get("action", "hold")).upper())
            d3.metric("执行", str(last_decision.get("executed", "none")).upper())
            d4.metric("成交额 (USDC)", f"{float(last_decision.get('notional', 0.0) or 0.0):.2f}")
            st.caption(f"理由: {str(last_decision.get('reason', ''))}")

        st.divider()
        st.write("**实时交易轮次（开始到结束为一轮）**")
        hc1, hc2 = st.columns([2, 2])
        with hc1:
            history_limit = st.number_input(
                "读取决策记录条数",
                min_value=20,
                max_value=5000,
                step=20,
                key="rt_history_limit",
            )
        with hc2:
            st.write("")
            st.write("")
            st.button("🔄 刷新轮次记录", key="btn_refresh_rt_history", width="stretch")

        if is_rt_auto_refresh:
            st.caption("自动刷新中：只更新 daemon 状态，轮次历史暂停读取。")
        else:
            history_rows = load_recent_live_runs(limit=int(history_limit))
            session_views = build_live_session_views(
                history_rows,
                str(daemon_control.get("session_id", "") or ""),
                bool(daemon_control.get("enabled", False)),
            )

            if session_views:
                rounds_df = pd.DataFrame(
                    [
                        {
                            "session_id": view["summary"].get("session_id", ""),
                            "status": view["summary"].get("status", ""),
                            "started_at": view["summary"].get("started_at", ""),
                            "ended_at": view["summary"].get("ended_at", ""),
                            "duration_sec": float(view["summary"].get("duration_sec", 0.0) or 0.0),
                            "start_net_value": float(view["summary"].get("start_net_value", 0.0) or 0.0),
                            "end_net_value": float(view["summary"].get("end_net_value", 0.0) or 0.0),
                            "pnl_usdc": float(view["summary"].get("pnl_usdc", 0.0) or 0.0),
                            "pnl_pct(%)": float(view["summary"].get("pnl_pct", 0.0) or 0.0) * 100,
                            "cycles_total": int(view["summary"].get("cycles_total", 0) or 0),
                            "buy_count": int(view["summary"].get("buy_count", 0) or 0),
                            "sell_count": int(view["summary"].get("sell_count", 0) or 0),
                        }
                        for view in session_views
                    ]
                )
                st.dataframe(rounds_df, width="stretch")
                st.caption("每轮收益率 = (结束净值 - 开始净值) / 开始净值")

                for idx, view in enumerate(session_views, start=1):
                    summary = view["summary"]
                    session_logs = view["logs"]
                    round_title = (
                        f"第{idx}轮 | {summary.get('session_id', '')} | "
                        f"收益率={float(summary.get('pnl_pct', 0.0) or 0.0) * 100:+.2f}% | "
                        f"状态={summary.get('status', '')}"
                    )
                    with st.expander(round_title, expanded=False):
                        r1, r2, r3, r4 = st.columns(4)
                        r1.metric("开始净值", f"{float(summary.get('start_net_value', 0.0) or 0.0):.4f}")
                        r2.metric("结束净值", f"{float(summary.get('end_net_value', 0.0) or 0.0):.4f}")
                        r3.metric("本轮盈亏(USDC)", f"{float(summary.get('pnl_usdc', 0.0) or 0.0):+.4f}")
                        r4.metric("本轮收益率", f"{float(summary.get('pnl_pct', 0.0) or 0.0) * 100:+.2f}%")

                        st.caption(
                            f"开始: {summary.get('started_at', '')} | 结束: {summary.get('ended_at', '')} | "
                            f"决策次数: {int(summary.get('cycles_total', 0) or 0)}"
                        )

                        decision_df = pd.DataFrame(
                            [
                                {
                                    "time": row.get("time", ""),
                                    "status": row.get("status", "success"),
                                    "action": row.get("action", "hold"),
                                    "executed": row.get("executed", "none"),
                                    "model_position_pct": row.get("model_position_pct", 0.0),
                                    "confidence": row.get("confidence", 0.0),
                                    "price": row.get("price", 0.0),
                                    "amount": row.get("amount", 0.0),
                                    "notional": row.get("notional", 0.0),
                                    "cycle_pnl_usdc": row.get("pnl_usdc", 0.0),
                                    "cycle_pnl_pct": float(row.get("pnl_pct", 0.0) or 0.0) * 100,
                                    "net_value": (row.get("snapshot") or {}).get("net_value", 0.0),
                                    "reason": row.get("reason", ""),
                                    "error": row.get("error", ""),
                                }
                                for row in session_logs
                            ]
                        )
                        st.dataframe(decision_df, width="stretch")

                        with st.expander("本轮模型决策详情（折叠）", expanded=False):
                            for log_index, row in enumerate(session_logs, start=1):
                                title = (
                                    f"#{log_index} | {row.get('time', '')} | "
                                    f"action={str(row.get('action', 'hold')).upper()} | "
                                    f"executed={row.get('executed', 'none')} | status={row.get('status', 'success')}"
                                )
                                with st.expander(title, expanded=False):
                                    left, right = st.columns(2)
                                    with left:
                                        st.caption("Prompt")
                                        st.text_area(
                                            f"rt_session_prompt_{idx}_{log_index}",
                                            value=str(row.get("prompt", row.get("prompt_preview", ""))),
                                            height=180,
                                            disabled=True,
                                            label_visibility="collapsed",
                                        )
                                    with right:
                                        st.caption("模型原始输出")
                                        st.text_area(
                                            f"rt_session_raw_{idx}_{log_index}",
                                            value=str(row.get("raw_output", "")),
                                            height=180,
                                            disabled=True,
                                            label_visibility="collapsed",
                                        )
            else:
                st.info("尚无实时交易轮次记录")

with tab_backtest:
    st.subheader("历史回测")

    col1, col2 = st.columns(2)
    with col1:
        init_usdc = st.number_input("初始资金 (USDC)", min_value=100.0, value=float(cfg.backtest.initial_usdc), step=100.0)
        timeframes = ["1m", "5m", "15m", "1h", "4h", "1d"]
        bt_tf = st.selectbox(
            "回测周期",
            timeframes,
            index=timeframes.index(cfg.exchange.timeframe),
            key="bt_tf",
        )
        bt_candles = st.number_input(
            "K线数量",
            min_value=50,
            max_value=1000,
            value=max(cfg.exchange.max_candles, 200),
            step=50,
            key="bt_candles",
        )
    with col2:
        st.caption("自定义时间范围（留空则使用最近 N 根 K 线）")
        start_dt = st.date_input("开始日期", value=None, key="bt_start")
        end_dt = st.date_input("结束日期", value=None, key="bt_end")

    if st.button("▶️ 运行回测", width="content", key="btn_backtest"):
        with st.spinner("拉取数据并回测..."):
            try:
                start_ms = (
                    int(datetime.combine(start_dt, datetime.min.time(), tzinfo=GMT8).timestamp() * 1000)
                    if isinstance(start_dt, date)
                    else cfg.backtest.start_ms
                )
                end_ms = (
                    int(datetime.combine(end_dt, datetime.min.time(), tzinfo=GMT8).timestamp() * 1000)
                    if isinstance(end_dt, date)
                    else cfg.backtest.end_ms
                )

                ex = get_exchange()
                if start_ms or end_ms:
                    ohlcv = ex.fetch_ohlcv_range(cfg.exchange.symbol, bt_tf, start_ms, end_ms, limit=int(bt_candles))
                else:
                    ohlcv = ex.fetch_ohlcv(cfg.exchange.symbol, bt_tf, int(bt_candles))
                mark_connected()

                required_klines = max(1, int(cfg.prompt.kline_count))
                if len(ohlcv) < required_klines:
                    st.warning(f"K 线数据不足，当前 Prompt 需要至少 {required_klines} 根 K 线才能开始决策")
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
                            cfg.exchange.symbol,
                            window,
                            cfg.prompt.template,
                            cfg.prompt.kline_count,
                            account_snapshot={
                                "cash_usdc": cash,
                                "position_qty": position,
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
                        llm_logs.append(
                            {
                                "idx": step_no,
                                "time": str(ts_dt),
                                "price": price,
                                "action": action,
                                "model_position_pct": model_position_pct,
                                "confidence": float(decision.get("confidence", 0.0)),
                                "executed": executed,
                                "amount": amount,
                                "notional": notional,
                                "cash": cash,
                                "position_qty": position,
                                "position_value": position_value,
                                "equity": equity,
                                "reason": str(decision.get("reason", "")),
                                "prompt": prompt,
                                "raw_output": raw,
                            }
                        )
                        progress.progress(step_no / max(total_steps, 1))

                    final_price = float(ohlcv[-1][4])
                    final_equity = cash + position * final_price
                    pnl = final_equity - float(init_usdc)
                    pnl_pct = (pnl / float(init_usdc) * 100) if init_usdc else 0.0

                    st.session_state.bt_summary = {
                        "ending_cash": cash,
                        "ending_position_qty": position,
                        "ending_position_value": position * final_price,
                        "final_equity": final_equity,
                        "pnl": pnl,
                        "pnl_pct": pnl_pct,
                        "trades": trades,
                        "candles": len(ohlcv),
                    }
                    st.session_state.bt_llm_logs = llm_logs
                    st.session_state.bt_ohlcv = ohlcv

                    run_record = {
                        "saved_at": datetime.now(GMT8).isoformat(),
                        "symbol": cfg.exchange.symbol,
                        "timeframe": bt_tf,
                        "candles": int(len(ohlcv)),
                        "range": {"start_ms": int(start_ms), "end_ms": int(end_ms)},
                        "prompt_config": {
                            "template": cfg.prompt.template,
                            "kline_count": int(cfg.prompt.kline_count),
                        },
                        "summary": st.session_state.bt_summary,
                        "decision_logs": compact_backtest_logs(llm_logs),
                    }
                    saved_path = save_backtest_run(run_record)
                    st.caption(f"💾 回测结果已保存：{saved_path}")
            except Exception as exc:
                st.error(f"回测失败：{exc}")
                st.code(traceback.format_exc())

    summary = st.session_state.bt_summary
    logs = st.session_state.bt_llm_logs
    if summary and logs:
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("现金 (USDC)", f"{float(summary['ending_cash']):.2f}")
        m2.metric("持仓价值 (USDC)", f"{float(summary['ending_position_value']):.2f}")
        m3.metric("净值 (USDC)", f"{float(summary['final_equity']):.2f}", delta=f"{float(summary['pnl']):+.2f} ({float(summary['pnl_pct']):+.1f}%)")
        m4.metric("持仓数量", f"{float(summary['ending_position_qty']):.6f}")
        m5, m6 = st.columns(2)
        m5.metric("执行交易次数", int(summary["trades"]))
        m6.metric("决策条数 / K线数量", f"{len(logs)} / {int(summary['candles'])}")

        chart_df = pd.DataFrame(logs)
        chart_df["time"] = pd.to_datetime(chart_df["time"], utc=True).dt.tz_convert(GMT8)
        chart_df["equity_curve"] = chart_df["equity"].astype(float)

        bt_ohlcv = st.session_state.bt_ohlcv
        if bt_ohlcv:
            k_df = pd.DataFrame(bt_ohlcv, columns=["ts", "open", "high", "low", "close", "volume"])
            k_df["time"] = pd.to_datetime(k_df["ts"], unit="ms", utc=True).dt.tz_convert(GMT8)

            fig2 = go.Figure()
            fig2.add_trace(
                go.Candlestick(
                    x=k_df["time"],
                    open=k_df["open"],
                    high=k_df["high"],
                    low=k_df["low"],
                    close=k_df["close"],
                    name="回测K线",
                    increasing_line_color="#2ecc71",
                    decreasing_line_color="#e74c3c",
                    yaxis="y",
                )
            )
            fig2.add_trace(
                go.Scatter(
                    x=chart_df["time"],
                    y=chart_df["equity_curve"],
                    mode="lines",
                    name="LLM 净值曲线",
                    line=dict(color="#00E5FF", width=3),
                    yaxis="y2",
                )
            )

            buy_df = chart_df[chart_df["executed"] == "buy"]
            sell_df = chart_df[chart_df["executed"] == "sell"]
            if not buy_df.empty:
                fig2.add_trace(
                    go.Scatter(
                        x=buy_df["time"],
                        y=buy_df["equity_curve"],
                        mode="markers",
                        name="买入点",
                        marker=dict(color="#39FF14", size=10, symbol="triangle-up"),
                        customdata=buy_df[["price", "amount", "notional"]],
                        hovertemplate=(
                            "时间: %{x}<br>"
                            "方向: BUY<br>"
                            "净值: %{y:.2f} USDC<br>"
                            "价格: %{customdata[0]:.2f}<br>"
                            "数量: %{customdata[1]:.6f}<br>"
                            "金额: %{customdata[2]:.2f} USDC<extra></extra>"
                        ),
                        yaxis="y2",
                    )
                )
            if not sell_df.empty:
                fig2.add_trace(
                    go.Scatter(
                        x=sell_df["time"],
                        y=sell_df["equity_curve"],
                        mode="markers",
                        name="卖出点",
                        marker=dict(color="#FF4D4F", size=10, symbol="triangle-down"),
                        customdata=sell_df[["price", "amount", "notional"]],
                        hovertemplate=(
                            "时间: %{x}<br>"
                            "方向: SELL<br>"
                            "净值: %{y:.2f} USDC<br>"
                            "价格: %{customdata[0]:.2f}<br>"
                            "数量: %{customdata[1]:.6f}<br>"
                            "金额: %{customdata[2]:.2f} USDC<extra></extra>"
                        ),
                        yaxis="y2",
                    )
                )

            fig2.add_shape(
                type="line",
                xref="paper",
                x0=0,
                x1=1,
                yref="y2",
                y0=float(init_usdc),
                y1=float(init_usdc),
                line=dict(color="gray", dash="dot"),
            )
            fig2.add_annotation(
                xref="paper",
                x=1,
                yref="y2",
                y=float(init_usdc),
                text="初始资金",
                showarrow=False,
                xanchor="left",
                yanchor="bottom",
                font=dict(color="gray"),
            )
            fig2.update_layout(
                xaxis=dict(title="时间", rangeslider=dict(visible=False)),
                yaxis=dict(title="价格", side="left", showgrid=True),
                yaxis2=dict(title="净值 (USDC)", overlaying="y", side="right", showgrid=False),
                legend=dict(orientation="h", y=1.02, x=0),
                height=520,
                margin=dict(l=0, r=0, t=30, b=0),
                hovermode="x unified",
            )
            st.plotly_chart(fig2, width="stretch")

        with st.expander("🧠 每一步决策（模型输入/输出）", expanded=False):
            decision_df = pd.DataFrame(
                [
                    {
                        "idx": row["idx"],
                        "time": row["time"],
                        "price": row["price"],
                        "action": row["action"],
                        "model_position_pct": row.get("model_position_pct", 0.0),
                        "confidence": row["confidence"],
                        "executed": row["executed"],
                        "amount": row["amount"],
                        "notional": row["notional"],
                        "cash": row["cash"],
                        "position_qty": row["position_qty"],
                        "position_value": row["position_value"],
                        "equity": row["equity"],
                    }
                    for row in logs
                ]
            )
            st.dataframe(decision_df, width="stretch")

            for row in logs:
                title = f"#{row['idx']} | {row['time']} | action={str(row['action']).upper()} | executed={row['executed']}"
                with st.expander(title, expanded=False):
                    left, right = st.columns(2)
                    with left:
                        st.caption("模型输入 Prompt")
                        st.text_area(
                            f"prompt_{row['idx']}",
                            value=str(row["prompt"]),
                            height=220,
                            disabled=True,
                            label_visibility="collapsed",
                        )
                    with right:
                        st.caption("模型原始输出")
                        st.text_area(
                            f"raw_{row['idx']}",
                            value=str(row["raw_output"]),
                            height=220,
                            disabled=True,
                            label_visibility="collapsed",
                        )

    with st.expander("📦 已保存回测记录", expanded=False):
        if is_rt_auto_refresh:
            st.caption("自动刷新中：已保存回测记录暂停读取。")
        else:
            recent_runs = load_recent_backtest_runs(limit=20)
            if not recent_runs:
                st.info("暂无已保存回测记录")
            else:
                rows = []
                for run in recent_runs:
                    summary_data = run.get("summary", {}) if isinstance(run, dict) else {}
                    prompt_cfg = run.get("prompt_config", {}) if isinstance(run, dict) else {}
                    rows.append(
                        {
                            "saved_at": run.get("saved_at", ""),
                            "symbol": run.get("symbol", ""),
                            "timeframe": run.get("timeframe", ""),
                            "candles": run.get("candles", 0),
                            "trades": summary_data.get("trades", 0),
                            "pnl": summary_data.get("pnl", 0.0),
                            "pnl_pct": summary_data.get("pnl_pct", 0.0),
                            "prompt_kline_count": prompt_cfg.get("kline_count", ""),
                        }
                    )
                st.dataframe(pd.DataFrame(rows), width="stretch")
                st.caption(f"存储文件：{BACKTEST_RUNS_FILE}")

with tab_prompt:
    st.subheader("LLM 配置")
    st.caption("支持在 Web 中直接配置 base_url / token / model / temperature")

    pending_editor = st.session_state.pop("llm_editor_pending", None)
    if isinstance(pending_editor, dict):
        st.session_state.llm_base_url_editor = str(pending_editor.get("base_url", st.session_state.llm_base_url_editor))
        st.session_state.llm_api_key_editor = str(pending_editor.get("api_key", st.session_state.llm_api_key_editor))
        st.session_state.llm_model_editor = str(pending_editor.get("model", st.session_state.llm_model_editor))
        st.session_state.llm_temperature_editor = float(
            pending_editor.get("temperature", st.session_state.llm_temperature_editor)
        )

    pending_selector = st.session_state.pop("llm_saved_api_selector_pending", None)
    if isinstance(pending_selector, str) and pending_selector.strip():
        st.session_state.llm_saved_api_selector = pending_selector

    if st.session_state.pop("llm_new_api_name_pending_clear", False):
        st.session_state.llm_new_api_name = ""

    saved_api_names = [item.name for item in cfg.openai.saved_apis]
    if saved_api_names:
        apply_col, _ = st.columns([2, 3])
        with apply_col:
            selected_saved_api = st.selectbox("已保存 API 选项", saved_api_names, key="llm_saved_api_selector")
            if st.button("📥 应用所选 API", key="btn_apply_saved_api", width="stretch"):
                target = next((item for item in cfg.openai.saved_apis if item.name == selected_saved_api), None)
                if target is None:
                    st.error("未找到对应的已保存 API 选项")
                else:
                    st.session_state.llm_editor_pending = {
                        "base_url": str(target.base_url),
                        "api_key": str(target.api_key),
                        "model": str(target.model),
                        "temperature": float(target.temperature),
                    }
                    st.session_state.llm_saved_api_selector_pending = str(target.name)
                    st.rerun()
    else:
        st.caption("暂无已保存 API 选项，可先在下方手动填写后新增。")

    llm_col1, llm_col2 = st.columns(2)
    with llm_col1:
        st.text_input("Base URL", key="llm_base_url_editor")
        st.text_input("API Token", key="llm_api_key_editor", type="password")
    with llm_col2:
        st.text_input("模型名称", key="llm_model_editor")
        st.number_input("Temperature", min_value=0.0, max_value=2.0, step=0.1, key="llm_temperature_editor")

    add_col1, add_col2 = st.columns([2, 1])
    with add_col1:
        st.text_input("新 API 选项名称", key="llm_new_api_name", placeholder="例如：deepseek-主账号")
    with add_col2:
        st.write("")
        st.write("")
        if st.button("➕ 添加为新 API 选项", key="btn_add_saved_api", width="stretch"):
            new_name = str(st.session_state.llm_new_api_name).strip()
            api_key = str(st.session_state.llm_api_key_editor).strip()
            base_url = str(st.session_state.llm_base_url_editor).strip()
            model_name = str(st.session_state.llm_model_editor).strip()
            temperature = float(st.session_state.llm_temperature_editor)

            if not new_name:
                st.error("新 API 选项名称不能为空")
            elif any(item.name == new_name for item in cfg.openai.saved_apis):
                st.error("该 API 选项名称已存在，请换一个名称")
            elif not api_key or not base_url or not model_name:
                st.error("请先填写完整的 Base URL / API Token / 模型名称")
            else:
                cfg.openai.saved_apis.append(
                    LLMApiOption(
                        name=new_name,
                        base_url=base_url,
                        api_key=api_key,
                        model=model_name,
                        temperature=temperature,
                    )
                )
                save_config(cfg)
                get_config.clear()
                st.session_state.llm_saved_api_selector_pending = new_name
                st.session_state.llm_new_api_name_pending_clear = True
                st.success(f"已新增 API 选项：{new_name}")
                st.rerun()

    b1, b2, b3 = st.columns([1, 1, 1])
    with b1:
        if st.button("💾 保存 LLM 配置", key="btn_save_llm_cfg_tab", width="stretch"):
            api_key = str(st.session_state.llm_api_key_editor).strip()
            base_url = str(st.session_state.llm_base_url_editor).strip()
            model_name = str(st.session_state.llm_model_editor).strip()

            if not api_key:
                st.error("API Token 不能为空")
            elif not base_url:
                st.error("Base URL 不能为空")
            elif not model_name:
                st.error("模型名称不能为空")
            else:
                cfg.openai.base_url = base_url
                cfg.openai.api_key = api_key
                cfg.openai.model = model_name
                cfg.openai.temperature = float(st.session_state.llm_temperature_editor)
                save_config(cfg)
                get_config.clear()
                st.success("LLM 配置已保存到 config.json")
                st.rerun()
    with b2:
        if st.button("↩️ 恢复当前 LLM 配置", key="btn_reset_llm_cfg_tab", width="stretch"):
            st.session_state.llm_editor_pending = {
                "base_url": str(cfg.openai.base_url),
                "api_key": str(cfg.openai.api_key),
                "model": str(cfg.openai.model),
                "temperature": float(cfg.openai.temperature),
            }
            st.rerun()
    with b3:
        if st.button("🔌 测试 LLM 连接", key="btn_test_llm_cfg_tab", width="stretch"):
            api_key = str(st.session_state.llm_api_key_editor).strip()
            base_url = str(st.session_state.llm_base_url_editor).strip()
            model_name = str(st.session_state.llm_model_editor).strip()
            provider_name = str(cfg.openai.provider).strip().lower()

            if not api_key:
                st.error("API Token 不能为空")
            elif not base_url:
                st.error("Base URL 不能为空")
            elif not model_name:
                st.error("模型名称不能为空")
            else:
                try:
                    temp_llm = get_llm_client_cached(
                        provider_name,
                        base_url,
                        api_key,
                        model_name,
                        float(st.session_state.llm_temperature_editor),
                    )
                    raw = temp_llm.decide(
                        "Return ONLY JSON with fields: action=hold, position_pct=0, confidence=1, reason='connection ok'."
                    )
                    parsed = parse_decision(raw)
                    st.session_state.llm_test_result = {
                        "ok": True,
                        "provider": provider_name,
                        "model": model_name,
                        "raw": raw,
                        "parsed": parsed,
                    }
                    st.success("LLM 连接测试成功")
                except Exception as exc:
                    st.session_state.llm_test_result = {
                        "ok": False,
                        "provider": provider_name,
                        "model": model_name,
                        "error": str(exc),
                    }
                    st.error(f"LLM 连接测试失败：{exc}")

    test_result = st.session_state.llm_test_result
    if isinstance(test_result, dict):
        with st.expander("🧪 最近一次 LLM 连接测试结果", expanded=False):
            st.write(f"model={test_result.get('model', '')} | ok={test_result.get('ok', False)}")
            if test_result.get("ok"):
                st.caption("解析后决策")
                st.json(test_result.get("parsed", {}))
                st.caption("模型原始输出")
                st.text_area(
                    "",
                    value=str(test_result.get("raw", "")),
                    height=160,
                    disabled=True,
                    key="llm_test_raw_output",
                )
            else:
                st.error(str(test_result.get("error", "未知错误")))

    st.caption(f"当前生效：model={cfg.openai.model} | base_url={cfg.openai.base_url}")
    st.divider()
    st.subheader("全局 Prompt 设置")
    st.caption("该配置全局生效于交易决策与历史回测")
    st.caption("模板变量：{symbol}、{ohlcv_csv}、{account_snapshot_json}")

    st.number_input("自动填入 K 线数量", min_value=1, max_value=500, step=1, key="prompt_kline_count_editor")
    st.text_area("Prompt 模板", key="prompt_template_editor", height=320)

    c1, c2 = st.columns([1, 1])
    with c1:
        if st.button("💾 保存 Prompt 配置", key="btn_save_prompt_cfg_tab", width="stretch"):
            template = str(st.session_state.prompt_template_editor)
            if "{symbol}" not in template or "{ohlcv_csv}" not in template:
                st.error("模板必须包含 {symbol} 和 {ohlcv_csv} 占位符")
            else:
                cfg.prompt.template = template
                cfg.prompt.kline_count = int(st.session_state.prompt_kline_count_editor)
                save_config(cfg)
                get_config.clear()
                st.success("Prompt 配置已保存到 config.json")
                st.rerun()
    with c2:
        if st.button("↩️ 恢复当前配置", key="btn_reset_prompt_cfg_tab", width="stretch"):
            st.session_state.prompt_template_editor = cfg.prompt.template
            st.session_state.prompt_kline_count_editor = int(cfg.prompt.kline_count)
            st.rerun()

    st.divider()
    st.caption("当前生效配置预览")
    st.write(f"K 线数量：{int(cfg.prompt.kline_count)}")
    st.text_area("当前模板（只读）", value=str(cfg.prompt.template), disabled=True, height=200)
