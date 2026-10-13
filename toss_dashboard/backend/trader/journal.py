"""Compact structured journal: everything the bot saw, decided and did, for later analysis.

Files: data/journal/YYYY-MM-DD.jsonl (ET date), one JSON array per line; the first element is the
record type and the field order is fixed by LEGEND, so lines stay short (no repeated key names).
At the end of each day a markdown digest is written next to it (YYYY-MM-DD.summary.md): read the
digest first, then drill into the raw lines only where needed. LEGEND.md in the same folder
explains every field; it is rewritten at every start, so it always matches the running code.

    python -m trader.journal 2026-10-13          # (re)build that day's digest
"""
from __future__ import annotations

import json
import logging
import sys
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import DATA_DIR

ET = ZoneInfo("America/New_York")
DIR = DATA_DIR / "journal"
LOG = logging.getLogger(__name__)
VERSION = 1

LEGEND: dict[str, tuple[list[str], str]] = {
    "H": (["t", "code_version", "mode", "settings"], "process start: code version and the live settings"),
    "B": (["t", "bar", "sym", "o", "h", "l", "c", "v", "vwap", "trades", "src"],
          "1-minute bar; bar = its START time HH:MM (ET), vwap = the bar's own VWAP, src = sip|toss"),
    "N": (["t", "sym", "bid", "ask", "last", "src"], "quote snapshot each minute for traded symbols (src = sip|toss)"),
    "D": (["t", "sleeve", "event", "data"], "decision or state change; data = dict of the numbers behind it (see EVENTS)"),
    "O": (["t", "id", "sym", "side", "qty", "limit", "tag"], "order sent; limit null = market; amount orders: qty = -$amount"),
    "F": (["t", "id", "sym", "side", "qty", "px", "bid", "ask", "tag"], "fill (partial fills are separate lines); bid/ask = NBBO at that moment"),
    "X": (["t", "id", "status", "err"], "order ended without a full fill: CANCELED / REJECTED (+ error code)"),
    "P": (["t", "sleeve", "cash", "equity", "pos"], "sleeve snapshot every 5 min; pos = {sym: [qty, avg cost]}"),
    "L": (["t", "lvl", "msg"], "engine log line (I info, W warning, E error)"),
    "E": (["t", "where", "error"], "exception caught by the engine (it kept running)"),
}
EVENTS = {
    "pre": "pre-market dip: prepare{sigma,dip,status} vwap{vwap,level,last,bars,src} trigger{level,last,vwap,qty} "
           "target{entry,target} exit{reason,qty}",
    "orb": "opening-range breakout: regime{soxx,ma,regime,status} armed{hi,lo} trigger{side,px,hi,lo} stop{px}",
    "swing": "swing: model{as_of,exposure,n,weights} plan{sells,buys,total}",
    "alloc": "allocator: run{reason,total,scalp,swing,reserve} phase{phase,total} breaker{total,hwm,dd} wait{reason}",
    "edge": "edge monitor: status{edge,from,to,reason}",
}


def _round(v):
    if isinstance(v, float):
        return round(v, 6) if abs(v) < 1 else round(v, 4)
    if isinstance(v, dict):
        return {k: _round(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_round(x) for x in v]
    return v


class Journal:
    def __init__(self, directory: Path = DIR):
        self.dir = directory
        self._day = ""
        self._fh = None

    def _file(self, now: datetime):
        day = now.date().isoformat()
        if day != self._day or self._fh is None:
            if self._fh:
                self._fh.close()
            self.dir.mkdir(parents=True, exist_ok=True)
            self._fh = (self.dir / f"{day}.jsonl").open("a", encoding="utf-8")
            self._day = day
        return self._fh

    def write(self, kind: str, *fields) -> None:
        """Never raises: journaling must not be able to break trading."""
        try:
            now = datetime.now(ET)
            line = json.dumps([kind, now.strftime("%H:%M:%S"), *_round(list(fields))], separators=(",", ":"), default=str)
            fh = self._file(now)
            fh.write(line + "\n")
            fh.flush()
        except Exception as exc:  # noqa: BLE001
            LOG.warning("journal write failed: %s", exc)

    def write_legend(self) -> None:
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            lines = [f"# Journal legend (v{VERSION})", "",
                     "Each line of YYYY-MM-DD.jsonl is a JSON array: [type, t, ...fields]. t = ET time HH:MM:SS",
                     "(the date is the file name). Prices 4 decimals, small numbers 6. Read YYYY-MM-DD.summary.md first.", ""]
            lines += [f"- `{k}` {desc}: `[{', '.join(['type'] + f)}]`" for k, (f, desc) in LEGEND.items()]
            lines += ["", "Decision events (`D` records, field `data`):", ""] + [f"- `{k}`: {v}" for k, v in EVENTS.items()]
            (self.dir / "LEGEND.md").write_text("\n".join(lines) + "\n")
        except Exception as exc:  # noqa: BLE001
            LOG.warning("legend write failed: %s", exc)


J = Journal()


def jr(kind: str, *fields) -> None:
    J.write(kind, *fields)


# ── daily digest ──────────────────────────────────────────────────────────
def summarize(day: date | str, directory: Path = DIR) -> str:
    day = str(day)
    path = directory / f"{day}.jsonl"
    if not path.exists():
        return f"# {day}\n\nno journal\n"
    recs = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        try:
            recs.append(json.loads(raw))
        except ValueError:
            continue
    by = defaultdict(list)
    for r in recs:
        by[r[0]].append(r)
    out = [f"# Trading day {day} (ET) — digest of {len(recs)} journal lines", ""]
    starts = by.get("H", [])
    if starts:
        out.append(f"Process starts: {len(starts)} ({', '.join(r[1] + ' ' + str(r[2]) for r in starts)})")
    # bars: open/high/low/close per symbol for the day
    bars = defaultdict(list)
    for r in by.get("B", []):
        bars[r[3]].append(r)
    if bars:
        out += ["", "## Prices (1-minute bars logged)", "", "| sym | first bar | open | high | low | last close | bars |", "|---|---|---|---|---|---|---|"]
        for sym, bs in sorted(bars.items()):
            bs.sort(key=lambda b: b[2])
            out.append(f"| {sym} | {bs[0][2]} | {bs[0][4]} | {max(b[5] for b in bs)} | {min(b[6] for b in bs)} | {bs[-1][7]} | {len(bs)} |")
    # decisions
    if by.get("D"):
        out += ["", "## Decisions", ""]
        last_vwap = {}
        for r in by["D"]:
            if r[2] == "pre" and r[3] == "vwap":
                last_vwap = r[4]
                continue
            out.append(f"- {r[1]} {r[2]}.{r[3]} {json.dumps(r[4], separators=(',', ':'))}")
        if last_vwap:
            out.append(f"- last pre-market VWAP reading: {json.dumps(last_vwap, separators=(',', ':'))}")
    # orders and fills
    if by.get("O") or by.get("F"):
        out += ["", "## Orders and fills", ""]
        for r in sorted(by.get("O", []) + by.get("F", []) + by.get("X", []), key=lambda r: r[1]):
            if r[0] == "O":
                out.append(f"- {r[1]} ORDER {r[4]} {r[3]} qty {r[5]} limit {r[6]} [{r[7]}] id {str(r[2])[:10]}")
            elif r[0] == "F":
                bid, ask = r[7], r[8]
                cost = ""
                if bid and ask:
                    mid = (bid + ask) / 2
                    cost = f" ({(r[6] / mid - 1) * (1 if r[4] == 'BUY' else -1):+.3%} vs mid)"
                out.append(f"- {r[1]} FILL {r[4]} {r[3]} {r[5]} @ {r[6]}{cost} [{r[9]}]")
            else:
                out.append(f"- {r[1]} {r[3]} id {str(r[2])[:10]} {r[4] or ''}")
    # end-of-day sleeve snapshot
    snaps = {}
    for r in by.get("P", []):
        snaps.setdefault(r[2], []).append(r)
    if snaps:
        out += ["", "## Sleeves (first -> last snapshot)", "", "| sleeve | equity first | equity last | change | cash last | positions last |", "|---|---|---|---|---|---|"]
        for s, rs in snaps.items():
            a, b = rs[0][4], rs[-1][4]
            chg = f"{b / a - 1:+.2%}" if a else ""
            out.append(f"| {s} | {a} | {b} | {chg} | {rs[-1][3]} | {json.dumps(rs[-1][5], separators=(',', ':'))} |")
    warn = [r for r in by.get("L", []) if r[2] in ("W", "E")]
    if warn or by.get("E"):
        out += ["", f"## Warnings / errors ({len(warn)} log, {len(by.get('E', []))} exceptions)", ""]
        counts = Counter(r[3][:120] for r in warn)
        out += [f"- {n}x {msg}" for msg, n in counts.most_common(25)]
        out += [f"- {r[1]} EXC {r[2]}: {r[3]}" for r in by.get("E", [])[:25]]
    out += ["", f"Raw lines by type: {dict(Counter(r[0] for r in recs))}. Legend: LEGEND.md", ""]
    return "\n".join(out)


def write_summary(day: date | str, directory: Path = DIR) -> Path:
    path = directory / f"{day}.summary.md"
    path.write_text(summarize(day, directory))
    return path


if __name__ == "__main__":
    print(write_summary(sys.argv[1] if len(sys.argv) > 1 else datetime.now(ET).date().isoformat()))
