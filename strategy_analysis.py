"""Pure analysis helpers for momentum, execution planning, and consensus.

These functions produce research levels only. They do not place orders or call
Kite endpoints, which also makes them straightforward to test offline.
"""

from __future__ import annotations

import math
from datetime import date


def _mean(values):
    return sum(values) / len(values) if values else 0.0


def _rsi(closes, period=14):
    changes = [b - a for a, b in zip(closes[-period - 1:-1], closes[-period:])]
    gains = _mean([max(change, 0.0) for change in changes])
    losses = _mean([max(-change, 0.0) for change in changes])
    if losses == 0:
        return 100.0 if gains else 50.0
    strength = gains / losses
    return 100.0 - 100.0 / (1.0 + strength)


def _atr(candles, period=14):
    tail = candles[-period:]
    true_ranges = []
    for index, candle in enumerate(tail):
        high, low = float(candle["high"]), float(candle["low"])
        previous = float(tail[index - 1]["close"]) if index else float(candle["open"])
        true_ranges.append(max(high - low, abs(high - previous), abs(low - previous)))
    return _mean(true_ranges)


def momentum_metrics(symbol, name, candles):
    """Price/volume momentum using returns, SMA spread, RSI, and ATR."""
    if len(candles) < 31:
        return None
    closes = [float(candle["close"]) for candle in candles]
    volumes = [float(candle.get("volume", 0)) for candle in candles]
    close = closes[-1]
    sma6, sma30 = _mean(closes[-6:]), _mean(closes[-30:])
    return5 = (close / closes[-6] - 1.0) * 100
    return20 = (close / closes[-21] - 1.0) * 100
    volume_base = _mean(volumes[-21:-1])
    volume_ratio = volumes[-1] / volume_base if volume_base else 0.0
    rsi14 = _rsi(closes)
    atr = _atr(candles)
    atr_pct = atr / close * 100 if close else 0.0
    spread = (sma6 / sma30 - 1.0) * 100 if sma30 else 0.0

    # A bounded conviction score. Fifty is neutral; price persistence and
    # moving-average spread matter most, while volume confirms rather than
    # determines the direction.
    score = 50.0
    score += 18.0 * math.tanh(return20 / 8.0)
    score += 14.0 * math.tanh(return5 / 4.0)
    score += 12.0 * math.tanh(spread / 3.0)
    score += max(-6.0, min(6.0, (volume_ratio - 1.0) * 8.0))
    score += max(-5.0, min(5.0, (rsi14 - 50.0) / 5.0))
    score = max(0.0, min(100.0, score))
    if score >= 60 and close > sma30 and return20 > 0:
        direction = "Bullish"
    elif score <= 40 and close < sma30 and return20 < 0:
        direction = "Bearish"
    else:
        direction = "Neutral"
    return {
        "symbol": symbol, "name": name, "direction": direction,
        "score": round(score, 1), "close": round(close, 2),
        "return_5d": round(return5, 2), "return_20d": round(return20, 2),
        "sma6": round(sma6, 2), "sma30": round(sma30, 2),
        "rsi14": round(rsi14, 1), "volume_ratio": round(volume_ratio, 2),
        "atr": round(atr, 2), "atr_pct": round(atr_pct, 2),
    }


def execution_plan(momentum, budget=5000.0):
    """Choose research entry/risk levels from the article's order mechanics."""
    close = float(momentum["close"])
    atr = max(float(momentum.get("atr") or 0), close * 0.005)
    direction = momentum["direction"]
    volume_ratio = float(momentum.get("volume_ratio") or 0)
    score = float(momentum["score"])
    qty = int(budget // close) if close else 0
    if direction == "Bullish":
        strong = score >= 68 and volume_ratio >= 1
        order_type = "BUY STOP-LIMIT" if strong else "BUY LIMIT"
        trigger = close + 0.15 * atr if strong else min(close, float(momentum["sma6"]))
        limit_price = trigger + 0.10 * atr if strong else trigger
        stop, target = trigger - atr, trigger + 2 * atr
    elif direction == "Bearish":
        strong = score <= 32 and volume_ratio >= 1
        order_type = "SELL STOP-LIMIT" if strong else "SELL LIMIT"
        trigger = close - 0.15 * atr if strong else max(close, float(momentum["sma6"]))
        limit_price = trigger - 0.10 * atr if strong else trigger
        stop, target = trigger + atr, trigger - 2 * atr
    else:
        return {
            "symbol": momentum["symbol"], "direction": "Neutral",
            "order_type": "WAIT", "reason": "Momentum is not directional enough.",
            "qty": qty, "iceberg": "Not applicable", "not_held": "Not automated",
        }
    return {
        "symbol": momentum["symbol"], "direction": direction,
        "order_type": order_type, "trigger": round(trigger, 2),
        "limit": round(limit_price, 2), "stop": round(stop, 2),
        "target": round(target, 2), "trailing_distance": round(atr, 2),
        "qty": qty,
        "iceberg": "Not needed at this position size" if qty < 1000 else "Review block execution",
        "not_held": "Broker-discretionary; not automated",
        "reason": "Momentum continuation" if "STOP" in order_type else "Controlled pullback entry",
    }


def unified_recommendations(scan_signals, momentum_rows, orb_plan, limit=15, market_context=None):
    """Combine strategy agreement and a small global-context confidence tilt."""
    scans = {row["symbol"]: row for row in scan_signals}
    momentums = {row["symbol"]: row for row in momentum_rows}
    orb_symbols = set(orb_plan.get("symbols") or [])
    orb_levels = {(row["symbol"], row["side"]): row for row in orb_plan.get("rows") or []}
    market_context = market_context or {}
    global_regime = market_context.get("regime") or "Unavailable"
    global_score = float(market_context.get("score") or 0)
    results = []
    for symbol, momentum in momentums.items():
        scan = scans.get(symbol)
        if not scan:
            continue
        sma_vote = 1 if scan["direction"] == "Bullish" else -1
        momentum_vote = 1 if momentum["direction"] == "Bullish" else -1 if momentum["direction"] == "Bearish" else 0
        aligned = momentum_vote != 0 and sma_vote == momentum_vote
        bias = "LONG" if aligned and sma_vote > 0 else "SHORT" if aligned else "WAIT"
        in_orb = symbol in orb_symbols or f"{symbol}.NS" in orb_symbols
        conviction = abs(float(momentum["score"]) - 50.0)
        global_adjustment = 0
        if bias == "LONG" and global_regime in ("Risk-on", "Risk-off"):
            global_adjustment = 6 if global_regime == "Risk-on" else -6
        elif bias == "SHORT" and global_regime in ("Risk-on", "Risk-off"):
            global_adjustment = 6 if global_regime == "Risk-off" else -6
        rank_score = conviction + (20 if aligned else 0) + (15 if in_orb else 0) + global_adjustment
        confidence = "High" if aligned and in_orb else "Medium" if aligned else "Low"
        confidence_levels = ["Low", "Medium", "High"]
        confidence_index = confidence_levels.index(confidence)
        if global_adjustment >= 5 and aligned:
            confidence_index = min(2, confidence_index + 1)
        elif global_adjustment <= -5:
            confidence_index = max(0, confidence_index - 1)
        confidence = confidence_levels[confidence_index]
        plan = execution_plan(momentum)
        orb_key = (f"{symbol}.NS", bias) if (f"{symbol}.NS", bias) in orb_levels else (symbol, bias)
        orb_row = orb_levels.get(orb_key)
        if bias == "WAIT":
            action = "Wait - SMA and momentum do not agree"
        elif orb_row:
            action = f"{bias} only beyond ORB trigger {orb_row['trigger']:.2f}"
            plan = {**plan, "trigger": orb_row["trigger"], "stop": orb_row["stop"], "target": orb_row["target"], "qty": orb_row["qty"], "order_type": f"{bias} STOP-LIMIT"}
        elif in_orb:
            action = f"{bias} bias; wait for today’s opening range"
        else:
            action = f"{bias} bias via {plan['order_type']}"
        if bias == "WAIT" or global_regime == "Unavailable":
            global_note = "Global context does not alter this row"
        elif global_regime == "Mixed":
            global_note = "Mixed global backdrop; no confidence adjustment"
        else:
            verb = "supports" if global_adjustment > 0 else "tempers"
            global_note = f"{global_regime} backdrop {verb} the {bias} bias ({global_adjustment:+d})"
        results.append({
            "symbol": symbol, "name": momentum.get("name") or scan.get("name"),
            "bias": bias, "confidence": confidence, "score": round(rank_score, 1),
            "sma": scan["direction"], "momentum": momentum["direction"],
            "momentum_score": momentum["score"], "orb_selected": in_orb,
            "global_regime": global_regime, "global_score": global_score,
            "global_adjustment": global_adjustment, "global_note": global_note,
            "action": action, "plan": plan,
        })
    results.sort(key=lambda row: (row["bias"] != "WAIT", row["score"]), reverse=True)
    return {
        "date": str(orb_plan.get("date") or date.today()),
        "market_regime": global_regime, "market_score": global_score,
        "recommendations": results[:limit],
    }
