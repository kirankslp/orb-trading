"""Exit sweep: replacement levels, safe swapping, the engine actually
changing behaviour, and the out-of-sample verdicts."""

import datetime
import unittest

import numpy as np
import pandas as pd

import exit_sweep as es
import orb_backtest as ob

DAY = datetime.date(2026, 8, 14)


def dip_day():
    """15m session: breaks up at bar 3 (entry 100.25), dips about 2.8% at bar 6, then
    climbs to 104 by the 15:15 bar."""
    t0 = datetime.datetime.combine(DAY, datetime.time(9, 15))
    closes = [100.0, 100.2, 100.1, 101.0, 101.0, 100.5, 97.5, 100.0] + \
             [100.0 + 0.2 * i for i in range(1, 18)]
    rows, prev = [], 100.0
    for i, c in enumerate(closes[:25]):
        t = t0 + datetime.timedelta(minutes=15 * i)
        rows.append(dict(dt=t, date=DAY, time=t.strftime("%H:%M"), Open=prev,
                         High=max(prev, c) + 0.05, Low=min(prev, c) - 0.05, Close=c, Volume=1000))
        prev = c
    return pd.DataFrame(rows)


class TestLevels(unittest.TestCase):
    def test_multiples_and_the_engines_link(self):
        sl, tgt = es.make_levels(0.75, 1.0)(4.0)
        self.assertAlmostEqual(sl, 0.03)
        self.assertAlmostEqual(tgt, 0.04)

    def test_none_means_out_of_reach(self):
        self.assertEqual(es.make_levels(None, 1.0)(4.0), (es.FAR, 0.04))
        self.assertEqual(es.make_levels(0.5, None)(4.0)[1], es.FAR)

    def test_finite_stops_keep_the_clamp(self):
        self.assertAlmostEqual(es.make_levels(1.0, 1.0)(20.0)[0], ob.ATR_BOUNDS[1])

    def test_missing_atr_falls_back_to_the_flat_levels(self):
        self.assertEqual(es.make_levels(0.75, 1.0)(None), (ob.SL_PCT, ob.TARGET_PCT))
        self.assertEqual(es.make_levels(None, None)(None), (es.FAR, es.FAR))

    def test_current_variant_is_the_engines_own(self):
        seen = es.with_levels(0.5, 1.0, lambda: ob.levels_for)
        self.assertIs(seen, ob.levels_for)

    def test_swap_is_undone_even_on_error(self):
        original = ob.levels_for

        def boom():
            raise RuntimeError("x")
        with self.assertRaises(RuntimeError):
            es.with_levels(None, None, boom)
        self.assertIs(ob.levels_for, original)


class TestEngineResponds(unittest.TestCase):
    def test_no_stop_rides_the_dip_to_the_square_off(self):
        g = dip_day()
        run = lambda: ob.trade_day(DAY, g, 3, "T", 100000, 2000, 4.0)
        current = es.with_levels(0.5, 1.0, run)
        loose = es.with_levels(None, None, run)
        self.assertEqual(current["reason"], "stoploss")
        self.assertEqual(loose["reason"], "squareoff")
        self.assertGreater(loose["pnl"], current["pnl"])


def variant_trades(per_day, sessions):
    """A trades frame with one row per session carrying that session's net."""
    return pd.DataFrame(dict(date=sessions, pnl=per_day, gross=np.array(per_day) + 18.0,
                             reason=["squareoff"] * len(sessions)))


class TestVerdicts(unittest.TestCase):
    def setUp(self):
        self.sessions = [str(d.date()) for d in pd.bdate_range("2026-01-05", periods=90)]
        rng = np.random.default_rng(0)
        self.base = rng.normal(-10, 30, 90)

    def rows(self, other):
        variants = {es.CURRENT: variant_trades(self.base, self.sessions),
                    (None, None): variant_trades(other, self.sessions)}
        return {r["key"]: r for r in es.evaluate(variants, self.sessions)}

    def test_current_row(self):
        self.assertEqual(es.verdict(self.rows(self.base)[es.CURRENT]), "current exits")

    def test_better_and_profitable(self):
        r = self.rows(self.base + 60)[(None, None)]
        self.assertEqual(es.verdict(r), "BETTER and PROFITABLE later: paper-test it")

    def test_better_but_losing(self):
        r = self.rows(self.base + 5)[(None, None)]
        self.assertEqual(es.verdict(r), "better than current, still not profitable")

    def test_only_early_gain_is_rejected(self):
        early, late = es.split_sessions(self.sessions)
        other = self.base + np.where(np.isin(self.sessions, early), 40, 0)
        self.assertEqual(es.verdict(self.rows(other)[(None, None)]), "no: no clear gain in the later third")

    def test_worse_early_is_rejected(self):
        self.assertEqual(es.verdict(self.rows(self.base - 5)[(None, None)]),
                         "no: not better in the earlier part")

    def test_late_difference_is_paired_by_session(self):
        r = self.rows(self.base + 5)[(None, None)]
        _, late = es.split_sessions(self.sessions)
        self.assertAlmostEqual(r["late_diff"], 5 * len(late))
        self.assertAlmostEqual(r["late_diff_lo"], 5 * len(late), places=6,
                               msg="a constant gain has no spread")


if __name__ == "__main__":
    unittest.main(verbosity=2)
