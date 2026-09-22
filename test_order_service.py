import unittest

from order_service import place_recommendation_order, validate_order


LONG = {"symbol": "INFY", "bias": "LONG"}


class FakeClient:
    def __init__(self):
        self.params = None

    def place_order(self, **params):
        self.params = params
        return "260907000001"


class OrderServiceTests(unittest.TestCase):
    def test_confirmed_buy_stop_limit_is_submitted(self):
        client = FakeClient()
        payload = {"confirmed": True, "side": "BUY", "quantity": 3, "product": "MIS",
                   "order_type": "SL", "price": 1510.0, "trigger_price": 1509.5}
        result = place_recommendation_order(client, LONG, payload, 1500, 0.05)
        self.assertEqual(result["order_id"], "260907000001")
        self.assertEqual(client.params["variety"], "regular")
        self.assertEqual(client.params["order_type"], "SL")

    def test_confirmation_and_side_are_enforced(self):
        base = {"confirmed": False, "side": "BUY", "quantity": 1, "product": "MIS",
                "order_type": "LIMIT", "price": 1500}
        with self.assertRaisesRegex(ValueError, "explicitly confirm"):
            validate_order(LONG, base, 1500, 0.05)
        with self.assertRaisesRegex(ValueError, "must remain BUY"):
            validate_order(LONG, {**base, "confirmed": True, "side": "SELL"}, 1500, 0.05)

    def test_wait_market_bad_tick_and_far_price_are_rejected(self):
        base = {"confirmed": True, "side": "BUY", "quantity": 1, "product": "MIS",
                "order_type": "LIMIT", "price": 1500}
        with self.assertRaisesRegex(ValueError, "LONG or SHORT"):
            validate_order({"symbol": "INFY", "bias": "WAIT"}, base, 1500, 0.05)
        with self.assertRaisesRegex(ValueError, "Only LIMIT"):
            validate_order(LONG, {**base, "order_type": "MARKET"}, 1500, 0.05)
        with self.assertRaisesRegex(ValueError, "tick size"):
            validate_order(LONG, {**base, "price": 1500.03}, 1500, 0.05)
        with self.assertRaisesRegex(ValueError, "more than 20%"):
            validate_order(LONG, {**base, "price": 1900}, 1500, 0.05)
        with self.assertRaisesRegex(ValueError, "whole number"):
            validate_order(LONG, {**base, "quantity": 1.5}, 1500, 0.05)

    def test_short_cannot_use_cnc(self):
        payload = {"confirmed": True, "side": "SELL", "quantity": 1, "product": "CNC",
                   "order_type": "LIMIT", "price": 1500}
        with self.assertRaisesRegex(ValueError, "must use MIS"):
            validate_order({"symbol": "INFY", "bias": "SHORT"}, payload, 1500, 0.05)


if __name__ == "__main__":
    unittest.main()
