# AGENTS.md — LLM Trading Bot 开发记录

## 环境

- **Python**: 3.13.9
- **Conda 环境**: `trade`
- **工作目录**: `D:\nof_newest`
- **启动方式**: `python -m app.start` (先启动 daemon 后台进程, 再启动 Streamlit Web 面板)

---

## 最近完成的工作 (2026-06-11)

### 1. 前端深色主题高可见度改造

**目标**: 保留深色背景, 大幅提升字体/按钮/标签的对比度

| 元素 | 旧色值 | 新色值 |
|------|--------|--------|
| 标题文字 | `#e6edf3` | `#ffffff` |
| Metric 标签 | `#6e7681` | `#a0aab4` |
| Metric 值 | `#e6edf3` | `#f0f6fc` |
| 按钮文字 | `#c9d1d9` | `#e6edf3` |
| 按钮边框 | `#30363d` | `#586069` |
| 按钮悬停色 | `#4dabf7` | `#58a6ff` |
| 输入框文字 | `#c9d1d9` | `#e6edf3` |
| 输入框边框 | `#30363d` | `#586069` |
| Caption | `#484f58` (几乎不可见) | `#8b949e` |
| Expander 头 | `#8b949e` | `#b0b8c0` |
| Tab 文字 | `#6e7681` | `#a0aab4` |
| Primary 按钮 | `#238636` | `#2ea043` |

**额外补充** (第二轮): 覆盖了 Streamlit 默认浅色背景组件:
- 全局字体强制浅色 (`body, p, span, div, label`)
- 输入框/下拉/单选/复选框标签全部浅色
- 下拉菜单 popover 暗色背景
- 数据表格 th/td 暗色背景 + 浅色文字
- 代码块 / JSON 块暗色背景
- 进度条 / Spinner 适配深色主题

### 2. 全界面中文化

5 个标签页、约 140+ 处 UI 文本全部翻译为中文:
- 标签页名: `Balance→账户`, `Chart→图表`, `Trade→交易`, `Backtest→回测`, `Settings→设置`
- 按钮/输入框标签/指标标题/提示信息/图表图例/hover 模板 全部中文
- `parse_decision` 错误路径的 `print()` 保持原样 (后续应改为 logger)

### 3. daemon ↔ web.py IPC 直连通信

**问题**: daemon 和 web.py 之前通过 JSON 文件读写通信, 存在并发读写风险、轮询延迟 (1-5s)、无双向通信。

**方案**: 使用 `multiprocessing.connection` (Python stdlib) 实现 localhost:6000 双向通信

**新建文件**: `app/ipc.py`
- `DaemonSharedState` — 线程安全的共享状态 (threading.Lock)
- `IpcServer` — daemon 端 daemon 线程, `Listener.accept()` 循环
- `ipc_request()` — 客户端发一条命令、收一条响应
- `ipc_ping()` / `ipc_is_alive()` — 健康检查

**协议**:
```
客户端 → 服务端  {"action": "start|stop|status|ping", ...}
服务端 → 客户端  {"ok": True, pid:..., state:..., ...}
```

**修改文件**:
| 文件 | 变更 |
|------|------|
| `app/daemon.py` | 启动 `IpcServer`; 主循环检查 IPC 命令队列 (优先级高于文件); `sync_state()` 同时更新共享状态 + 写文件 |
| `app/daemon_runtime.py` | `load_daemon_status()` IPC 优先/文件兜底; `start_daemon_process()` 等待 IPC 就绪 |
| `app/web.py` | Daemon Control 区用 `_ipc_status/start/stop()` 发送 IPC 命令; 失败自动回退文件; Caption 显示当前状态来源 |

**向后兼容**: IPC 不可用时自动回退到文件模式。

**接口**: 本地 `localhost:6000`, 无外部端口暴露。

**零依赖**: 所有新增代码仅使用 Python 标准库, requirements.txt 无需修改。

---

## 工程提升点分析 (代码审查)

### 架构层面

| # | 问题 | 影响 |
|---|------|------|
| 1 | `web.py` 1868 行巨石文件: UI、业务逻辑、缓存、数据访问混在一起 | 维护困难, 改一处可能影响整个页面 |
| 2 | 交易决策逻辑重复: `daemon.py:90` `run_live_trade_step()` 和 `web.py:1322` 回测循环的买/卖/持仓逻辑完全重复 | 改一处忘记另一处 → bug |
| 3 | `backtester.py` 是死代码: 模块保留但 `web.py` 从不调用 | 混淆、浪费 |

### 可靠性层面

| # | 问题 | 影响 |
|---|------|------|
| 4 | LLM 调用没有 timeout/retry/exponential backoff | 一次超时就死 |
| 5 | Daemon 无法真正停止: 只能设置 `enabled=False`, 进程不退出, 无 SIGTERM 处理 | 只能 kill PID |
| 6 | 零单元测试: 项目无任何 `test_*.py` | 改动无保障 |
| 7 | `fetch_ohlcv_range()` 无最大迭代次数上限 | 理论上可无限循环 |

### 代码质量

| # | 问题 | 文件:行 |
|---|------|---------|
| 8 | `parse_decision` 用 `print` 而非 `logger` | `strategy/llm_strategy.py:71` |
| 9 | `fetch_balance` 异常静默吞掉 (spot 余额获取失败无日志) | `exchange/hyperliquid_client.py:140` |
| 10 | LLM 工厂方法 `create_llm_client(cfg)` 参数无类型标注 | `llm/__init__.py:6` |

### 可观测性

| # | 问题 |
|---|------|
| 11 | 无结构化日志 (所有 log 是字符串拼接, 无 context) |
| 12 | 无任何指标 (LLM 延迟、API 调用耗时、错误率) |

### 安全

| # | 问题 |
|---|------|
| 13 | `config.json` 明文存私钥, 未支持环境变量覆盖 |

### 建议优先级

| 优先级 | 事项 |
|--------|------|
| P0 | LLM 调用加 retry + timeout |
| P1 | 拆分 `web.py` 为多模块 |
| P1 | 提取共用交易逻辑消除 daemon/web 重复 |
| P1 | 加单元测试 |
| P2 | 私钥支持环境变量 |
| P2 | 结构化日志 + 指标 |

---

## 项目结构

```
nof_newest/
├── app/
│   ├── __init__.py
│   ├── config.py              # Pydantic 配置模型与读写
│   ├── daemon.py              # 后台自动交易守护进程 (含 IPC 集成)
│   ├── daemon_runtime.py      # daemon 进程生命周期管理 (IPC 优先)
│   ├── ipc.py                 # [NEW] daemon-web IPC 通信 (multiprocessing.connection)
│   ├── start.py               # 统一入口
│   ├── web.py                 # Streamlit Web 面板 (中文化 + 深色主题)
│   ├── backtest/
│   │   └── backtester.py      # 简易动量回测器 (死代码, 未被 web 调用)
│   ├── exchange/
│   │   ├── base.py
│   │   └── hyperliquid_client.py
│   ├── llm/
│   │   ├── base.py
│   │   ├── deepseek_client.py
│   │   └── openai_client.py
│   ├── strategy/
│   │   └── llm_strategy.py    # Prompt 构建与解析
│   └── utils/
│       ├── io.py
│       ├── logger.py
│       └── snapshot.py
├── config.json
├── daemon_status.json         # daemon 运行时状态 (仍写入, 向后兼容)
├── requirements.txt
└── AGENTS.md
```

## 下次续做建议

1. 以上 "工程提升点" 中按优先级逐步实施
2. 如遇 daemon 通信问题, 检查 `localhost:6000` 端口是否被占用; 可修改 `app/ipc.py` 中 `DEFAULT_IPC_PORT`
3. `backtester.py` 中的 `run_backtest` 函数可在回测 Tab 中作为"简易动量回测"选项暴露
