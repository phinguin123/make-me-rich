"""Put the repo root on sys.path so `screener` can be imported."""
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
OUTPUT_DIR = REPO_ROOT / "output" / "us"

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
