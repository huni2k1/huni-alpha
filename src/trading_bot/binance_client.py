"""
Binance USDM Futures REST API client.

Thin wrapper around the Futures REST API — no third-party SDK dependency.
Supports live and testnet. Defaults to TESTNET unless BINANCE_TESTNET=false.

Credentials (in priority order):
  1. Constructor args
  2. BINANCE_API_KEY / BINANCE_API_SECRET env vars
  3. config/binance-trading.json  (separate from binance-real.json which is read-only)
"""

import os
import json
import time
import hmac
import hashlib
import logging
import requests
from typing import Optional

log = logging.getLogger("trader.binance")

BASE_URL_LIVE    = "https://fapi.binance.com"
BASE_URL_TESTNET = "https://testnet.binancefuture.com"

_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", "config", "binance-trading.json"
)


class BinanceClient:
    def __init__(
        self,
        api_key: str = None,
        api_secret: str = None,
        testnet: bool = None,
    ):
        # Resolve credentials: args → env → config file
        if not api_key:
            api_key = os.environ.get("BINANCE_API_KEY", "")
        if not api_secret:
            api_secret = os.environ.get("BINANCE_API_SECRET", "")

        if not api_key or not api_secret:
            try:
                with open(_CONFIG_PATH) as f:
                    cfg = json.load(f)
                    api_key = api_key or cfg.get("api_key", "")
                    api_secret = api_secret or cfg.get("api_secret", "")
            except FileNotFoundError:
                pass

        if not api_key or not api_secret:
            raise ValueError(
                "Binance credentials not found. "
                "Set BINANCE_API_KEY + BINANCE_API_SECRET env vars, "
                "or create config/binance-trading.json."
            )

        self._api_key = api_key
        self._api_secret = api_secret.encode()

        # Testnet default: True (safe). Set BINANCE_TESTNET=false to go live.
        if testnet is None:
            raw = os.environ.get("BINANCE_TESTNET", "true").strip().lower()
            testnet = raw not in ("false", "0", "no")

        self.testnet = testnet
        self.base_url = BASE_URL_TESTNET if testnet else BASE_URL_LIVE
        self._symbol_cache: dict = {}

        mode = "TESTNET" if testnet else "LIVE ⚠️"
        log.info(f"BinanceClient ready [{mode}] {self.base_url}")

    # ── Internal ────────────────────────────────────────────────────

    def _sign(self, params: dict) -> dict:
        params["timestamp"] = int(time.time() * 1000)
        query = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
        sig = hmac.new(self._api_secret, query.encode(), hashlib.sha256).hexdigest()
        params["signature"] = sig
        return params

    def _request(
        self,
        method: str,
        endpoint: str,
        params: dict = None,
        signed: bool = False,
    ) -> Optional[dict]:
        params = dict(params or {})
        if signed:
            params = self._sign(params)

        headers = {"X-MBX-APIKEY": self._api_key}
        url = self.base_url + endpoint

        try:
            r = getattr(requests, method.lower())(
                url, params=params, headers=headers, timeout=10
            )
            if not r.ok:
                try:
                    err = r.json()
                    log.error(
                        f"Binance [{method} {endpoint}] {r.status_code}: "
                        f"code={err.get('code')} msg={err.get('msg')}"
                    )
                except Exception:
                    log.error(f"Binance [{method} {endpoint}] HTTP {r.status_code}")
                return None
            return r.json()
        except Exception as e:
            log.error(f"Binance request failed [{method} {endpoint}]: {e}")
            return None

    # ── Account ─────────────────────────────────────────────────────

    def get_usdt_balance(self) -> Optional[float]:
        """Free USDT in the futures wallet."""
        data = self._request("GET", "/fapi/v2/balance", signed=True)
        if not data:
            return None
        for asset in data:
            if asset.get("asset") == "USDT":
                return float(asset["availableBalance"])
        return None

    def get_open_positions(self) -> list:
        """All futures positions with non-zero size."""
        data = self._request("GET", "/fapi/v2/positionRisk", signed=True)
        if not data:
            return []
        return [p for p in data if float(p.get("positionAmt", 0)) != 0.0]

    def get_mark_price(self, symbol: str) -> Optional[float]:
        data = self._request("GET", "/fapi/v1/premiumIndex", {"symbol": symbol})
        if not data:
            return None
        return float(data.get("markPrice", 0))

    # ── Symbol rules ────────────────────────────────────────────────

    def get_symbol_info(self, symbol: str) -> Optional[dict]:
        """
        Returns trading rules for quantity/price rounding and minimums.

        {
            qty_precision:   int    (e.g. 3  → round to 0.001)
            price_precision: int    (e.g. 2  → round to 0.01)
            min_qty:         float  minimum order quantity
            min_notional:    float  minimum order value in USDT
        }
        """
        if symbol in self._symbol_cache:
            return self._symbol_cache[symbol]

        data = self._request("GET", "/fapi/v1/exchangeInfo")
        if not data:
            return None

        for s in data.get("symbols", []):
            if s["symbol"] != symbol:
                continue

            info = {
                "qty_precision": int(s.get("quantityPrecision", 3)),
                "price_precision": int(s.get("pricePrecision", 2)),
                "min_qty": 0.001,
                "min_notional": 5.0,
            }
            for f in s.get("filters", []):
                if f["filterType"] == "LOT_SIZE":
                    info["min_qty"] = float(f.get("minQty", 0.001))
                elif f["filterType"] == "MIN_NOTIONAL":
                    info["min_notional"] = float(f.get("notional", 5.0))

            self._symbol_cache[symbol] = info
            return info

        log.error(f"Symbol {symbol} not found in exchangeInfo")
        return None

    # ── Position mode ────────────────────────────────────────────────

    def ensure_one_way_mode(self) -> bool:
        """
        Confirm/set account to one-way position mode (not hedge mode).
        Must be true for closePosition=true to work on TP/SL orders.
        """
        data = self._request("GET", "/fapi/v1/positionSide/dual", signed=True)
        if not data:
            return False
        if data.get("dualSidePosition"):
            log.info("Setting position mode to one-way (disabling hedge mode)...")
            result = self._request(
                "POST", "/fapi/v1/positionSide/dual",
                {"dualSidePosition": "false"}, signed=True
            )
            return result is not None
        return True

    def set_leverage(self, symbol: str, leverage: int = 1) -> bool:
        params = {"symbol": symbol, "leverage": leverage}
        result = self._request("POST", "/fapi/v1/leverage", params, signed=True)
        return result is not None

    # ── Orders ──────────────────────────────────────────────────────

    def place_market_order(
        self, symbol: str, side: str, quantity: float
    ) -> Optional[dict]:
        """
        Open a position with a MARKET order.

        Args:
            symbol:   e.g. "BTCUSDT"
            side:     "BUY" for LONG entry, "SELL" for SHORT entry
            quantity: base asset amount (e.g. 0.001 BTC)

        Returns:
            {"order_id", "filled_price", "filled_qty", "status"}
        """
        params = {
            "symbol": symbol,
            "side": side,
            "type": "MARKET",
            "quantity": quantity,
        }
        result = self._request("POST", "/fapi/v1/order", params, signed=True)
        if not result:
            return None
        return {
            "order_id": str(result["orderId"]),
            "filled_price": float(result.get("avgPrice", 0)),
            "filled_qty": float(result.get("executedQty", quantity)),
            "status": result.get("status"),
        }

    def place_tp_order(
        self, symbol: str, close_side: str, tp_price: float, price_precision: int
    ) -> Optional[str]:
        """
        Place a TAKE_PROFIT_MARKET order that closes the full position.

        Args:
            close_side: "SELL" (close long) or "BUY" (close short)
            tp_price:   trigger price (mark price based)

        Returns:
            order_id string or None on failure
        """
        params = {
            "symbol": symbol,
            "side": close_side,
            "type": "TAKE_PROFIT_MARKET",
            "stopPrice": round(tp_price, price_precision),
            "closePosition": "true",
            "workingType": "MARK_PRICE",
            "priceProtect": "true",
        }
        result = self._request("POST", "/fapi/v1/order", params, signed=True)
        if not result:
            return None
        return str(result["orderId"])

    def place_sl_order(
        self, symbol: str, close_side: str, sl_price: float, price_precision: int
    ) -> Optional[str]:
        """
        Place a STOP_MARKET order that closes the full position.

        Args:
            close_side: "SELL" (close long) or "BUY" (close short)
            sl_price:   stop trigger price (mark price based)

        Returns:
            order_id string or None on failure
        """
        params = {
            "symbol": symbol,
            "side": close_side,
            "type": "STOP_MARKET",
            "stopPrice": round(sl_price, price_precision),
            "closePosition": "true",
            "workingType": "MARK_PRICE",
            "priceProtect": "true",
        }
        result = self._request("POST", "/fapi/v1/order", params, signed=True)
        if not result:
            return None
        return str(result["orderId"])

    def place_market_close(self, symbol: str, close_side: str, quantity: float) -> Optional[dict]:
        """
        Close a position immediately with a MARKET order.

        Args:
            close_side: "SELL" (close long) or "BUY" (close short)
            quantity:   position size to close
        """
        params = {
            "symbol": symbol,
            "side": close_side,
            "type": "MARKET",
            "quantity": quantity,
            "reduceOnly": "true",
        }
        result = self._request("POST", "/fapi/v1/order", params, signed=True)
        if not result:
            return None
        return {
            "order_id": str(result["orderId"]),
            "filled_price": float(result.get("avgPrice", 0)),
            "status": result.get("status"),
        }

    def cancel_order(self, symbol: str, order_id: str) -> bool:
        params = {"symbol": symbol, "orderId": order_id}
        result = self._request("DELETE", "/fapi/v1/order", params, signed=True)
        return result is not None

    def cancel_all_orders(self, symbol: str) -> bool:
        result = self._request(
            "DELETE", "/fapi/v1/allOpenOrders", {"symbol": symbol}, signed=True
        )
        return result is not None

    def get_order_status(self, symbol: str, order_id: str) -> Optional[str]:
        """Returns: "NEW" | "FILLED" | "CANCELED" | "PARTIALLY_FILLED" | "EXPIRED"."""
        params = {"symbol": symbol, "orderId": order_id}
        result = self._request("GET", "/fapi/v1/order", params, signed=True)
        if not result:
            return None
        return result.get("status")

    def get_order_fill_price(self, symbol: str, order_id: str) -> Optional[float]:
        """Get the average fill price of a completed order."""
        params = {"symbol": symbol, "orderId": order_id}
        result = self._request("GET", "/fapi/v1/order", params, signed=True)
        if not result:
            return None
        return float(result.get("avgPrice", 0)) or None
