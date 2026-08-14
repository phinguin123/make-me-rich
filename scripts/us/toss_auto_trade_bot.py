"""CLI shim — trading classes live in toss_dashboard/backend/core."""
from dotenv import load_dotenv
load_dotenv()

import _paths  # noqa: F401 — repo root on sys.path
import logging
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2] / "toss_dashboard" / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from config import (  # noqa: E402
    TOSS_ACCOUNT_SEQ,
    TOSS_CLIENT_ID,
    TOSS_CLIENT_SECRET,
    ENABLE_LIVE_TRADING,
    TOSS_SYMBOL,
    TOSS_TARGET_SIZE,
)
from core.bot import Quantitative24HourBot  # noqa: E402
from core.client import RateLimiter, TossOpenAPIClient  # noqa: E402
from core.microstructure import AlmgrenChrissExecutionEngine, MicrostructureEngine  # noqa: E402
from core.sessions import MarketSessionManager  # noqa: E402

__all__ = [
    "RateLimiter",
    "TossOpenAPIClient",
    "MarketSessionManager",
    "MicrostructureEngine",
    "AlmgrenChrissExecutionEngine",
    "Quantitative24HourBot",
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
    handlers=[logging.StreamHandler()],
)

if __name__ == "__main__":
    toss_client = TossOpenAPIClient(
        client_id=TOSS_CLIENT_ID,
        client_secret=TOSS_CLIENT_SECRET,
        account_seq=TOSS_ACCOUNT_SEQ,
        dry_run=not ENABLE_LIVE_TRADING,
    )
    bot = Quantitative24HourBot(
        api_client=toss_client,
        symbol=TOSS_SYMBOL,
        target_size=TOSS_TARGET_SIZE,
    )
    try:
        bot.run_loop()
    except KeyboardInterrupt:
        logging.info("Manual Interrupt detected. Safely shutting down.")
    except Exception as e:
        logging.critical(f"FATAL SYSTEM ERROR: {str(e)}", exc_info=True)
