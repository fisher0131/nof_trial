# LLM Trading Bot

基于 Hyperliquid 交易所的 LLM 驱动自动化交易系统，提供 Streamlit Web 面板、后台 daemon 自动交易与历史回测功能。

## 项目结构

```
nof_newest/
├── app/
│   ├── __init__.py
│   ├── config.py              # Pydantic 配置模型与读写
│   ├── daemon.py              # 后台自动交易守护进程
│   ├── daemon_runtime.py      # daemon 进程生命周期管理
│   ├── start.py               # 统一入口：启动 daemon + Streamlit
│   ├── web.py                 # Streamlit Web 面板（5 个标签页）
│   ├── backtest/
│   │   ├── __init__.py
│   │   └── backtester.py      # 简易动量回测器（预留）
│   ├── exchange/
│   │   ├── __init__.py
│   │   ├── base.py            # ExchangeClient 抽象基类
│   │   └── hyperliquid_client.py  # Hyperliquid 交易所实现（ccxt）
│   ├── llm/
│   │   ├── __init__.py        # LLM 客户端工厂
│   │   ├── base.py            # LLMClient 抽象基类
│   │   ├── deepseek_client.py # DeepSeek 客户端（OpenAI 兼容接口）
│   │   └── openai_client.py   # OpenAI 原生客户端
│   ├── strategy/
│   │   ├── __init__.py
│   │   └── llm_strategy.py    # Prompt 构建与 LLM 响应解析
│   └── utils/
│       ├── __init__.py
│       ├── logger.py          # 日志配置
│       ├── io.py              # JSON 文件安全读写
│       └── snapshot.py        # 账户快照提取
├── config.json                # 主配置文件
├── daemon_status.json         # daemon 运行时状态
├── implementation_plan.md     # 重构实施方案
├── performance_analysis.md    # 性能瓶颈分析报告
├── README.md
└── requirements.txt
```

**运行时生成的数据文件：**

| 文件 | 说明 |
|------|------|
| `live_runs.jsonl` | 实时交易每轮记录（追加写入） |
| `backtest_runs.jsonl` | 回测结果记录（追加写入） |
| `daemon_control.json` | daemon 启停与间隔控制 |

## 环境要求

- Python 3.10+
- Windows / Linux / macOS

## 安装

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -U pip
python -m pip install -r requirements.txt
```

## 配置

编辑 `config.json`，关键字段如下：

```jsonc
{
  "openai": {
    "provider": "deepseek",        // "deepseek" 或 "openai"
    "api_key": "sk-...",
    "base_url": "https://api.deepseek.com",
    "model": "deepseek-chat",
    "temperature": 0.5
  },
  "exchange": {
    "sandbox": true,               // true = 测试网, false = 主网
    "api_key": "0x...",
    "secret": "0x...",
    "account_address": "0x...",
    "wallet_address": "0x...",
    "symbol": "BTC/USDC:USDC",
    "timeframe": "5m",
    "max_candles": 200,
    "market_order_slippage": 0.05  // 5% 滑点保护
  },
  "trading": {
    "usd_notional": 20.0,          // 单笔名义金额
    "max_position": 0.5,           // 最大仓位比例
    "min_trade_notional": 5.0      // 最小交易金额
  },
  "backtest": {
    "initial_usdc": 1000.0         // 回测初始资金
  },
  "prompt": {
    "template": "...{symbol}...{ohlcv_csv}...{account_snapshot_json}...",
    "kline_count": 60              // 传入 LLM 的 K 线数量
  }
}
```

配置说明：
- `exchange.account_address` 填写主账户地址
- `exchange.api_key` 和 `exchange.secret` 用于签名下单
- `sandbox=true` 时，订单发送到测试网，行情数据从主网拉取（测试网无真实 K 线）
- Prompt 模板需包含 `{symbol}`、`{ohlcv_csv}`、`{account_snapshot_json}` 占位符

## 启动

### 统一启动（推荐）

```powershell
cd D:\nof_newest
python -m app.start
```

该命令先启动 daemon 后台进程，再启动 Streamlit Web 面板。访问 http://localhost:8501。

### 单独启动 daemon

```powershell
python -m app.daemon
```

daemon 通过文件与 Web 面板协同：
- `daemon_control.json` — 面板写入启停与间隔指令
- `daemon_status.json` — daemon 写入心跳、状态快照

### 单独启动 Web 面板

```powershell
streamlit run app/web.py
```

Web 面板启动时会自动检测 daemon，如未运行则尝试拉起。

## Web 面板功能

| 标签页 | 功能 |
|--------|------|
| 账户余额 | 手动刷新查询余额与仓位 |
| K 线图表 | 拉取指定周期和数量的 K 线，Plotly 蜡烛图 |
| LLM 决策下单 | 获取 LLM 决策并执行交易；手动测试下单；daemon 启停控制 |
| 历史回测 | 基于历史 K 线驱动 LLM 决策回测，展示权益曲线与交易标记 |
| 全局 Prompt 设置 | 在线修改 API、模型、Temperature、Prompt 模板；管理多个 API 配置 |

## 架构设计

### 交易决策流程

```
OHLCV + 账户快照 → Prompt 模板 → LLM 推理 → JSON 解析 → 市价单执行
```

LLM 接收近 N 根 K 线数据（CSV 格式）和当前账户快照（JSON 格式），返回结构化决策：
- `action`：buy / sell / hold
- `position_pct`：仓位比例（0.0 ~ 1.0）
- `confidence`：置信度（0.0 ~ 1.0）
- `reason`：决策理由

### 性能优化

相较于原版本，本重构版修复了 7 个关键性能瓶颈：

1. 手动下单区改为显式按钮刷新，不再每次渲染访问交易所 API
2. K 线拉取移至按钮回调，避免 Tab 切换时阻塞
3. JSONL 文件读取增加 `@st.cache_data` 缓存（TTL 5 ~ 30 秒）
4. LLM 客户端通过 `@st.cache_resource` 缓存复用
5. 自动刷新仅读取 `daemon_status.json`（本地小文件），不触发网络请求
6. 回测结果仅持久化 `prompt_preview`（前 200 字符），不保存完整 prompt
7. 消除重复的数据处理与序列化

## 免责声明

本项目仅供学习和研究使用，不构成任何投资建议。数字货币交易存在极高风险，实盘操作请自行承担后果。
