from __future__ import annotations

import json
from pathlib import Path
from pydantic import BaseModel, Field


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.json"


class AppConfig(BaseModel):
    log_level: str = "INFO"
    # NOTE: `mode` field removed — CLI interaction has been removed from this project.


class LLMApiOption(BaseModel):
    name: str
    base_url: str
    api_key: str
    model: str
    temperature: float = 0.2


class OpenAIConfig(BaseModel):
    provider: str = "openai"   # openai | deepseek
    api_key: str
    base_url: str = "https://api.openai.com/v1"
    model: str = "gpt-4o-mini"
    temperature: float = 0.2
    saved_apis: list[LLMApiOption] = Field(default_factory=list)


class ExchangeConfig(BaseModel):
    name: str = "hyperliquid"
    api_key: str
    secret: str
    sandbox: bool = False        # True = 测试网；False = 主网（真实资金）
    base_url: str | None = None  # 仅在 sandbox=False 时生效，用于自定义私有节点
    account_address: str | None = None  # 主账户地址（查询余额/仓位）
    wallet_address: str | None = None
    symbol: str = "BTC/USDC"
    timeframe: str = "5m"
    max_candles: int = 200
    market_order_slippage: float = Field(default=0.05, ge=0.0, le=1.0)


class TradingConfig(BaseModel):
    usd_notional: float = 20
    max_position: float = 0.002
    min_trade_notional: float = 5


class BacktestConfig(BaseModel):
    start_ms: int = 0
    end_ms: int = 0
    initial_usdc: float = 1000


class PromptConfig(BaseModel):
    template: str = (
        "You are a trading assistant. Return ONLY a pure JSON object. "
        "NO markdown formatting, NO explanations. Fields must be: "
        "action (buy/sell/hold), confidence (0-1), reason (string). "
        "Symbol: {symbol}. Recent OHLCV:\n{ohlcv_csv}"
    )
    kline_count: int = 20


class Config(BaseModel):
    app: AppConfig
    openai: OpenAIConfig
    exchange: ExchangeConfig
    trading: TradingConfig
    backtest: BacktestConfig
    prompt: PromptConfig = PromptConfig()


def load_config(config_path: str | Path = DEFAULT_CONFIG_PATH) -> Config:
    path = Path(config_path)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    data = json.loads(path.read_text(encoding="utf-8"))
    # Tolerate legacy config.json that still contains the removed `mode` field
    data.get("app", {}).pop("mode", None)
    return Config(**data)


def save_config(config: Config, config_path: str | Path = DEFAULT_CONFIG_PATH) -> None:
    path = Path(config_path)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    path.write_text(
        json.dumps(config.model_dump(mode="json"), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
