from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class ExchangeClient(ABC):
    @abstractmethod
    def connect(self) -> None:
        raise NotImplementedError

    @abstractmethod
    def fetch_balance(self) -> dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int) -> list[list[float]]:
        raise NotImplementedError

    @abstractmethod
    def create_market_order(self, symbol: str, side: str, amount: float) -> dict[str, Any]:
        raise NotImplementedError
