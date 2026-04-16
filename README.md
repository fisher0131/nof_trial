# NOF Trading Bot

基于 **Hyperliquid** 交易所与 **LLM（大语言模型）** 驱动决策的自动化加密货币交易系统，提供 Streamlit Web 控制面板。

## 功能特性

- **LLM 驱动决策** — 支持 OpenAI / DeepSeek 等多种模型，通过市场数据 Prompt 生成交易信号
- **Daemon 后台自动交易** — 独立后台进程按设定周期自动执行策略
- **Streamlit Web 面板** — 实时查看持仓、运行记录、手动下单、配置回测
- **历史回测** — 基于 JSONL 日志对 LLM 决策进行回测分析
- **性能优化** — 页面渲染不触发 API 请求，数据读取带缓存，LLM client 复用

## 目录结构

```text
nof-refactored/
├── app/
│   ├── config.py         # 配置模型（Pydantic）
│   ├── daemon.py         # 后台自动交易进程
│   ├── web.py            # Streamlit Web 面板
│   ├── exchange/         # 交易所接口（Hyperliquid / CCXT）
│   ├── llm/              # LLM 客户端（OpenAI / DeepSeek）
│   ├── strategy/         # 策略逻辑
│   ├── backtest/         # 回测模块
│   └── utils/            # 日志工具
├── config.json           # 运行配置（含 API 密钥，不提交 Git）
├── config.example.json   # 配置模板
├── requirements.txt
└── README.md
```

## 环境要求

- Python 3.10+
- Windows / Linux / macOS

## 安装

```bash
pip install -r requirements.txt
```

依赖包：`ccxt`、`openai`、`pydantic`、`streamlit`、`plotly`、`pandas`

## 配置

复制 `config.example.json` 为 `config.json`，填入以下信息：

```jsonc
{
  "exchange": {
    "api_key": "YOUR_HYPERLIQUID_API_KEY",
    "secret":  "YOUR_PRIVATE_KEY"
  },
  "llm": {
    "provider": "openai",        // openai 或 deepseek
    "api_key":  "YOUR_LLM_KEY",
    "model":    "gpt-4o-mini"
  }
}
```

> `config.json` 已加入 `.gitignore`，不会被提交到版本库。

## 使用

**启动 Web 面板：**
```bash
streamlit run app/web.py
```

**启动后台 Daemon：**
```bash
python -m app.daemon
```

Daemon 运行状态写入 `daemon_status.json`，Web 面板自动读取展示。

## 数据文件

| 文件 | 说明 |
|------|------|
| `live_runs.jsonl` | 实盘每轮决策记录 |
| `backtest_runs.jsonl` | 回测结果记录 |
| `daemon_status.json` | Daemon 当前状态 |
| `daemon_control.json` | Daemon 控制指令 |

以上文件均在运行时自动生成，已加入 `.gitignore`。

Windows PowerShell:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -U pip
python -m pip install -r requirements.txt
```

如果你使用 Conda，也可以直接在目标环境里执行：

```powershell
python -m pip install -r requirements.txt
```

## 配置

主配置文件是 `config.json`。

关键字段：

```jsonc
{
  "openai": {
    "provider": "deepseek", // deepseek | openai
    "api_key": "sk-...",
    "base_url": "https://api.deepseek.com",
    "model": "deepseek-chat",
    "temperature": 0.5
  },
  "exchange": {
    "sandbox": true,
    "api_key": "0x...",
    "secret": "0x...",
    "account_address": "0x...",
    "wallet_address": "0x...",
    "symbol": "BTC/USDC:USDC",
    "timeframe": "5m",
    "max_candles": 200
  },
  "prompt": {
    "template": "...{symbol}...{ohlcv_csv}...{account_snapshot_json}...",
    "kline_count": 60
  }
}
```

配置说明：

- `exchange.account_address` 应填写主账户地址
- `exchange.api_key + exchange.secret` 用于签名下单
- `sandbox=false` 表示主网真实资金
- Prompt 模板建议包含 `{symbol}`、`{ohlcv_csv}`、`{account_snapshot_json}`

## 启动方式

### 1. 启动 Web

```powershell
cd llm-trading-bot
streamlit run app/web.py
```

默认访问地址：

```text
http://localhost:8501
```

### 2. 启动 daemon

```powershell
cd llm-trading-bot
python -m app.daemon
```

daemon 通过以下文件协同工作：

- `daemon_control.json`：启停与间隔控制
- `daemon_status.json`：心跳、最近状态、最近快照

## Web 页面说明

### 1. 账户余额

- 手动点击刷新后查询余额与仓位

### 2. K 线图表

- 手动拉取指定周期和数量的 K 线

### 3. LLM 决策下单

- 获取最新行情并请求 LLM 决策
- 支持手动测试下单
- 支持控制 daemon 实时交易

### 4. 历史回测

- 使用历史 K 线驱动 LLM 决策回测
- 保存回测摘要与决策日志

### 5. Prompt / LLM 配置

- 在线修改 API、模型、temperature、Prompt 模板

## 数据文件

- `live_runs.jsonl`：实时交易每轮记录
- `backtest_runs.jsonl`：回测结果
- `daemon_control.json`：daemon 控制文件
- `daemon_status.json`：daemon 状态文件

## 已知运行前提

项目代码已经重构完成，但运行前仍需要保证当前 Python 环境里至少安装了：

- `ccxt`
- `streamlit`
- `openai`
- `pydantic`
- `plotly`
- `pandas`

如果缺少依赖，`streamlit run app/web.py` 会直接失败。

## 手动验证建议

启动后重点检查：

1. 侧边栏“连接交易所”是否可用
2. 余额刷新是否正常
3. K 线拉取是否正常
4. Tab 3 切换时是否不再卡顿
5. daemon 启停是否正常
6. 历史回测是否可运行
7. Prompt 设置是否可保存

## 安全提示

- `config.json` 含 API key / 私钥时，不要再提交到公共仓库
- 建议先在测试网验证
- 主网模式会使用真实资金

## 免责声明

本项目仅用于学习和研究，不构成投资建议。交易有风险，实盘请谨慎。
