from __future__ import annotations

import logging
import math
import warnings
from datetime import datetime, timedelta, timezone
from typing import Any

import ccxt

from app.exchange.base import ExchangeClient

logger = logging.getLogger(__name__)
GMT8 = timezone(timedelta(hours=8))
HYPERLIQUID_TESTNET_API_URL = "https://api.hyperliquid-testnet.xyz"


class HyperliquidClient(ExchangeClient):
    def __init__(
        self,
        api_key: str,
        secret: str,
        sandbox: bool = False,
        base_url: str | None = None,
        account_address: str | None = None,
        wallet_address: str | None = None,
        market_order_slippage: float = 0.05,
    ) -> None:
        self.api_key = api_key
        self.secret = secret
        self.sandbox = sandbox
        normalized_base_url = (base_url or "").strip()
        self.base_url = normalized_base_url or None
        self.signing_wallet_address = wallet_address or api_key
        # Hyperliquid 文档语义：
        # - account_address: 主账户地址（用于余额/仓位查询）
        # - api_key + secret: API wallet 地址与私钥（用于签名下单）
        self.account_address = account_address or wallet_address or api_key
        self.wallet_address = self.signing_wallet_address
        self.market_order_slippage = max(0.0, min(1.0, float(market_order_slippage)))
        self.exchange = None       # 交易所（sandbox 时指向测试网，用于下单/余额）
        self._mkt_exchange = None  # 行情专用（始终指向主网，获取真实 OHLCV）

    def _ensure_connected(self) -> None:
        if self.exchange is None or self._mkt_exchange is None:
            self.connect()

    def connect(self) -> None:
        if not self.sandbox:
            # ⚠️ 主网模式：操作真实资金，任何下单均不可撤销
            warnings.warn(
                "[MAINNET] sandbox=False — 当前连接主网，操作将使用真实资金！",
                stacklevel=2,
            )
            logger.warning("[MAINNET] 当前连接主网，操作将使用真实资金！")

        # ── 1. 交易所实例（下单 / 余额） ──────────────────────────────────────
        if not self.signing_wallet_address:
            raise ValueError("缺少 walletAddress：请在 config.exchange.wallet_address 或 api_key 中提供签名地址")
        if not self.secret:
            raise ValueError("缺少 privateKey：请在 config.exchange.secret 中提供签名私钥")

        self.exchange = ccxt.hyperliquid({
            "apiKey": self.api_key,
            "secret": self.secret,
            "privateKey": self.secret,
            "walletAddress": self.signing_wallet_address,
            "enableRateLimit": True,
        })

        if self.sandbox:
            # 使用 ccxt 内置沙盒模式切换到测试网
            self.exchange.set_sandbox_mode(True)
            # 兼容部分 ccxt 版本：sandbox URL 可能为 None，后续请求会触发 None + str
            api_urls = self.exchange.urls.get("api") if isinstance(self.exchange.urls, dict) else None
            if isinstance(api_urls, dict):
                public_url = str(api_urls.get("public") or "").strip()
                private_url = str(api_urls.get("private") or "").strip()
                if not public_url or not private_url:
                    self.exchange.urls["api"] = {
                        "public": HYPERLIQUID_TESTNET_API_URL,
                        "private": HYPERLIQUID_TESTNET_API_URL,
                    }
            elif not api_urls:
                self.exchange.urls["api"] = {
                    "public": HYPERLIQUID_TESTNET_API_URL,
                    "private": HYPERLIQUID_TESTNET_API_URL,
                }
        elif self.base_url:
            # 非 sandbox 时才允许使用自定义节点（如企业私有节点）
            self.exchange.urls["api"] = {
                "public": self.base_url,
                "private": self.base_url,
            }

        # walletAddress 用于签名（API wallet）；放在 options 兼容部分旧版本 ccxt 读取路径
        self.exchange.options["walletAddress"] = self.signing_wallet_address
        self.exchange.options["defaultSlippage"] = str(self.market_order_slippage)
        # Hyperliquid testnet 的 spot meta 可能包含异常 token，限制为 swap 可规避 ccxt 的 None + str 问题。
        self.exchange.options["fetchMarkets"] = {"types": ["swap"]}
        self.exchange.load_markets()

        # ── 2. 行情实例（始终主网，获取真实历史 K 线） ─────────────────────────
        # Hyperliquid 测试网无真实行情，OHLCV 全为占位符，必须从主网拉取
        if self.sandbox:
            self._mkt_exchange = ccxt.hyperliquid({"enableRateLimit": True})
            self._mkt_exchange.options["fetchMarkets"] = {"types": ["swap"]}
            self._mkt_exchange.load_markets()
            logger.info("行情数据将从主网公共 API 获取（测试网无真实行情）")
        else:
            self._mkt_exchange = self.exchange

    def fetch_balance(self) -> dict:
        self._ensure_connected()
        # 现金口径：优先读取 spot USDC.free；失败时回退 withdrawable
        # 保证金/净值口径：clearinghouseState.marginSummary
        state = self.fetch_user_state()
        ms = state.get("marginSummary", {})
        timestamp = state.get("time")

        perp_withdrawable = self._to_float(state.get("withdrawable", 0))
        cash_usdc = perp_withdrawable
        spot_cash_usdc = 0.0
        spot_cash_ok = False
        try:
            spot_params = {"type": "spot"}
            if self.account_address:
                spot_params["user"] = self.account_address
            spot_balance = self.exchange.fetch_balance(spot_params)
            spot_usdc = (spot_balance.get("USDC", {}) or {}) if isinstance(spot_balance, dict) else {}
            spot_cash_usdc = self._to_float(
                spot_usdc.get(
                    "free",
                    spot_balance.get("free", {}).get("USDC", 0) if isinstance(spot_balance, dict) else 0,
                )
            )
            if spot_cash_usdc >= 0:
                cash_usdc = spot_cash_usdc
                spot_cash_ok = True
        except Exception:
            spot_cash_usdc = 0.0

        position_value = self._to_float(ms.get("totalNtlPos", 0))
        equity = self._to_float(ms.get("accountValue", 0))
        margin_used = self._to_float(ms.get("totalMarginUsed", 0))

        dt_gmt8 = (
            datetime.fromtimestamp(int(timestamp) / 1000, tz=timezone.utc)
            .astimezone(GMT8)
            .strftime("%Y-%m-%d %H:%M:%S %Z")
            if timestamp is not None
            else None
        )

        # 尽量保持 ccxt fetch_balance 返回结构，兼容现有调用方
        return {
            "info": state,
            "USDC": {"free": cash_usdc, "used": margin_used, "total": equity},
            "free": {"USDC": cash_usdc},
            "used": {"USDC": margin_used},
            "total": {"USDC": equity},
            "timestamp": int(timestamp) if timestamp is not None else None,
            "datetime": dt_gmt8,
            "positionValue": {"USDC": position_value},
            "withdrawable": {"USDC": cash_usdc},
            "spotCash": {"USDC": spot_cash_usdc, "ok": spot_cash_ok},
            "perpWithdrawable": {"USDC": perp_withdrawable},
        }

    def fetch_user_state(self) -> dict[str, Any]:
        self._ensure_connected()
        payload = {"type": "clearinghouseState", "user": self.account_address}
        # 兼容不同 ccxt 命名
        if hasattr(self.exchange, "public_post_info"):
            return self.exchange.public_post_info(payload)
        return self.exchange.publicPostInfo(payload)

    def fetch_positions(self, symbol: str | None = None) -> list[dict]:
        self._ensure_connected()
        try:
            params = {}
            if self.account_address:
                params["user"] = self.account_address
            if symbol:
                return self.exchange.fetch_positions([symbol], params)
            return self.exchange.fetch_positions(None, params)
        except Exception:
            # 回退到原始 user_state，避免因解析失败导致上层无法展示仓位
            state = self.fetch_user_state()
            rows = state.get("assetPositions", []) or []
            if not symbol:
                return rows

            symbol_upper = symbol.upper()
            filtered: list[dict] = []
            for row in rows:
                pos = row.get("position", {}) if isinstance(row, dict) else {}
                coin = str(pos.get("coin", "")).upper()
                if coin and coin in symbol_upper:
                    filtered.append(row)
            return filtered

    @staticmethod
    def _to_float(value: Any) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int) -> list[list[float]]:
        self._ensure_connected()
        return self._mkt_exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)

    def fetch_ohlcv_range(
        self,
        symbol: str,
        timeframe: str,
        since_ms: int,
        end_ms: int,
        limit: int = 200,
    ) -> list[list[float]]:
        self._ensure_connected()
        all_rows: list[list[float]] = []
        since = since_ms

        while True:
            batch = self._mkt_exchange.fetch_ohlcv(
                symbol, timeframe=timeframe, since=since, limit=limit
            )
            if not batch:
                break

            all_rows.extend(batch)
            last_ts = batch[-1][0]
            if end_ms and last_ts >= end_ms:
                break

            if len(batch) < limit:
                break

            since = last_ts + 1

        if end_ms:
            all_rows = [row for row in all_rows if row[0] <= end_ms]

        return all_rows

    def _resolve_market_order_price(self, symbol: str, side: str) -> float:
        ticker = self.exchange.fetch_ticker(symbol)

        raw_price = None
        side_lower = str(side).lower()
        if side_lower == "buy":
            raw_price = ticker.get("ask") or ticker.get("last") or ticker.get("close") or ticker.get("bid")
        elif side_lower == "sell":
            raw_price = ticker.get("bid") or ticker.get("last") or ticker.get("close") or ticker.get("ask")
        else:
            raw_price = ticker.get("last") or ticker.get("close") or ticker.get("ask") or ticker.get("bid")

        price = self._to_float(raw_price)
        if not math.isfinite(price) or price <= 0:
            book = self.exchange.fetch_order_book(symbol, limit=1)
            side_lower = str(side).lower()
            if side_lower == "buy":
                asks = book.get("asks", []) if isinstance(book, dict) else []
                if asks and isinstance(asks[0], (list, tuple)) and len(asks[0]) > 0:
                    price = self._to_float(asks[0][0])
            elif side_lower == "sell":
                bids = book.get("bids", []) if isinstance(book, dict) else []
                if bids and isinstance(bids[0], (list, tuple)) and len(bids[0]) > 0:
                    price = self._to_float(bids[0][0])

        if not math.isfinite(price) or price <= 0:
            raise ValueError(f"无法为市价单解析有效价格: symbol={symbol}, side={side}, ticker={ticker}")
        return price

    def create_market_order(self, symbol: str, side: str, amount: float) -> dict:
        self._ensure_connected()
        if not math.isfinite(float(amount)) or float(amount) <= 0:
            raise ValueError(f"市价单数量无效: symbol={symbol}, side={side}, amount={amount}")

        # ccxt(hyperliquid) 最新写法：
        # market order 仍需传入参考 price，ccxt 再按 slippage 计算可接受最差成交价
        price = self._resolve_market_order_price(symbol, side)
        params = {"slippage": str(self.market_order_slippage)}

        try:
            return self.exchange.create_order(symbol, "market", side, amount, price, params)
        except Exception as exc:
            msg = str(exc)
            if "User or API Wallet" in msg and "does not exist" in msg:
                raise ValueError(
                    "Hyperliquid 下单签名地址不存在或未授权。"
                    f"当前配置: wallet_address={self.signing_wallet_address}, account_address={self.account_address}。"
                    "请检查 config.exchange.secret 对应地址是否与 wallet_address/api_key 一致，"
                    "并确认该地址已在当前网络（sandbox/testnet 或 mainnet）创建并授权 API wallet。"
                ) from exc
            raise
