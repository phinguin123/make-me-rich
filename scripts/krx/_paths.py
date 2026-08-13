"""Repo paths for KR scripts. Import this before anything else."""
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
OUTPUT_DIR = REPO_ROOT / "output" / "krx"
SESSION_PATH = REPO_ROOT / "session_data.json"

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
