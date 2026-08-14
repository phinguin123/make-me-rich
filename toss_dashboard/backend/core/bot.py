import logging
import pandas as pd
from typing import List
from .sessions import MarketSessionManager
from .microstructure import MicrostructureEngine, AlmgrenChrissExecutionEngine

class Quantitative24HourBot:
    def __init__(self, api_client, symbol: str, target_size: int, on_state_update=None):
        self.logger = logging.getLogger(self.__class__.__name__)
        self.client = api_client
        self.symbol = symbol
        self.target_size = target_size
        self.on_state_update = on_state_update  # WebSocket Callback
        
        self.session_manager = MarketSessionManager()
        self.micro = MicrostructureEngine()
        self.execution = AlmgrenChrissExecutionEngine()
        
        self.base_bucket_volume = 50000.0  
        self.vpin_threshold = 0.70    
        
        self.quotes_history = pd.DataFrame(columns=["timestamp", "bid_price", "bid_size", "ask_price", "ask_size"])
        self.trades_history = pd.DataFrame(columns=["timestamp", "price", "size"])

    def _broadcast_state(self, regime, current_price=0.0, vpin=0.0, ofi=0.0):
        if not self.on_state_update: return
        
        total_vol = self.trades_history["size"].sum() if not self.trades_history.empty else 0
        active_bucket = self.base_bucket_volume * self.session_manager.get_session_info()["bucket_scale"]
        target_vol = 50 * active_bucket
        progress_pct = (total_vol / target_vol) * 100 if target_vol > 0 else 0
        
        self.on_state_update({
            "regime": regime,
            "price": float(current_price),
            "vpin": float(vpin),
            "ofi": float(ofi),
            "ticks": len(self.trades_history),
            "total_vol": float(total_vol),
            "target_vol": float(target_vol),
            "progress_pct": float(progress_pct)
        })

    def run_strategy_iteration(self) -> None:
        et_time, kst_time = self.session_manager.get_times()
        session_rules = self.session_manager.get_session_info()
        regime = session_rules["regime"]
        
        triggers = self.session_manager.check_action_triggers(et_time)
        for t in triggers:
            self.logger.warning(f"SYSTEM ACTION TRIGGERED: {t}")
            if t == "EOD_FLATTEN":
                self.client.cancel_all_orders(self.symbol)
                self.client.close_position(self.symbol)
            elif t == "ATS_LIQUIDATION":
                self.client.cancel_all_orders(self.symbol)
            elif t == "PRE_MARKET_RESET":
                self.client.cancel_all_orders(self.symbol)
                self.quotes_history = self.quotes_history.iloc[0:0]
                self.trades_history = self.trades_history.iloc[0:0]

        if regime == "CLOSED":
            self._broadcast_state(regime)
            return

        current_quote = self.client.get_l1_orderbook(self.symbol)
        if current_quote.empty:
            self._broadcast_state(regime)
            return
            
        spread = current_quote["ask_price"].iloc[0] - current_quote["bid_price"].iloc[0]
        if spread > session_rules["max_spread"]:
            self._broadcast_state(regime)
            return

        self.quotes_history = pd.concat([self.quotes_history, current_quote], ignore_index=True).tail(100)

        new_trades = self.client.get_tick_trades(self.symbol, count=50)
        if new_trades.empty:
            self._broadcast_state(regime)
            return
            
        self.trades_history = (
            pd.concat([self.trades_history, new_trades])
            .drop_duplicates(subset=["timestamp", "price", "size"])
            .tail(5000).reset_index(drop=True)
        )
            
        active_bucket = self.base_bucket_volume * session_rules["bucket_scale"]
        vol_bars = self.micro.volume_clock_transform(self.trades_history, active_bucket)
        vol_bars = self.micro.compute_bvc_and_vpin(vol_bars, window=50, bucket_vol=active_bucket)
        
        if len(vol_bars) < 50:
            self.logger.info(f"[{kst_time.strftime('%H:%M:%S')} KST] {regime} | Clock Warm-up: {len(vol_bars)}/50")
            self._broadcast_state(regime, current_price=current_quote["bid_price"].iloc[0])
            return

        current_price = vol_bars["close"].iloc[-1]
        sigma_p = vol_bars["sigma_p"].iloc[-1]
        latest_vpin = vol_bars["vpin"].iloc[-1]
        
        ofi_series = self.micro.compute_ofi(self.quotes_history)
        expected_ofi = ofi_series.ewm(span=10).mean().iloc[-1] if len(ofi_series) > 1 else 0.0

        # Broadcast live, fully-warmed state to React
        self._broadcast_state(regime, current_price, latest_vpin, expected_ofi)

        if regime == "REGULAR":
            if latest_vpin > self.vpin_threshold:
                self.logger.error("VPIN Toxicity > 0.70. Halting Regular Session entries.")
                return

            schedule = self.execution.generate_schedule(self.target_size, 5, sigma_p, expected_ofi)
            if expected_ofi > 500:
                self.client.place_order(self.symbol, "BUY", schedule[0], current_price, "MARKET")
            elif expected_ofi < -500:
                self.client.place_order(self.symbol, "SELL", schedule[0], current_price, "MARKET")

        elif regime in ["AFTER_MARKET", "DAYTIME_ATS", "PRE_MARKET"]:
            k_supports, k_resistances = self.micro.kmeans_cluster_levels(vol_bars, window=5, k=3)
            supp = max([s for s in k_supports if s < current_price], default=current_price*0.98)
            res = min([r for r in k_resistances if r > current_price], default=current_price*1.02)
            
            if current_price <= supp * 1.002:
                self.client.place_order(self.symbol, "BUY", self.target_size // 5, supp, "LIMIT")
            elif current_price >= res * 0.998:
                self.client.place_order(self.symbol, "SELL", self.target_size // 5, res, "LIMIT")