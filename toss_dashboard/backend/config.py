import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()
load_dotenv(Path(__file__).resolve().parents[2] / ".env")

TOSS_CLIENT_ID = os.getenv("TOSS_CLIENT_ID", "YOUR_CLIENT_ID")
TOSS_CLIENT_SECRET = os.getenv("TOSS_CLIENT_SECRET", "YOUR_CLIENT_SECRET")
TOSS_ACCOUNT_SEQ = os.getenv("TOSS_ACCOUNT_SEQ", "1")

# Defaults to dry-run (False) unless explicitly enabled
ENABLE_LIVE_TRADING = os.getenv("TOSS_ENABLE_TRADING", "false").lower() == "true"

TOSS_SYMBOL = os.getenv("TOSS_SYMBOL", "SOXL")
TOSS_TARGET_SIZE = int(os.getenv("TOSS_TARGET_SIZE", "500"))
FLASK_SECRET_KEY = os.getenv("FLASK_SECRET_KEY", "toss-dashboard-dev")
TOSS_DASHBOARD_PORT = int(os.getenv("TOSS_DASHBOARD_PORT", "5050"))

APP_KEY = os.getenv("APP_KEY", "DUMMY")
APP_SECRET = os.getenv("APP_SECRET", "DUMMY")
KIWOOM_TICKER = os.getenv("KIWOOM_TICKER") or os.getenv("TOSS_SYMBOL", "SOXL")
KIWOOM_STEX_TP = os.getenv("KIWOOM_STEX_TP", "ND,NY,AM")
KIWOOM_MOCK = os.getenv("KIWOOM_MOCK", "false").lower() == "true"
