import pytest

from bot.risk import lots_for_risk, MIN_STOP_POINTS


def test_normal_case_scales_with_risk_amount():
    # $25 risk, 50-point stop, $1/point/lot -> 0.5 lot
    assert lots_for_risk(25.0, 50.0, 1.0, min_lot=0.01) == 0.5


def test_wide_stop_can_size_below_min_lot_floor():
    # a very wide stop would risk-size to less than the broker minimum ->
    # floor at min_lot ("0.25% risk, or 0.01 if it doesn't fit")
    lots = lots_for_risk(25.0, 100000.0, 1.0, min_lot=0.01)
    assert lots == 0.01


def test_narrow_stop_sizes_up_from_min_lot():
    # a tight stop lets us take a bigger lot for the same dollar risk
    lots = lots_for_risk(25.0, 5.0, 1.0, min_lot=0.01)
    assert lots == 5.0


def test_near_zero_stop_distance_floors_to_min_lot():
    lots = lots_for_risk(25.0, MIN_STOP_POINTS / 2, money_per_point_per_lot=1.0, min_lot=0.01)
    assert lots == 0.01


def test_zero_or_negative_stop_distance_floors_to_min_lot():
    assert lots_for_risk(25.0, 0.0, 1.0, min_lot=0.01) == 0.01
    assert lots_for_risk(25.0, -5.0, 1.0, min_lot=0.01) == 0.01


def test_missing_money_per_point_floors_to_min_lot():
    assert lots_for_risk(25.0, 50.0, 0.0, min_lot=0.01) == 0.01
    assert lots_for_risk(25.0, 50.0, None, min_lot=0.01) == 0.01


def test_non_positive_risk_amount_floors_to_min_lot():
    assert lots_for_risk(0.0, 50.0, 1.0, min_lot=0.01) == 0.01
    assert lots_for_risk(-10.0, 50.0, 1.0, min_lot=0.01) == 0.01


def test_realistic_ger40_shape_2026_07_21_actual_stops():
    # Real entry/shared-stop pairs logged live on 2026-07-21
    # (reports/logs/S007/positions/S007_2026-07-21_{0,39,74,79}.jsonl), all
    # sharing one stop at 24850.35 (the range midpoint). money_per_point_per_lot
    # ~114.3 is the bot/ctrader_s007.py-derived value (full_symbol.lotSize),
    # close to the ~112-145 range back-computed from that day's real $ P&L.
    # 0.25% of a $10k balance = $25 target risk. Only the TIGHTEST stop
    # (:74, 14.75 pts) sizes above the 0.01 floor -- the other three (28.75,
    # 30.75, 43.75 pts) are all wider than the ~21.9-point breakeven and floor
    # at 0.01, same as FIXED_LOT would have given them anyway.
    balance, risk_pct, ppp, min_lot = 10_000.0, 0.25, 114.3, 0.01
    risk_amount = balance * risk_pct / 100.0
    stops = {
        "S007:2026-07-21:0": 43.75,
        "S007:2026-07-21:39": 30.75,
        "S007:2026-07-21:74": 14.75,
        "S007:2026-07-21:79": 28.75,
    }
    lots = {lab: lots_for_risk(risk_amount, d, ppp, min_lot) for lab, d in stops.items()}
    assert lots["S007:2026-07-21:0"] == min_lot
    assert lots["S007:2026-07-21:39"] == min_lot
    assert lots["S007:2026-07-21:79"] == min_lot
    assert lots["S007:2026-07-21:74"] > min_lot   # the only one that sizes up
    assert lots["S007:2026-07-21:74"] == pytest.approx(25.0 / (14.75 * 114.3))


def test_realistic_ger40_shape_hypothetical_wide_stop_floors_to_min_lot():
    # a wide, day-height-filtered ~250-point stop (wider than anything seen
    # 2026-07-21) makes the risk-sized lot even smaller -- still floors to 0.01.
    balance = 10_000.0
    risk_amount = balance * 0.25 / 100.0
    lots = lots_for_risk(risk_amount, 250.0, money_per_point_per_lot=114.3, min_lot=0.01)
    assert lots == 0.01
