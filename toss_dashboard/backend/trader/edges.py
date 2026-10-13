"""Edge health monitor: compares each live edge's trades with its backtest and sizes it down or
switches it off when live results fall clearly outside what the backtest allows.

Expectations come from trader/edges.json (python -m trader.research.edge_stats): per-trade net
return mean and standard deviation, trades per year, and one-year drawdown limits from bootstrap
resampling (dd_reduce: worse than 90% of resampled years, dd_off: worse than 99%).

Rules (per-trade net returns on the position, counted since the last re-enable):
  z  = (live mean - backtest mean) / (backtest std / sqrt(n))
  dd = current cumulative return minus its running peak
  off      n >= 15 and z <= -3, or dd <= dd_off               size x0, until re-enabled by hand
  reduced  n >= 8 and z <= -2,  or dd <= dd_reduce           size x0.5
           back to healthy only once z > -1.5 and dd > dd_reduce / 2
  healthy  otherwise                                         size x1
These catch an edge that broke (execution problems, a market that turned against it). A slow fade
toward zero needs hundreds of trades to see; the periodic re-test of the backtest catches that.
"""
from __future__ import annotations

import json
import logging
import math
from datetime import datetime
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
EXPECTATIONS = Path(__file__).with_name("edges.json")
SIZE = {"healthy": 1.0, "reduced": 0.5, "off": 0.0}


def load_expectations(path: Path = EXPECTATIONS) -> dict[str, dict]:
    return json.loads(path.read_text())["edges"] if path.exists() else {}


class EdgeMonitor:
    def __init__(self, path: Path, log: Callable[[str, int], None] = lambda msg, lvl=20: None,
                 expectations: dict[str, dict] | None = None):
        self.path, self.log = path, log
        self.exp = expectations if expectations is not None else load_expectations()
        self.edges: dict[str, dict] = {}
        if path.exists():
            self.edges = json.loads(path.read_text()).get("edges", {})

    def _edge(self, name: str) -> dict:
        return self.edges.setdefault(name, {"trades": [], "since": 0, "status": "healthy", "reason": "", "changed": ""})

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"edges": self.edges}, indent=1))
        tmp.replace(self.path)

    # ── inputs ────────────────────────────────────────────────────────────
    def record(self, name: str, ret: float, pnl: float, **info) -> None:
        """One closed trade: net return on the position (fees included) and dollar P&L."""
        e = self._edge(name)
        e["trades"].append({"ts": datetime.now(ET).isoformat(timespec="seconds"), "ret": round(ret, 6), "pnl": round(pnl, 2), **info})
        self._evaluate(name)
        self.save()

    def reenable(self, name: str) -> None:
        """Manual reset after an edge was switched off: judge it again from the next trade."""
        e = self._edge(name)
        e["since"] = len(e["trades"])
        self._set(name, "healthy", "re-enabled by hand")
        self.save()

    # ── decisions ─────────────────────────────────────────────────────────
    def multiplier(self, name: str) -> float:
        return SIZE[self.edges[name]["status"]] if name in self.edges else 1.0

    def stats(self, name: str) -> dict:
        e = self.edges.get(name, {"trades": [], "since": 0})
        rets = [t["ret"] for t in e["trades"][e["since"]:]]
        exp = self.exp.get(name, {})
        n = len(rets)
        mean = sum(rets) / n if n else 0.0
        cum = peak = dd = 0.0
        for r in rets:
            cum += r
            peak = max(peak, cum)
            dd = min(dd, cum - peak)
        cur_dd = cum - peak
        z = (mean - exp["mean"]) / (exp["std"] / math.sqrt(n)) if n and exp.get("std") else 0.0
        return {"n": n, "mean": mean, "cum": cum, "dd": cur_dd, "max_dd": dd, "z": z,
                "pnl": sum(t["pnl"] for t in e["trades"][e["since"]:])}

    def _evaluate(self, name: str) -> None:
        exp = self.exp.get(name)
        if not exp:
            return
        e, s = self.edges[name], self.stats(name)
        n, z, dd = s["n"], s["z"], s["dd"]
        if e["status"] == "off":
            return
        if (n >= 15 and z <= -3) or dd <= exp["dd_off"]:
            why = f"z {z:.1f} after {n} trades" if n >= 15 and z <= -3 else f"drawdown {dd:.1%} beyond the backtest's 1-in-100 year ({exp['dd_off']:.1%})"
            self._set(name, "off", why)
        elif (n >= 8 and z <= -2) or dd <= exp["dd_reduce"]:
            why = f"z {z:.1f} after {n} trades" if n >= 8 and z <= -2 else f"drawdown {dd:.1%} beyond the backtest's 1-in-10 year ({exp['dd_reduce']:.1%})"
            self._set(name, "reduced", why)
        elif e["status"] == "reduced" and not (z > -1.5 and dd > exp["dd_reduce"] / 2):
            pass  # hysteresis: stay at half size until clearly recovered
        else:
            self._set(name, "healthy", "" if e["status"] == "healthy" else "recovered")

    def _set(self, name: str, status: str, reason: str) -> None:
        e = self.edges[name]
        if e["status"] != status:
            level = logging.WARNING if status != "healthy" else logging.INFO
            self.log(f"Edge monitor: {name} {e['status']} -> {status} ({reason}); size x{SIZE[status]:g}", level)
            e["changed"] = datetime.now(ET).isoformat(timespec="seconds")
        e["status"], e["reason"] = status, reason

    def snapshot(self) -> list[dict]:
        out = []
        for name in sorted(set(self.exp) | set(self.edges)):
            exp, e = self.exp.get(name, {}), self.edges.get(name, {})
            out.append({"name": name, "label": exp.get("label", name), "status": e.get("status", "healthy"),
                        "size": self.multiplier(name), "reason": e.get("reason", ""), "changed": e.get("changed", ""),
                        "live": self.stats(name), "backtest": exp})
        return out
