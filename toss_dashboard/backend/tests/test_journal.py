import json

from trader.journal import J, jr, summarize


def test_journal_lines_legend_and_digest():
    J.write_legend()
    jr("H", "abc123", "paper", {"scalp": 6254.14})
    jr("B", "09:30", "SOXL", 120.1, 121.0, 119.5, 120.8, 150000, 120.4, 900, "sip")
    jr("B", "09:31", "SOXL", 120.8, 122.2, 120.6, 122.0, 98000, 121.5, 700, "sip")
    jr("D", "pre", "trigger", {"level": 119.21, "last": 119.2, "vwap": 122.187451926, "qty": 52})
    jr("O", "ord-1", "SOXL", "BUY", 52, 119.21, "scalp-pre-entry")
    jr("F", "ord-1", "SOXL", "BUY", 52, 119.18, 119.17, 119.19, "scalp-pre-entry")
    jr("L", "W", "something odd")
    lines = [json.loads(x) for x in (J.dir / f"{J._day}.jsonl").read_text().splitlines()]
    assert lines[1][:3] == ["B", lines[1][1], "09:30"] and lines[3][4]["vwap"] == 122.1875  # rounded, compact
    assert (J.dir / "LEGEND.md").read_text().count("`F`") == 1
    digest = summarize(J._day, J.dir)
    assert "| SOXL | 09:30 | 120.1 | 122.2 | 119.5 | 122.0 | 2 |" in digest
    assert "FILL BUY SOXL 52 @ 119.18 (+0.000% vs mid)" in digest and "1x something odd" in digest


def test_journal_never_raises(tmp_path):
    J.dir = tmp_path / "file-not-dir"
    J.dir.write_text("x")  # a file where the folder should be: writes fail silently
    J._fh, J._day = None, ""
    jr("L", "I", "still fine")
