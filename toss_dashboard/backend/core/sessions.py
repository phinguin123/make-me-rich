import logging
from typing import Dict, List, Tuple, Optional, Any
from datetime import datetime, timezone, time as datetime_time
from zoneinfo import ZoneInfo

class MarketSessionManager:
    def __init__(self):
        self.logger = logging.getLogger(self.__class__.__name__)
        # Relying on America/New_York safely tracks ET shifts (EDT/EST) automatically.
        self.tz_et = ZoneInfo("America/New_York")
        self.tz_kst = ZoneInfo("Asia/Seoul")
        
        self.pre_market_reset = datetime_time(9, 15, 0)
        self.eod_flatten = datetime_time(15, 30, 0)
        self.after_market_close = datetime_time(20, 0, 0)
        self.ats_liquidation = datetime_time(3, 45, 0)

        self.last_action_date: Dict[str, Optional[datetime.date]] = {
            "eod_flatten": None,
            "ats_liquidation": None,
            "pre_market_reset": None
        }

    def get_times(self) -> Tuple[datetime, datetime]:
        now_utc = datetime.now(timezone.utc)
        return now_utc.astimezone(self.tz_et), now_utc.astimezone(self.tz_kst)

    def get_session_info(self) -> Dict[str, Any]:
        et_time, kst_time = self.get_times()
        if et_time.weekday() >= 5:
            return {"regime": "CLOSED", "limit_only": True, "max_spread": 0.0, "bucket_scale": 1.0}

        t = et_time.time()
        if datetime_time(4, 0, 0) <= t < datetime_time(9, 30, 0):
            return {"regime": "PRE_MARKET", "limit_only": True, "max_spread": 0.15, "bucket_scale": 1.0}
        elif datetime_time(9, 30, 0) <= t < datetime_time(16, 0, 0):
            return {"regime": "REGULAR", "limit_only": False, "max_spread": 999.0, "bucket_scale": 1.0}
        elif datetime_time(16, 0, 0) <= t < datetime_time(20, 0, 0):
            return {"regime": "AFTER_MARKET", "limit_only": True, "max_spread": 0.15, "bucket_scale": 1.0}
        else:
            return {"regime": "DAYTIME_ATS", "limit_only": True, "max_spread": 0.15, "bucket_scale": 0.25}

    def check_action_triggers(self, current_et: datetime) -> List[str]:
        triggers = []
        t, today = current_et.time(), current_et.date()

        if t >= self.eod_flatten and t < datetime_time(16, 0, 0):
            if self.last_action_date["eod_flatten"] != today:
                triggers.append("EOD_FLATTEN")
                self.last_action_date["eod_flatten"] = today

        if t >= self.ats_liquidation and t < datetime_time(4, 0, 0):
            if self.last_action_date["ats_liquidation"] != today:
                triggers.append("ATS_LIQUIDATION")
                self.last_action_date["ats_liquidation"] = today
                
        if t >= self.pre_market_reset and t < datetime_time(9, 30, 0):
            if self.last_action_date["pre_market_reset"] != today:
                triggers.append("PRE_MARKET_RESET")
                self.last_action_date["pre_market_reset"] = today
        return triggers