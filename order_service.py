"""Validation and submission of explicitly confirmed recommendation orders."""

from __future__ import annotations

import math
import re


SYMBOL_PATTERN = re.compile(r"^[A-Z0-9&-]{1,32}$")
ALLOWED_PRODUCTS = {"MIS", "CNC"}
ALLOWED_ORDER_TYPES = {"LIMIT", "SL"}


def _number(value, field):
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a number.") from exc
    if not math.isfinite(number):
        raise ValueError(f"{field} must be finite.")
    return number


def _on_tick(value, tick_size):
    units = value / tick_size
    return abs(units - round(units)) < 1e-6


def validate_order(recommendation, payload, last_price, tick_size):
    """Build broker parameters only after strict server-side validation."""
    if payload.get("confirmed") is not True:
        raise ValueError("Review the ticket and explicitly confirm the live order.")
    symbol = str(recommendation.get("symbol") or "").upper().removesuffix(".NS")
    if not SYMBOL_PATTERN.fullmatch(symbol):
        raise ValueError("The recommendation has an invalid NSE symbol.")
    bias = recommendation.get("bias")
    if bias not in ("LONG", "SHORT"):
        raise ValueError("Orders can only be placed for LONG or SHORT recommendations.")
    expected_side = "BUY" if bias == "LONG" else "SELL"
    if str(payload.get("side") or "").upper() != expected_side:
        raise ValueError(f"Order side must remain {expected_side} for this recommendation.")

    quantity_value = _number(payload.get("quantity"), "Quantity")
    quantity = int(quantity_value)
    if quantity != quantity_value:
        raise ValueError("Quantity must be a whole number.")
    if quantity <= 0 or quantity > 100000:
        raise ValueError("Quantity must be between 1 and 100,000.")

    product = str(payload.get("product") or "").upper()
    if product not in ALLOWED_PRODUCTS:
        raise ValueError("Product must be MIS or CNC.")
    if expected_side == "SELL" and product == "CNC":
        raise ValueError("A SHORT recommendation must use MIS; CNC cannot open an equity short.")

    order_type = str(payload.get("order_type") or "").upper()
    if order_type not in ALLOWED_ORDER_TYPES:
        raise ValueError("Only LIMIT and stop-limit (SL) orders are enabled.")
    price = _number(payload.get("price"), "Limit price")
    last_price = _number(last_price, "Latest market price")
    tick_size = _number(tick_size or 0.05, "Tick size")
    if price <= 0 or tick_size <= 0:
        raise ValueError("Limit price and tick size must be positive.")
    if not _on_tick(price, tick_size):
        raise ValueError(f"Limit price must use the NSE tick size of {tick_size:g}.")
    if abs(price / last_price - 1) > 0.20:
        raise ValueError("Limit price is more than 20% away from the latest Kite price.")

    order = {
        "tradingsymbol": symbol,
        "exchange": "NSE",
        "transaction_type": expected_side,
        "quantity": quantity,
        "product": product,
        "order_type": order_type,
        "price": price,
        "validity": "DAY",
        "tag": "unified_algo",
    }
    if order_type == "SL":
        trigger = _number(payload.get("trigger_price"), "Trigger price")
        if trigger <= 0 or not _on_tick(trigger, tick_size):
            raise ValueError(f"Trigger price must be positive and use the {tick_size:g} tick size.")
        if expected_side == "BUY" and price < trigger:
            raise ValueError("A BUY stop-limit price must be at or above its trigger.")
        if expected_side == "SELL" and price > trigger:
            raise ValueError("A SELL stop-limit price must be at or below its trigger.")
        order["trigger_price"] = trigger
    return order


def place_recommendation_order(client, recommendation, payload, last_price, tick_size):
    """Submit one validated regular order and return the OMS acknowledgement."""
    order = validate_order(recommendation, payload, last_price, tick_size)
    order_id = client.place_order(variety="regular", **order)
    return {
        "order_id": str(order_id), "status": "SUBMITTED", "order": order,
        "message": "Submitted to Kite OMS. Check the Kite order book for exchange status or fills.",
    }
