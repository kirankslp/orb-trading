"""Relative opening volume: correctness and, above all, point-in-time safety."""

import datetime
import unittest

import numpy as np
import pandas as pd

import rvol

D0 = datetime.date(2026, 3, 2)


def sessions(opening_vols, n_or=3, later_vol=50.0, start=D0):
    """15m bars, one session per value: the first n_or bars split that
    session's opening volume, later bars carry `later_vol` each."""
    rows = []
    for k, ov in enumerate(opening_vols):
        day = start + datetime.timedelta(days=k)
        t0 = datetime.datetime.combine(day, datetime.time(9, 15))
        for i in range(8):
            t = t0 + datetime.timedelta(minutes=15 * i)
            v = ov / n_or if i < n_or else later_vol
            rows.append(dict(dt=t, date=day, time=t.strftime("%H:%M"), Open=100.0,
                             High=100.5, Low=99.5, Close=100.0, Volume=float(v)))
    return pd.DataFrame(rows)


class TestOpeningVolume(unittest.TestCase):
    def test_sums_only_the_opening_bars(self):
        vol = rvol.opening_volume(sessions([300.0]), 3)
        self.assertAlmostEqual(vol.iloc[0], 300.0, msg="later bars must not count")

    def test_short_session_is_left_out(self):
        df = sessions([300.0, 300.0])
        df = df[~((df.date == D0) & (df.time > "09:15"))]   # D0 has one opening bar
        vol = rvol.opening_volume(df, 3)
        self.assertNotIn(D0, vol.index)
        self.assertEqual(len(vol), 1)


class TestRVOL(unittest.TestCase):
    def test_spike_against_a_flat_baseline(self):
        r = rvol.opening_rvol({"S": sessions([100.0] * 12 + [400.0])}, 3)
        spike_day = D0 + datetime.timedelta(days=12)
        self.assertAlmostEqual(r[(spike_day, "S")], 4.0)

    def test_baseline_never_includes_the_day_itself(self):
        """With today in its own baseline a spike would read lower than it is."""
        r = rvol.opening_rvol({"S": sessions([100.0] * 10 + [1000.0])}, 3)
        self.assertAlmostEqual(r[(D0 + datetime.timedelta(days=10), "S")], 10.0)

    def test_median_resists_an_earlier_spike(self):
        vols = [100.0] * 11 + [5000.0] + [100.0]
        r = rvol.opening_rvol({"S": sessions(vols)}, 3)
        self.assertAlmostEqual(r[(D0 + datetime.timedelta(days=12), "S")], 1.0,
                               msg="one spike in the window must not set the baseline")

    def test_too_little_history_is_unknown(self):
        r = rvol.opening_rvol({"S": sessions([100.0] * 12)}, 3, min_history=10)
        known = sorted(d for d, _ in r)
        self.assertEqual(known[0], D0 + datetime.timedelta(days=10))

    def test_zero_baseline_is_unknown_not_infinite(self):
        r = rvol.opening_rvol({"S": sessions([0.0] * 11 + [100.0])}, 3)
        self.assertEqual(r, {})

    def test_later_bars_and_later_days_cannot_move_it(self):
        """Point in time: the value for day d must not change when anything
        after d's opening range changes, including later sessions."""
        base = sessions([100.0] * 11 + [300.0] + [100.0] * 3)
        d = D0 + datetime.timedelta(days=11)
        before = rvol.opening_rvol({"S": base}, 3)[(d, "S")]
        altered = base.copy()
        after_open = (altered.date == d) & (altered.time >= "10:00")
        later = altered.date > d
        altered.loc[after_open | later, "Volume"] = 1e9
        self.assertAlmostEqual(rvol.opening_rvol({"S": altered}, 3)[(d, "S")], before)

    def test_frames_without_volume_are_skipped(self):
        self.assertEqual(rvol.opening_rvol({"S": sessions([1.0]).drop(columns="Volume")}, 3), {})


class TestFilterAndBuckets(unittest.TestCase):
    def trades(self):
        return pd.DataFrame(dict(
            date=[D0] * 5, symbol=["A", "B", "C", "D", "E"],
            pnl=[10.0, -5.0, 20.0, -8.0, 3.0], gross=[15.0, 0.0, 25.0, -3.0, 8.0],
            reason=["target", "stoploss", "squareoff", "stoploss", "squareoff"]))

    def test_annotate_marks_missing_as_nan(self):
        t = rvol.annotate(self.trades(), {(D0, "A"): 2.5, (D0, "B"): 0.5})
        self.assertEqual(t.rvol.iloc[0], 2.5)
        self.assertTrue(np.isnan(t.rvol.iloc[2]))

    def test_in_play_is_inclusive_and_excludes_unknown(self):
        t = rvol.annotate(self.trades(), {(D0, "A"): 2.0, (D0, "B"): 1.99, (D0, "C"): 5.0})
        self.assertEqual(sorted(rvol.in_play(t).symbol), ["A", "C"])

    def test_bucket_edges(self):
        self.assertEqual([rvol.bucket_of(x) for x in (np.nan, 0.5, 1.0, 1.99, 2.0, 2.99, 3.0, 9.0)],
                         ["unknown", "< 1x", "1-2x", "1-2x", "2-3x", "2-3x", ">= 3x", ">= 3x"])

    def test_bucket_table_with_moves(self):
        t = rvol.annotate(self.trades(), {(D0, "A"): 3.5, (D0, "B"): 3.1, (D0, "C"): 0.4})
        moves = pd.DataFrame(dict(date=[D0] * 3, symbol=["A", "B", "C"],
                                  mfe_pct=[2.0, 1.0, 0.2]))
        tab = rvol.bucket_table(t, moves).set_index("bucket")
        self.assertEqual(tab.loc[">= 3x", "trades"], 2)
        self.assertAlmostEqual(tab.loc[">= 3x", "gross_tr"], 7.5)
        self.assertAlmostEqual(tab.loc[">= 3x", "move"], 1.5)
        self.assertEqual(tab.loc[">= 3x", "target"], 50.0)
        self.assertEqual(tab.loc["unknown", "trades"], 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
