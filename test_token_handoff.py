"""The contract run-backtest.ps1 depends on when it exchanges a request_token.

A Kite request_token is single-use and short-lived. The runner spends it in its
preflight, so it must carry the resulting access_token forward to the backtest
process instead of letting that process attempt the same exchange again. These
pin the two facts that makes possible:

  1. kite_client() exchanges a request_token and leaves the session token
     readable on the returned client as `.access_token`
  2. an access_token in the environment takes precedence, so the handoff
     actually suppresses a second exchange
"""

import os
import sys
import types
import unittest

import kite_data


class FakeKiteConnect:
    """Stands in for kiteconnect.KiteConnect, counting session exchanges."""

    exchanges = 0

    def __init__(self, api_key=None):
        self.api_key = api_key
        self.access_token = None

    def generate_session(self, request_token, api_secret=None):
        type(self).exchanges += 1
        if request_token == "SPENT":
            raise RuntimeError("Token is invalid or has expired.")
        return {"access_token": f"session-for-{request_token}"}

    def set_access_token(self, token):
        self.access_token = token


class TokenHandoffTests(unittest.TestCase):
    def setUp(self):
        FakeKiteConnect.exchanges = 0
        self._saved_env = {k: os.environ.get(k) for k in
                           ("KITE_API_KEY", "KITE_API_SECRET", "KITE_ACCESS_TOKEN",
                            "KITE_REQUEST_TOKEN", "KITE_CREDENTIALS_FILE")}
        for k in self._saved_env:
            os.environ.pop(k, None)
        os.environ["KITE_API_KEY"] = "testkey"
        os.environ["KITE_API_SECRET"] = "testsecret"
        # kite_client imports kiteconnect lazily, so a stub module is enough.
        self._saved_mod = sys.modules.get("kiteconnect")
        mod = types.ModuleType("kiteconnect")
        mod.KiteConnect = FakeKiteConnect
        sys.modules["kiteconnect"] = mod

    def tearDown(self):
        for k, v in self._saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        if self._saved_mod is None:
            sys.modules.pop("kiteconnect", None)
        else:
            sys.modules["kiteconnect"] = self._saved_mod

    def test_request_token_is_exchanged_and_token_readable(self):
        os.environ["KITE_REQUEST_TOKEN"] = "fresh"
        client = kite_data.kite_client()
        self.assertEqual(FakeKiteConnect.exchanges, 1)
        self.assertEqual(client.access_token, "session-for-fresh",
                         "the runner reads this to hand the session forward")

    def test_access_token_env_suppresses_a_second_exchange(self):
        """This is what the handoff buys: the backtest process must not try to
        spend a request_token that the preflight already consumed."""
        os.environ["KITE_ACCESS_TOKEN"] = "session-for-fresh"
        os.environ["KITE_REQUEST_TOKEN"] = "fresh"
        client = kite_data.kite_client()
        self.assertEqual(FakeKiteConnect.exchanges, 0,
                         "an access_token must win over a request_token")
        self.assertEqual(client.access_token, "session-for-fresh")

    def test_reusing_a_spent_token_fails(self):
        """Without the handoff this is the failure mode, and it is not silent."""
        os.environ["KITE_REQUEST_TOKEN"] = "SPENT"
        with self.assertRaises(RuntimeError) as ctx:
            kite_data.kite_client()
        self.assertIn("expired", str(ctx.exception))

    def test_missing_token_names_the_remedy(self):
        with self.assertRaises(kite_data.KiteConfigurationError) as ctx:
            kite_data.kite_client()
        msg = str(ctx.exception)
        self.assertIn("06:00 IST", msg)
        self.assertIn("KITE_REQUEST_TOKEN", msg)

    def test_request_token_without_secret_is_rejected(self):
        os.environ.pop("KITE_API_SECRET")
        os.environ["KITE_REQUEST_TOKEN"] = "fresh"
        with self.assertRaises(kite_data.KiteConfigurationError):
            kite_data.kite_client()
        self.assertEqual(FakeKiteConnect.exchanges, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
