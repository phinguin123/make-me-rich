from trader.research.intraday_relay import SAFE, relay_day


def sip(t, sym="X", entry=100.0, exit_=101.0, exit_min=700):
    return {"entry_min": t, "exit_min": exit_min, "entry": entry, "stop0": entry - 2.0, "exit": exit_, "symbol": sym}


def test_stocks_in_play_only_uses_cash_the_book_freed():
    book = [(60, 390, 0.01), (70, 390, 0.0), (80, 340, 0.02)]  # three slots: two held to 10:30, one target filled at 09:40
    res = relay_day(3000.0, book, False, None, [sip(336, "A"), sip(345, "B"), sip(395, "C")], SAFE)
    # 09:36: no free cash yet -> skipped; 09:45: the 09:40 take-profit freed ~$1,020; 10:35: the 10:30 exits freed ~$2,010
    assert res["sip_n"] == 2 and res["sip_skipped"] == 1
    assert abs(res["book"] - (10.0 + 0.0 + 20.0)) < 1e-9


def test_position_cap_and_risk_sizing():
    res = relay_day(3000.0, [], False, None, [sip(336, "A", entry=10.0, exit_=10.0)], SAFE)
    # risk 1% of $3,000 over a $2 stop = 15 shares; max 25% of the pot = 75 shares -> 15 shares, flat price: fees only
    assert res["sip_n"] == 1 and -1.0 < res["sip"] < 0


def test_bear_days_run_the_breakout_on_the_whole_pot_and_ignore_stocks_in_play():
    res = relay_day(3000.0, [(60, 359, 0.01)], True, 0.02, [sip(336, "A")], SAFE)
    assert res["sip_n"] == 0 and abs(res["orb"] - 60.0) < 1e-9 and abs(res["book"] - 10.0) < 1e-9
