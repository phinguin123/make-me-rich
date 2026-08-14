import numpy as np
import pandas as pd
from scipy.stats import norm
from scipy.cluster.vq import kmeans
from typing import List, Tuple

class MicrostructureEngine:
    @staticmethod
    def volume_clock_transform(trades: pd.DataFrame, bucket_volume: float) -> pd.DataFrame:
        if trades.empty: return pd.DataFrame()
        trades = trades.copy()
        trades["cum_volume"] = trades["size"].cumsum()
        trades["bucket_id"] = (trades["cum_volume"] // bucket_volume).astype(int)
        
        return trades.groupby("bucket_id").agg(
            timestamp=("timestamp", "last"),
            open=("price", "first"),
            high=("price", "max"),
            low=("price", "min"),
            close=("price", "last"),
            volume=("size", "sum")
        ).reset_index()

    @staticmethod
    def compute_bvc_and_vpin(volume_bars: pd.DataFrame, window: int = 50, bucket_vol: float = 50000.0) -> pd.DataFrame:
        if len(volume_bars) < window: return volume_bars
        volume_bars = volume_bars.copy()
        
        volume_bars["delta_p"] = volume_bars["close"].diff()
        volume_bars["sigma_p"] = volume_bars["delta_p"].rolling(window=window).std()
        safe_sigma = np.where(volume_bars["sigma_p"] == 0, 1e-8, volume_bars["sigma_p"])
        
        volume_bars["buy_prob"] = norm.cdf((volume_bars["delta_p"] / safe_sigma).fillna(0))
        volume_bars["v_buy"] = bucket_vol * volume_bars["buy_prob"]
        volume_bars["v_sell"] = bucket_vol - volume_bars["v_buy"]
        
        imbalance = (volume_bars["v_buy"] - volume_bars["v_sell"]).abs()
        volume_bars["vpin"] = imbalance.rolling(window=window).sum() / (window * bucket_vol)
        return volume_bars

    @staticmethod
    def compute_ofi(quotes: pd.DataFrame) -> pd.Series:
        if quotes.empty: return pd.Series(dtype=float)
        q = quotes.copy()
        q["prev_bid_p"], q["prev_bid_s"] = q["bid_price"].shift(1), q["bid_size"].shift(1)
        q["prev_ask_p"], q["prev_ask_s"] = q["ask_price"].shift(1), q["ask_size"].shift(1)
        
        e_b = np.where(q["bid_price"] > q["prev_bid_p"], q["bid_size"],
              np.where(q["bid_price"] == q["prev_bid_p"], q["bid_size"] - q["prev_bid_s"], -q["prev_bid_s"]))
        e_a = np.where(q["ask_price"] < q["prev_ask_p"], q["ask_size"],
              np.where(q["ask_price"] == q["prev_ask_p"], q["ask_size"] - q["prev_ask_s"], -q["prev_ask_s"]))
        
        q["ofi"] = np.nan_to_num(e_b) - np.nan_to_num(e_a)
        return q["ofi"]

    @staticmethod
    def kmeans_cluster_levels(df: pd.DataFrame, window: int = 5, k: int = 3) -> Tuple[List[float], List[float]]:
        highs, lows, supports, resistances = df["high"].values, df["low"].values, [], []
        for i in range(window, len(df) - window):
            if highs[i] == np.max(highs[i - window : i + window + 1]): resistances.append(highs[i])
            if lows[i] == np.min(lows[i - window : i + window + 1]): supports.append(lows[i])
                
        def get_centroids(levels, k_val):
            return sorted(levels) if len(levels) <= k_val else sorted(kmeans(np.array(levels, dtype=float), k_val)[0].tolist())
        return get_centroids(supports, k), get_centroids(resistances, k)

class AlmgrenChrissExecutionEngine:
    def __init__(self, risk_aversion: float = 1e-4, temp_impact: float = 0.01):
        self.lambda_risk = risk_aversion
        self.eta_impact = temp_impact

    def generate_schedule(self, X0: float, T_steps: int, sigma: float, expected_ofi: float = 0.0) -> List[float]:
        if sigma <= 0 or T_steps <= 0: return [X0]
        kappa = np.sqrt((self.lambda_risk * (sigma ** 2)) / self.eta_impact)
        
        inventory = [X0 * (np.sinh(kappa * (T_steps - k)) / np.sinh(kappa * T_steps)) if np.sinh(kappa * T_steps) != 0 else 0 for k in range(T_steps + 1)]
        base_executions = [-np.diff(inventory)[k] for k in range(T_steps)]
        
        adjusted = []
        for k, v_ac in enumerate(base_executions):
            rem = T_steps - k
            discount = (1 - np.exp(-kappa * rem)) / kappa if kappa != 0 else rem
            adjusted.append(max(0.0, v_ac + (1 / (2 * self.eta_impact)) * expected_ofi * discount))
            
        total_adj = sum(adjusted)
        if total_adj > 0:
            schedule = [int(np.round(X0 * (x / total_adj))) for x in adjusted]
            schedule[-1] += int(X0 - sum(schedule))
            return schedule
        return [int(X0)]