"""Trade diagnostics: preparation, buckets, the out-of-sample lever verdicts
and an end-to-end run on a synthetic log."""

import contextlib
import io
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

import trade_diagnostics as td

DAYS = [str(d.date()) for d in pd.bdate_range("2026-01-05", periods=60)]


def trade(date, entry_time="10:00", exit_time="15:15", side="LONG", gross=10.0, cost=18.0,
          strategy="orb45", reason="squareoff", slip=0.03, sl=1.5, deployed=9500):
    return dict(date=date, strategy=strategy, symbol="X", side=side, entry_time=entry_time,
                exit_time=exit_time, reason=reason, gross=gross, cost=cost, pnl=gross - cost,
                slip_pct=slip, sl_pct=sl, deployed=deployed, ambiguous=False)


class TestPrepare(unittest.TestCase):
    def test_held_minutes_and_inside_costs(self):
        t = td.prepare(pd.DataFrame([trade(DAYS[0], "10:00", "11:30", gross=10, cost=18),
                                     trade(DAYS[0], "09:45", "15:15", gross=-30, cost=18)]))
        self.assertEqual(list(t.held_min), [90, 330])
        self.assertEqual(list(t.inside_costs), [True, False])

    def test_missing_columns_do_not_break_it(self):
        t = td.prepare(pd.DataFrame([dict(date=DAYS[0], strategy="orb45", gross=1.0, cost=2.0, pnl=-1.0)]))
        self.assertTrue(np.isnan(t.held_min.iloc[0]))

    def test_rvol_rows_are_not_separate_strategies(self):
        t = pd.DataFrame([trade(DAYS[0], strategy=s) for s in ("ema", "orb45_rvol", "orb45", "vwap")])
        self.assertEqual(td.base_strategies(t), ["orb45", "ema", "vwap"])

    def test_split_is_chronological(self):
        early, late = td.split_sessions(list(reversed(DAYS[:9])))
        self.assertEqual(early, DAYS[:6])
        self.assertEqual(late, DAYS[6:9])


class TestBuckets(unittest.TestCase):
    def test_edges(self):
        t = td.add_buckets(td.prepare(pd.DataFrame([
            trade(DAYS[0], "09:59", "10:20", sl=0.99, deployed=6999),
            trade(DAYS[0], "10:00", "11:30", sl=1.0, deployed=7000),
            trade(DAYS[0], "13:00", "15:15", sl=3.0, deployed=9000)])))
        self.assertEqual(list(t.entry_bucket), ["before 10:00", "10:00-10:30", "13:00 on"])
        self.assertEqual(list(t.held_bucket), ["< 30 min", "1-2 h", "2-4 h"])
        self.assertEqual(list(t.stop_bucket), ["< 1%", "1-2%", ">= 3%"])
        self.assertEqual(list(t.size_bucket), ["< Rs7k", "Rs7-9k", ">= Rs9k"])

    def test_group_stats(self):
        t = td.prepare(pd.DataFrame([trade(DAYS[0], side="LONG", gross=40),
                                     trade(DAYS[0], side="LONG", gross=0),
                                     trade(DAYS[0], side="SHORT", gross=-20)]))
        g = td.group_stats(t, "side", ["LONG", "SHORT"]).set_index("group")
        self.assertEqual(g.loc["LONG", "trades"], 2)
        self.assertAlmostEqual(g.loc["LONG", "gross_tr"], 20.0)
        self.assertAlmostEqual(g.loc["LONG", "win"], 50.0)
        self.assertAlmostEqual(g.loc["SHORT", "net"], -38.0)


def two_sided_log(early_kept, early_dropped, late_kept, late_dropped, per_day=4, noise=5.0, seed=0):
    """Morning entries (kept by 'before 11:00') and afternoon entries, with
    chosen mean gross in each half."""
    rng = np.random.default_rng(seed)
    rows = []
    early, late = td.split_sessions(DAYS)
    for d in DAYS:
        k, dr = (early_kept, early_dropped) if d in early else (late_kept, late_dropped)
        for _ in range(per_day):
            rows.append(trade(d, "10:15", gross=k + rng.normal(0, noise)))
            rows.append(trade(d, "13:30", gross=dr + rng.normal(0, noise)))
    return td.prepare(pd.DataFrame(rows))


class TestLevers(unittest.TestCase):
    def verdict(self, t):
        mask = t["entry_time"] < "11:00"
        return td.lever_verdict(td.lever_result(t, mask, DAYS))

    def test_profitable_out_of_sample(self):
        self.assertTrue(self.verdict(two_sided_log(60, 0, 60, 0)).startswith("PROFITABLE"))

    def test_better_but_still_losing(self):
        self.assertEqual(self.verdict(two_sided_log(15, 0, 15, 0)),
                         "picks better trades, still not profitable")

    def test_only_in_the_earlier_part_is_rejected(self):
        self.assertEqual(self.verdict(two_sided_log(60, 0, 0, 60)),
                         "no: no clear difference in the later third")
        self.assertEqual(self.verdict(two_sided_log(0, 60, 60, 0)), "no: not better in the earlier part")

    def test_pure_noise_is_screened_out(self):
        passed = 0
        for seed in range(40):
            v = self.verdict(two_sided_log(10, 10, 10, 10, noise=80.0, seed=seed))
            passed += not v.startswith("no:")
        self.assertLessEqual(passed, 4, "a no-difference lever should rarely pass")

    def test_too_few_trades(self):
        t = two_sided_log(60, 0, 60, 0, per_day=1)
        t = t[t.date.isin(DAYS[:30]) | (t.entry_time == "13:30")]
        self.assertEqual(self.verdict(t), "too few trades on one side")

    def test_late_interval_uses_only_late_sessions(self):
        t = two_sided_log(-500, 0, 60, 0)
        r = td.lever_result(t, t["entry_time"] < "11:00", DAYS)
        self.assertGreater(r["late"]["lo"], 0, "early losses must not leak into the late interval")


class TestEndToEnd(unittest.TestCase):
    def test_report_runs(self):
        rng = np.random.default_rng(3)
        rows = []
        for d in DAYS:
            for s in ("orb45", "ema", "orb45_rvol"):
                for _ in range(3):
                    rows.append(trade(d, rng.choice(["09:45", "10:15", "11:30", "13:15"]),
                                      side=rng.choice(["LONG", "SHORT"]), gross=float(rng.normal(12, 80)),
                                      strategy=s, reason=rng.choice(["squareoff", "stoploss", "target"]),
                                      slip=float(rng.choice([0.03, 0.05])), sl=float(rng.uniform(0.5, 3.5)),
                                      deployed=float(rng.uniform(6000, 10000))))
        with tempfile.TemporaryDirectory() as tmp:
            pd.DataFrame(rows).to_csv(Path(tmp) / "strategy_trades.csv", index=False)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                td.run(None, "orb45", root=Path(tmp))
        text = out.getvalue()
        for part in ("TRADE DIAGNOSTICS", "A. Where the money goes", "B. orb45 in detail",
                     "by entry time", "by stop distance", "C. Tuning levers", "entries before 11:00",
                     "slot at least 90% used", "No orders placed"):
            self.assertIn(part, text)
        self.assertNotIn("orb45_rvol ", text.split("C. Tuning")[0].split("A. Where")[1])
        self.assertNotIn("nan", text.lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)
