"""Calendar data: log discovery, daily roll-up, summary, and the HTTP route."""

import json
import os
import shutil
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pandas as pd

import calendar_data as cd


def write(path, rows):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    pd.DataFrame(rows).to_csv(path, index=False)


COMPARE = [
    dict(date="2026-08-03", strategy="orb45", symbol="A.NS", side="LONG", qty=10,
         entry=100, exit=101, entry_time="10:00", exit_time="15:15", reason="squareoff",
         gross=10.0, cost=4.0, pnl=6.0),
    dict(date="2026-08-03", strategy="orb45", symbol="B.NS", side="SHORT", qty=5,
         entry=200, exit=202, entry_time="10:15", exit_time="11:00", reason="stoploss",
         gross=-10.0, cost=4.0, pnl=-14.0),
    dict(date="2026-08-04", strategy="orb45", symbol="A.NS", side="LONG", qty=10,
         entry=100, exit=103, entry_time="10:00", exit_time="12:00", reason="target",
         gross=30.0, cost=4.0, pnl=26.0),
    dict(date="2026-08-05", strategy="orb45", symbol="A.NS", side="LONG", qty=10,
         entry=100, exit=99, entry_time="10:00", exit_time="15:15", reason="squareoff",
         gross=-10.0, cost=4.0, pnl=-14.0),
    dict(date="2026-08-03", strategy="ema", symbol="C.NS", side="LONG", qty=1,
         entry=50, exit=55, entry_time="11:00", exit_time="13:00", reason="signal",
         gross=5.0, cost=1.0, pnl=4.0),
]


class RootCase(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def p(self, *parts):
        return os.path.join(self.root, *parts)


class TestLogs(RootCase):
    def test_discovers_every_kind_paper_first(self):
        write(self.p("strategy_trades.csv"), COMPARE)
        write(self.p("orb_trades.csv"), [dict(COMPARE[0], strategy=None)])
        write(self.p("paper", "ledger.csv"), [dict(plan_date="2026-09-01", pnl=1.0)])
        write(self.p("backtests", "strategy_trades_2026-10-01_093522.csv"), COMPARE)
        ids = [l["id"] for l in cd.available_logs(self.root)]
        self.assertEqual(ids, ["paper", "compare", "orb",
                               "archive:strategy_trades_2026-10-01_093522.csv"])
        archived = cd.available_logs(self.root)[-1]
        self.assertEqual(archived["kind"], "compare")
        self.assertIn("2026-10-01_093522", archived["label"])

    def test_nothing_on_disk(self):
        self.assertEqual(cd.available_logs(self.root), [])
        self.assertEqual(cd.calendar_payload(self.root)["logs"], [])


class TestDailyAndSummary(RootCase):
    def setUp(self):
        super().setUp()
        write(self.p("strategy_trades.csv"), COMPARE)

    def test_days_are_summed_across_positions(self):
        out = cd.calendar_payload(self.root, "compare", "orb45")
        first = out["days"][0]
        self.assertEqual(first, dict(date="2026-08-03", pnl=-8.0, gross=0.0, cost=8.0,
                                     trades=2, wins=1))
        self.assertEqual([d["date"] for d in out["days"]],
                         ["2026-08-03", "2026-08-04", "2026-08-05"])

    def test_summary(self):
        s = cd.calendar_payload(self.root, "compare", "orb45")["summary"]
        self.assertEqual((s["sessions"], s["profitable_days"], s["losing_days"]), (3, 1, 2))
        self.assertAlmostEqual(s["net"], 4.0)
        self.assertEqual(s["best"], dict(date="2026-08-04", pnl=26.0))
        self.assertEqual(s["worst"]["pnl"], -14.0)
        self.assertEqual(s["longest_winning_streak"], 1)
        self.assertEqual(s["longest_losing_streak"], 1)

    def test_strategies_are_separate(self):
        out = cd.calendar_payload(self.root, "compare", "ema")
        self.assertEqual(out["strategies"], ["ema", "orb45"])
        self.assertEqual(len(out["days"]), 1)

    def test_trades_for_the_drilldown(self):
        trades = cd.calendar_payload(self.root, "compare", "orb45")["trades"]
        self.assertEqual(len(trades["2026-08-03"]), 2)
        self.assertEqual(trades["2026-08-04"][0]["reason"], "target")

    def test_defaults_to_first_log_and_strategy(self):
        out = cd.calendar_payload(self.root)
        self.assertEqual((out["log"]["id"], out["strategy"]), ("compare", "orb45"),
                         "opens on the baseline, not the alphabetically first name")

    def test_unknown_ids_are_refused_not_resolved_as_paths(self):
        with self.assertRaises(ValueError):
            cd.calendar_payload(self.root, "../../etc/passwd")
        with self.assertRaises(ValueError):
            cd.calendar_payload(self.root, "compare", "nope")

    def test_paths_never_reach_the_client(self):
        out = cd.calendar_payload(self.root)
        self.assertNotIn("path", out["log"])
        self.assertTrue(all("path" not in l for l in out["logs"]))

    def test_payload_is_strict_json_even_with_gaps(self):
        """json.dumps(default=str) writes NaN as a token browsers reject."""
        rows = [dict(r) for r in COMPARE]
        rows[0]["entry_time"] = None
        rows[0]["qty"] = None
        write(self.p("strategy_trades.csv"), rows)
        out = cd.calendar_payload(self.root, "compare", "orb45")
        json.dumps(out, allow_nan=False)          # raises on NaN or inf
        self.assertIsNone(out["trades"]["2026-08-03"][0]["entry_time"])
        self.assertIsInstance(out["days"][0]["trades"], int, "numbers stay numbers")


class TestOtherKinds(RootCase):
    def test_paper_ledger_uses_plan_date(self):
        write(self.p("paper", "ledger.csv"),
              [dict(plan_date="2026-09-01", symbol="A.NS", pnl=5.0, gross=7.0, cost=2.0),
               dict(plan_date="2026-09-02", symbol="A.NS", pnl=-3.0, gross=-1.0, cost=2.0)])
        out = cd.calendar_payload(self.root, "paper")
        self.assertEqual(out["strategies"], ["paper"])
        self.assertEqual([d["date"] for d in out["days"]], ["2026-09-01", "2026-09-02"])

    def test_orb_log_without_strategy_column(self):
        write(self.p("orb_trades.csv"),
              [{k: v for k, v in r.items() if k != "strategy"} for r in COMPARE[:3]])
        out = cd.calendar_payload(self.root, "orb")
        self.assertEqual(out["strategies"], ["orb45"])


class TestRoute(RootCase):
    """The real handler over HTTP, pointed at a temp root."""

    def setUp(self):
        super().setUp()
        write(self.p("strategy_trades.csv"), COMPARE)
        import unified_api
        self.api = unified_api
        self._root = unified_api.ROOT
        unified_api.ROOT = self.root
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), unified_api.ApiHandler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.api.ROOT = self._root
        super().tearDown()

    def get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=5) as r:
            return r.status, json.loads(r.read())

    def test_default_and_query(self):
        status, body = self.get("/api/calendar")
        self.assertEqual(status, 200)
        self.assertEqual(body["log"]["id"], "compare")
        _, body = self.get("/api/calendar?log=compare&strategy=orb45")
        self.assertEqual(len(body["days"]), 3)

    def test_bad_id_is_a_400_not_a_file_read(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.get("/api/calendar?log=..%2F..%2Fcreds.txt")
        self.assertEqual(ctx.exception.code, 400)


if __name__ == "__main__":
    unittest.main(verbosity=2)
