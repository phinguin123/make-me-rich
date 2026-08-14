import os
from dotenv import load_dotenv

load_dotenv()

TOSS_CLIENT_ID = os.getenv("TOSS_CLIENT_ID", "YOUR_CLIENT_ID")
TOSS_CLIENT_SECRET = os.getenv("TOSS_CLIENT_SECRET", "YOUR_CLIENT_SECRET")
TOSS_ACCOUNT_SEQ = os.getenv("TOSS_ACCOUNT_SEQ", "1")

# Defaults to dry-run (False) unless explicitly enabled
ENABLE_LIVE_TRADING = os.getenv("TOSS_ENABLE_TRADING", "false").lower() == "true"

TOSS_SYMBOL = os.getenv("TOSS_SYMBOL", "SOXL")
TOSS_TARGET_SIZE = int(os.getenv("TOSS_TARGET_SIZE", "500"))
FLASK_SECRET_KEY = os.getenv("FLASK_SECRET_KEY", "toss-dashboard-dev")
TOSS_DASHBOARD_PORT = int(os.getenv("TOSS_DASHBOARD_PORT", "5050"))
