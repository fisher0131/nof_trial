# 性能瓶颈分析报告 — LLM Trading Bot (Streamlit)

## 总览

项目的卡顿根源是 **Streamlit 的运行模型被严重误用**：每次用户交互都会从顶到底重新执行整个 `web.py`（1905 行），而脚本中存在大量在每次重跑时都会触发的 **网络 I/O、文件 I/O、重型数据处理**，这些阻塞操作直接拦在了渲染路径上。

---

## 瓶颈 1 — Tab 3（手动下单区域）每次渲染都调用两次交易所 API

**严重程度：🔴 极高**

**位置：** `web.py` L918 & L953–957

```python
# L918 — 每次 rerender 都执行（无条件）
manual_ohlcv = get_exchange().fetch_ohlcv(cfg.exchange.symbol, cfg.exchange.timeframe, 2)

# L953–957 — 每次 rerender 都执行（无条件）
bal = get_exchange().fetch_balance()
pos = get_exchange().fetch_positions(cfg.exchange.symbol)
```

**问题：** 这两段代码位于 `with tab_llm:` 块内，但**在任何按钮点击之外**、在普通代码流中直接调用，意味着只要页面刷新（包括自动刷新、切换 Tab、点击任意按钮），就会强制发起 **3 次交易所 HTTP 请求**。每次网络往返至少 500ms～2s（Hyperliquid 海外节点），叠加起来即 1.5s～6s+ 的阻塞。

**修复方案：**
- 将这两段调用移入按钮点击的回调内（仅在用户主动触发时执行）。
- 或用 `@st.cache_data(ttl=30)` 做短时 cache，TTL 内复用结果。

---

## 瓶颈 2 — Tab 3 调试区域每次渲染都实例化 exchange（即使未连接）

**严重程度：🔴 高**

**位置：** `web.py` L782–788

```python
# Tab 3 开头（无条件执行）
ex = get_exchange()
mkt = getattr(ex, "_mkt_exchange", None)
```

**问题：** `get_exchange()` 内部的 `HyperliquidClient` 在首次调用时会执行 `connect()` → `load_markets()`（重型操作，加载所有交易对元数据），但这里是写在 Tab UI 的最外层，**每次 rerender 都会调用**。在交换所未连接时还会隐式触发连接，造成意外延迟。

---

## 瓶颈 3 — `load_recent_live_runs()` 每次渲染全量读取并解析 JSONL 文件

**严重程度：🔴 高**

**位置：** `web.py` L1179 & `load_recent_live_runs()` L67–77

```python
# 每次 rerender 都执行（Tab 3 实时面板区）
history_rows = load_recent_live_runs(limit=int(history_limit))
```

```python
def load_recent_live_runs(limit: int = 100) -> list[dict]:
    lines = LIVE_RUNS_FILE.read_text(encoding="utf-8").splitlines()  # 全量读取！
    for line in lines[-limit:]:
        rows.append(json.loads(line))
```

**问题：** `live_runs.jsonl` 目前已达 **2.35 MB / 424 行**，且每条记录都包含完整的 prompt 文本（数百至数千字符）。`read_text()` 会把整个文件读入内存，`splitlines()` 再拆分，最后还要 JSON 反序列化。这个操作在 Tab 3 打开期间**每次 rerender 都无条件触发**，随着文件增长会线性变慢。

**修复方案：**
- 用 `@st.cache_data(ttl=5)` 缓存读取结果。
- 或用二进制反向读取（从文件末尾读 N 行），避免全量加载。

---

## 瓶颈 4 — `build_live_session_views()` 在每次渲染中做大量 CPU 聚合计算

**严重程度：🟠 中高**

**位置：** `web.py` L1180–1184

```python
session_views = build_live_session_views(
    logs=history_rows,          # 最多 5000 条记录
    active_session_id=...,
    active_enabled=...,
)
```

**问题：** `build_live_session_views()` 对所有记录按 session 分组、排序、计算统计，包含多次 `sorted()`、`pd.to_datetime()`、`sum()` 遍历，复杂度为 O(N log N)。当 `history_limit=5000` 时数据量大，且这段代码在 Tab 3 打开时每次 rerender 都重新跑一遍。

**修复方案：** 结果用 `st.session_state` 或 `@st.cache_data` 缓存，只在数据源（文件）有更新时重计算。

---

## 瓶颈 5 — 自动刷新（`st_autorefresh`）持续触发整页重新执行

**严重程度：🟠 中高**

**位置：** `web.py` L1101–1104

```python
if bool(st.session_state.rt_auto_refresh):
    interval_ms = int(st.session_state.rt_auto_refresh_sec) * 1000
    if hasattr(st, "autorefresh"):
        st.autorefresh(interval=interval_ms, key="rt_daemon_autorefresh")
```

**问题：** `st.autorefresh` 的实现方式是定时触发整个 Streamlit 脚本重新执行（等效于用户操作），这意味着每隔 `rt_auto_refresh_sec`（默认 5s）就会触发所有上述瓶颈（3 次 API 调用 + 全量文件读 + 聚合计算），完全抵消自动刷新带来的体验优势。

**修复方案：**
- 禁用自动刷新期间的按钮级 API 调用（guard 语句），把重 I/O 操作限制为只在自动刷新路径上按需调用，其余 rerender 跳过。
- 或将状态面板移至独立页面（multipage）减少刷新范围。

---

## 瓶颈 6 — `get_llm()` 每次调用都创建新的 LLM Client 实例

**严重程度：🟡 中**

**位置：** `web.py` L458–459

```python
def get_llm() -> object:
    return create_llm_client(cfg.openai)  # 每次都 new 一个新对象
```

**问题：** `get_llm()` 没有任何缓存，每次调用都重新构造 `OpenAIClient` 或 `DeepSeekClient`（含 HTTP client 初始化）。虽然单次开销不大，但在回测循环内每步都调用时（`llm_client = get_llm()` 模式）会积累。回测功能本身已在按钮内用局部变量复用，但 Tab 3 的"获取 LLM 决策"每次点击都重建。

**修复方案：** 用 `@st.cache_resource` 缓存，或存入 `session_state`，按配置签名失效。

---

## 瓶颈 7 — 大型 JSONL 回测记录文件全量写入（保存时阻塞）

**严重程度：🟡 中**

**位置：** `web.py` L1450, `save_backtest_run()` L40–44

```python
# 回测结束时保存，decision_logs 包含每步完整 prompt 文本
run_record = {
    ...
    "decision_logs": llm_logs,   # 可能几百条，每条含完整 prompt 字符串
}
saved_path = save_backtest_run(run_record)
```

**问题：** 每次回测都会将带完整 prompt 的所有决策记录追加到 `backtest_runs.jsonl`（目前 1.21 MB / 10 次回测），随着测试次数增加文件持续膨胀，后续读取 `load_recent_backtest_runs()` 耗时增加。同时在 UI 主线程中同步写文件，写入大记录时会造成界面卡顿。

**修复方案：**
- 存储时截断超长 prompt（仅保留摘要），或单独存储 prompt 文件。
- 写文件操作移至线程（`threading.Thread`）或用 `st.spinner` 配合后台任务。

---

## 性能瓶颈汇总表

| # | 瓶颈描述 | 严重程度 | 触发时机 | 影响预估延迟 |
|---|---------|---------|---------|------------|
| 1 | 手动下单区无条件发起 3 次 API 请求 | 🔴 极高 | **每次 rerender** | +1.5s～6s |
| 2 | Tab 3 开头隐式触发交易所连接 | 🔴 高 | **每次 rerender** | +0.5s～3s |
| 3 | 全量读取 JSONL 文件（内存+解析） | 🔴 高 | **每次 rerender** | +0.2s～1s（随文件增长） |
| 4 | session 聚合计算未缓存 | 🟠 中高 | **每次 rerender** | +0.1s～0.5s |
| 5 | 自动刷新放大所有瓶颈 | 🟠 中高 | 每 5s 自动触发 | 乘数效应 |
| 6 | LLM Client 每次重建 | 🟡 中 | 点击按钮 | +100ms |
| 7 | 回测结果大文件阻塞写入 | 🟡 中 | 回测完成时 | +200ms～1s |

---

## 核心修复优先级建议

**立刻见效（1-2 天）：**
1. 把 L918 的 `fetch_ohlcv` 和 L953–957 的 `fetch_balance/fetch_positions` 移入按钮点击回调，或用 `@st.cache_data(ttl=30)` 包裹。
2. 把 `load_recent_live_runs` 和 `build_live_session_views` 加 `@st.cache_data(ttl=5)`。

**中期优化（1 周）：**
3. 用 `@st.cache_resource` 缓存 LLM Client，按配置签名失效。
4. JSONL 存储不保存完整 prompt，改为引用外部文件。
5. 评估是否需要自动刷新，或仅刷新状态 JSON 而非全页。
