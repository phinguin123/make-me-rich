import time
import uuid
import logging
import requests
import pandas as pd
from typing import Dict, Optional

class RateLimiter:
    def __init__(self, max_rate: float = 12.0, time_period: float = 1.0):
        self.max_rate = max_rate
        self.time_period = time_period
        self.tokens = max_rate
        self.last_update = time.monotonic()

    def acquire(self) -> None:
        now = time.monotonic()
        elapsed = now - self.last_update
        self.last_update = now
        
        self.tokens = min(self.max_rate, self.tokens + elapsed * (self.max_rate / self.time_period))
        
        if self.tokens < 1.0:
            sleep_time = (1.0 - self.tokens) * (self.time_period / self.max_rate)
            time.sleep(sleep_time)
            self.tokens = 0.0
            self.last_update = time.monotonic()
        else:
            self.tokens -= 1.0

class TossOpenAPIClient:
    def __init__(self, client_id: str, client_secret: str, account_seq: str, dry_run: bool = True):
        self.logger = logging.getLogger(self.__class__.__name__)
        self.client_id = client_id
        self.client_secret = client_secret
        self.account_seq = account_seq
        self.dry_run = dry_run
        
        self.base_url = "https://openapi.tossinvest.com"
        self.access_token: Optional[str] = None
        self.token_expiration: float = 0.0
        self.session = requests.Session()
        self.session.headers.update({"Content-Type": "application/json"})
        
        self.rate_limiter = RateLimiter(max_rate=12.0, time_period=1.0)
        
        if self.dry_run:
            self.logger.warning("INITIALIZED IN DRY-RUN MODE. NO LIVE ORDERS WILL BE ROUTED.")
        else:
            self.logger.warning("LIVE TRADING ENABLED. ORDERS WILL HIT THE EXCHANGE.")

    def _authenticate(self) -> None:
        url = f"{self.base_url}/oauth2/token"
        payload = {"grant_type": "client_credentials", "client_id": self.client_id, "client_secret": self.client_secret}
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        
        response = self.session.post(url, data=payload, headers=headers)
        if response.status_code == 200:
            data = response.json()
            token_data = data.get("result", data)
            self.access_token = token_data["access_token"]
            expires_in = token_data.get("expires_in", 3600)
            self.token_expiration = time.time() + expires_in - 60
            self.session.headers.update({"Authorization": f"Bearer {self.access_token}"})
        else:
            raise RuntimeError(f"Authentication failure [{response.status_code}]")

    def _ensure_token_validity(self) -> None:
        if not self.access_token or time.time() >= self.token_expiration:
            self._authenticate()

    def request(self, method: str, endpoint: str, params: Optional[Dict] = None, data: Optional[Dict] = None) -> Dict:
        self.rate_limiter.acquire()
        self._ensure_token_validity()
        
        url = f"{self.base_url}{endpoint}"
        for attempt in range(5):
            response = self.session.request(method, url, params=params, json=data)
            remaining = int(response.headers.get("x-ratelimit-remaining", -1))
            
            if remaining == 0 or response.status_code == 429:
                reset = response.headers.get("x-ratelimit-reset")
                sleep_s = max((float(reset) - (time.time() * 1000.0)) / 1000.0, 0.5) if reset else (2 ** attempt)
                time.sleep(sleep_s)
                continue

            if response.status_code in (200, 201):
                res_json = response.json()
                return res_json.get("result", res_json)
            
            response.raise_for_status()
        raise RuntimeError("Rate limit retry threshold exceeded.")

    def get_tick_trades(self, symbol: str, count: int = 50) -> pd.DataFrame:
        data = self.request("GET", "/api/v1/trades", params={"symbol": symbol, "count": count})
        trades = data if isinstance(data, list) else []
        if not trades:
            return pd.DataFrame(columns=["timestamp", "price", "size"])
        df = pd.DataFrame(trades)
        if "volume" in df.columns:
            df.rename(columns={"volume": "size"}, inplace=True)
        for col in ["price", "size"]:
            df[col] = df[col].astype(float)
        return df.sort_values("timestamp").reset_index(drop=True)

    def get_l1_orderbook(self, symbol: str) -> pd.DataFrame:
        data = self.request("GET", "/api/v1/orderbook", params={"symbol": symbol})
        asks, bids = data.get("asks", []), data.get("bids", [])
        if not asks or not bids:
            return pd.DataFrame(columns=["timestamp", "bid_price", "bid_size", "ask_price", "ask_size"])

        best_ask = min(asks, key=lambda x: float(x["price"]))
        best_bid = max(bids, key=lambda x: float(x["price"]))

        df = pd.DataFrame([{
            "timestamp": pd.to_datetime(data.get("timestamp")),
            "bid_price": float(best_bid["price"]),
            "bid_size": float(best_bid["volume"]),
            "ask_price": float(best_ask["price"]),
            "ask_size": float(best_ask["volume"])
        }])
        return df

    def place_order(self, symbol: str, side: str, quantity: int, price: float, order_type: str = "LIMIT") -> Dict:
        cid = str(uuid.uuid4())
        payload = {
            "accountNumber": self.account_seq,
            "symbol": symbol,
            "side": side.upper(),
            "orderType": order_type.upper(),
            "quantity": quantity,
            "price": price if order_type.upper() == "LIMIT" else 0.0,
            "timeInForce": "IOC" if order_type.upper() == "MARKET" else "GTC",
            "clientOrderId": cid
        }
        if self.dry_run:
            self.logger.info(f"[DRY RUN] Validated: {payload}")
            return {"status": "DRY_RUN", "clientOrderId": cid, "filledQty": quantity}
        
        self.logger.info(f"LIVE EXEC: {side} {quantity} {symbol} @ {price} [{cid}]")
        return self.request("POST", "/api/v1/orders", data=payload)

    def cancel_all_orders(self, symbol: str) -> None:
        if self.dry_run: return
        self.request("POST", "/api/v1/orders/cancel-all", data={"symbol": symbol, "accountNumber": self.account_seq})

    def close_position(self, symbol: str) -> None:
        if self.dry_run: return
        self.request("POST", "/api/v1/positions/close", data={"symbol": symbol, "accountNumber": self.account_seq})