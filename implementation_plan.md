# 重构实施方案 — LLM Trading Bot

## 目标

在 `d:\nof-Aliyun_edition-20260311-refactored` 新目录中重构整个项目，同时：
1. **删除 CLI 交互**（移除 `main.py` 和 `rich` 依赖），保留底层接口不变
2. **修复所有 7 个性能瓶颈**，消除 Streamlit 页面的卡顿
3. **功能完整保留**：Streamlit Web UI、daemon 后台进程、所有数据读写

---

## User Review Required

> [!IMPORTANT]
> 新目录名定为 `d:\nof-Aliyun_edition-20260311-refactored`，将同时复制 `config.json`（含真实 API key/secret），请确认是否可接受。

> [!WARNING]
> `config.json` 中含有明文 API key 和私钥，已存在于原项目，复制时无额外安全风险，但重构后请注意文件权限。

---

## Proposed Changes

### 新目录结构

```
nof-Aliyun_edition-20260311-refactored/
├── app/
│   ├── __init__.py
│   ├── config.py              [MODIFY] 移除 AppConfig.mode（CLI专属字段）
│   ├── daemon.py              [COPY]   基本不变
│   ├── backtest/
│   │   ├── __init__.py
│   │   └── backtester.py      [COPY]   不变
│   ├── exchange/
│   │   ├── __init__.py
│   │   ├── base.py            [COPY]   不变（接口保留）
│   │   └── hyperliquid_client.py [COPY] 不变
│   ├── llm/
│   │   ├── __init__.py        [COPY]   不变（接口保留）
│   │   ├── base.py            [COPY]   不变（接口保留）
│   │   ├── deepseek_client.py [COPY]   不变
│   │   └── openai_client.py   [COPY]   不变
│   ├── strategy/
│   │   ├── __init__.py
│   │   └── llm_strategy.py    [COPY]   不变
│   ├── utils/
│   │   ├── __init__.py
│   │   └── logger.py          [COPY]   不变
│   └── web.py                 [MODIFY] 重构：修复所有性能瓶颈
├── config.json                [COPY]
└── requirements.txt           [MODIFY] 移除 rich
```

> [!NOTE]
> `main.py` (CLI) **不复制到新目录**。`rich` 从 requirements.txt 移除

---

### 核心文件改动

---

#### [MODIFY] web.py — 修复所有 7 个性能瓶颈

**瓶颈 1 & 2 修复 — Tab 3 手动下单区每次渲染发起 API 请求**

将以下代码从渲染流移入按钮点击回调：
```python
# 原代码（渲染时无条件执行）— 移除
manual_ohlcv = get_exchange().fetch_ohlcv(...)   # L918
bal = get_exchange().fetch_balance()              # L953
pos = get_exchange().fetch_positions(...)          # L956

# 改为：仅在用户点击"刷新"时触发，结果缓存入 session_state
```

同时，Tab 3 开头的 `ex = get_exchange()` 不再无条件显式触发连接，改为只在需要时惰性连接。

**瓶颈 3 & 4 修复 — JSONL 全量读取 + session 聚合未缓存**

```python
# 为以下函数添加 @st.cache_data，TTL 5s
@st.cache_data(ttl=5)
def load_recent_live_runs(limit: int = 100) -> list[dict]: ...

# 同样缓存 backtest runs 读取
@st.cache_data(ttl=30)
def load_recent_backtest_runs(limit: int = 20) -> list[dict]: ...
```

`build_live_session_views()` 的结果在每次读取结果变化时才重计算（利用 cache_data 的参数哈希）。

**瓶颈 5 修复 — 自动刷新放大所有瓶颈**

自动刷新的路径仅读取 `daemon_status.json`（纯本地文件，约 1KB），不触发任何 API 请求。通过 `st.session_state` 标记区分"自动刷新"和"用户主动操作"，仅在后者时才允许 API 调用路径执行。

**瓶颈 6 修复 — LLM Client 每次重建**

```python
# 改为 @st.cache_resource，按配置签名失效
@st.cache_resource
def get_llm_client(provider, base_url, api_key, model, temperature):
    return create_llm_client(...)
```

**瓶颈 7 修复 — 回测大文件写入阻塞**

保存回测时，`decision_logs` 中每条记录的 `prompt` 字段仅保留前 200 字符摘要（`prompt_preview`），完整 prompt 不持久化。

---

#### [MODIFY] config.py

移除 `AppConfig.mode`（`"interactive"` 模式标识，CLI 专属）：

```python
class AppConfig(BaseModel):
    log_level: str = "INFO"
    # mode 字段已删除（CLI 专属，不再需要）
```

---

#### [MODIFY] requirements.txt

```diff
 ccxt>=4.5.40
 openai>=2.0.0
 pydantic>=2.10.6
-rich>=13.9.4
 streamlit>=1.32.0
 plotly>=5.20.0
 pandas>=2.0.0
```

---

## Open Questions

无需用户决策的开放问题。以下列出供参考：

- `backtest/backtester.py` 中的简单动量回测逻辑与 web.py 中的 LLM 驱动回测是两套独立逻辑，前者仅被 CLI `main.py` 使用。重构后将**保留模块文件（接口不变）但 web.py 不调用它**，仍可供未来扩展使用。

---

## Verification Plan

### 检查项

1. 新目录结构完整，无遗漏文件
2. `web.py` import 路径正确（仍是 `from app.xxx import ...`）
3. `requirements.txt` 中无 `rich`
4. `config.json` 正确复制

### 手动验证（用户操作）

```bash
cd d:\nof-Aliyun_edition-20260311-refactored
streamlit run app/web.py
```

打开浏览器后验证：
- [ ] 侧边栏"连接交易所"按钮可用
- [ ] Tab 1 余额刷新正常
- [ ] Tab 2 K线拉取正常
- [ ] Tab 3 手动下单区无卡顿（切换 tab 不再触发 API 请求）
- [ ] Tab 3 实时交易 daemon 启停正常
- [ ] Tab 4 历史回测可运行
- [ ] Tab 5 Prompt 设置可保存
