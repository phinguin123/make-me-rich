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

        # --- NEW: Fee & Edge Management ---
        self.fee_rate = 0.001          # 0.1% per side (0.2% round trip)
        self.min_net_edge = 0.003      # Require at least 0.3% net profit after fees

        # --- NEW: Position Inventory Tracker ---
        self.current_inventory = 0
        
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

    def _ingest_ticks(self) -> None:
        new_trades = self.client.get_tick_trades(self.symbol, count=500)
        if new_trades.empty:
            return
        self.trades_history = (
            pd.concat([self.trades_history, new_trades])
            .drop_duplicates(subset=["timestamp", "price", "size"])
            .tail(20000)
            .reset_index(drop=True)
        )

    def _broadcast_clock(self, regime, current_price, kst_time, session_rules) -> dict:
        active_bucket = self.base_bucket_volume * session_rules["bucket_scale"]
        vol_bars = self.micro.volume_clock_transform(self.trades_history, active_bucket)
        vol_bars = self.micro.compute_bvc_and_vpin(vol_bars, window=50, bucket_vol=active_bucket)
        if len(vol_bars) < 50:
            self.logger.info(
                f"[{kst_time.strftime('%H:%M:%S')} KST] {regime} | "
                f"Clock Warm-up: {len(vol_bars)}/50 | ticks={len(self.trades_history)}"
            )
            self._broadcast_state(regime, current_price=current_price)
            return {"ready": False, "vol_bars": vol_bars}
        current_price = vol_bars["close"].iloc[-1]
        latest_vpin = vol_bars["vpin"].iloc[-1]
        ofi_series = self.micro.compute_ofi(self.quotes_history)
        expected_ofi = ofi_series.ewm(span=10).mean().iloc[-1] if len(ofi_series) > 1 else 0.0
        self._broadcast_state(regime, current_price, latest_vpin, expected_ofi)
        return {
            "ready": True,
            "vol_bars": vol_bars,
            "current_price": current_price,
            "sigma_p": vol_bars["sigma_p"].iloc[-1],
            "latest_vpin": latest_vpin,
            "expected_ofi": expected_ofi,
        }

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
            self._ingest_ticks()
            self._broadcast_state(regime)
            return

        self._ingest_ticks()

        current_quote = self.client.get_l1_orderbook(self.symbol)
        if current_quote.empty:
            last_px = float(self.trades_history["price"].iloc[-1]) if not self.trades_history.empty else 0.0
            if self.trades_history.empty:
                self._broadcast_state(regime, current_price=last_px)
            else:
                self._broadcast_clock(regime, last_px, kst_time, session_rules)
            return

        spread = current_quote["ask_price"].iloc[0] - current_quote["bid_price"].iloc[0]
        self.quotes_history = pd.concat([self.quotes_history, current_quote], ignore_index=True).tail(100)

        last_px = float(current_quote["bid_price"].iloc[0])
        if not self.trades_history.empty:
            last_px = float(self.trades_history["price"].iloc[-1])

        if self.trades_history.empty:
            self._broadcast_state(regime, current_price=last_px)
            return

        if spread > session_rules["max_spread"]:
            self.logger.warning(
                f"Spread Gatekeeper skipped orders. Spread {spread:.4f} > {session_rules['max_spread']:.4f}"
            )
            self._broadcast_clock(regime, last_px, kst_time, session_rules)
            return

        clock = self._broadcast_clock(regime, last_px, kst_time, session_rules)
        if not clock["ready"]:
            return

        vol_bars = clock["vol_bars"]
        current_price = clock["current_price"]
        sigma_p = clock["sigma_p"]
        latest_vpin = clock["latest_vpin"]
        expected_ofi = clock["expected_ofi"]

        if regime == "REGULAR":
            if latest_vpin > self.vpin_threshold:
                self.logger.error("VPIN Toxicity > 0.70. Halting Regular Session entries.")
                return

            required_ofi_threshold = 500.0 + (sigma_p * 1000)
            
            # Generate the Almgren-Chriss slicing schedule
            schedule = self.execution.generate_schedule(self.target_size, 5, sigma_p, expected_ofi)
            order_qty = schedule[0] if schedule else (self.target_size // 5)

            # BUY Signal (Institutional Momentum Up)
            if expected_ofi > required_ofi_threshold:
                if self.current_inventory + order_qty <= self.target_size:
                    self.logger.info(f"Regular Fee Gate Passed. Buying momentum slice of {order_qty} shares. Inventory: {self.current_inventory}/{self.target_size}")
                    self.client.place_order(self.symbol, "BUY", order_qty, current_price, "MARKET")
                    self.current_inventory += order_qty  # Add to backpack
                else:
                    self.logger.debug("Regular session target inventory maxed out. Skipping buy.")
                
            # SELL Signal (Institutional Momentum Down)
            elif expected_ofi < -required_ofi_threshold:
                if self.current_inventory > 0:
                    sell_qty = min(order_qty, self.current_inventory)
                    self.logger.info(f"Regular Fee Gate Passed. Selling momentum slice of {sell_qty} shares. Inventory: {self.current_inventory}/{self.target_size}")
                    self.client.place_order(self.symbol, "SELL", sell_qty, current_price, "MARKET")
                    self.current_inventory -= sell_qty  # Empty backpack
                else:
                    self.logger.debug("No inventory to dump in regular session. Skipping.")

        elif regime in ["AFTER_MARKET", "DAYTIME_ATS", "PRE_MARKET"]:
            k_supports, k_resistances = self.micro.kmeans_cluster_levels(vol_bars, window=5, k=3)
            nearest_support = max([s for s in k_supports if s < current_price], default=current_price*0.98)
            nearest_resistance = min([r for r in k_resistances if r > current_price], default=current_price*1.02)
            
            round_trip_fee = self.fee_rate * 2  
            required_total_edge = round_trip_fee + self.min_net_edge  
            
            order_qty = self.target_size // 5  # e.g., 10 // 5 = 2 shares per slice

            # Check Support (BUY opportunity)
            potential_upside = (nearest_resistance - current_price) / current_price
            if current_price <= nearest_support * 1.002 and potential_upside >= required_total_edge:
                
                # --- INVENTORY CHECK ---
                if self.current_inventory + order_qty <= self.target_size:
                    self.logger.info(f"Fee Gate Passed. Buying {order_qty} shares. Current Inventory: {self.current_inventory}/{self.target_size}")
                    self.client.place_order(self.symbol, "BUY", order_qty, nearest_support, "LIMIT")
                    self.current_inventory += order_qty  # Add to backpack
                else:
                    self.logger.debug("Target inventory reached. Skipping buy order.")
                
            # Check Resistance (SELL opportunity to take profit)
            potential_downside = (current_price - nearest_support) / current_price
            if current_price >= nearest_resistance * 0.998 and potential_downside >= required_total_edge:
                
                # --- INVENTORY CHECK ---
                if self.current_inventory > 0:
                    sell_qty = min(order_qty, self.current_inventory)
                    self.logger.info(f"Fee Gate Passed. Selling {sell_qty} shares to take profit.")
                    self.client.place_order(self.symbol, "SELL", sell_qty, nearest_resistance, "LIMIT")
                    self.current_inventory -= sell_qty  # Empty backpack
                else:
                    self.logger.debug("No inventory to sell. Skipping.")