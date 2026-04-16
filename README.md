# LLM Trading Bot

基于 Hyperliquid + LLM 的 Streamlit 交易面板。

保留了：

- Streamlit Web UI
- daemon 后台自动交易
- LLM 决策与历史回测
- 原有数据文件读写方式

同时修复了原版 Web 页面的主要性能问题，重点是避免页面渲染时无条件访问交易所 API。

## 主要变化

- 删除 CLI 入口，**不再包含 `app/main.py`**
- `requirements.txt` 移除了 `rich`
- `app/config.py` 删除了 CLI 专属的 `app.mode`
- `app/web.py` 完成性能重构：
  - 手动下单区改为显式刷新，不再切换页面就请求 API
  - live/backtest JSONL 读取增加缓存
  - LLM client 改为缓存复用
  - 自动刷新只读取 `daemon_status.json`
  - 回测结果只持久化 `prompt_preview`，不保存完整 prompt

## 目录结构

```text
llm-trading-bot/
├── app/
│   ├── config.py
│   ├── daemon.py
│   ├── web.py
│   ├── exchange/
│   ├── llm/
│   ├── strategy/
│   ├── backtest/
│   └── utils/
├── config.json
├── requirements.txt
└── README.md
```

## 环境要求

- Python 3.10+
- Windows / Linux / macOS

## 安装依赖

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
