"""
vcp_scanner — Korean-market momentum scanner (Minervini/VCP adapted).

Public API
----------
from vcp_scanner.compute  import compute_features
from vcp_scanner.ranking  import rank_candidates, summary_table
from vcp_scanner.scanner  import run_scanner
"""
from .compute  import compute_features
from .ranking  import rank_candidates, summary_table
from .scanner  import run_scanner

__all__ = ["compute_features", "rank_candidates", "summary_table", "run_scanner"]
