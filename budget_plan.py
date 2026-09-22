"""Turn ranked recommendations into a cash-capped intraday action plan."""

from __future__ import annotations

import math


DEFAULT_BUDGET = 10000.0
MIN_BUDGET = 500.0
MAX_BUDGET = 10_000_000.0


def _price(row):
    plan = row.get("plan") or {}
    return float(plan.get("trigger") or plan.get("limit") or row.get("last_price") or 0)


def build_intraday_budget_plan(recommendations, budget=DEFAULT_BUDGET, max_positions=2):
    """Allocate cash equally across at most two ranked directional ideas."""
    try:
        budget = float(budget)
    except (TypeError, ValueError) as exc:
        raise ValueError("Intraday budget must be a number.") from exc
    if not math.isfinite(budget) or budget < MIN_BUDGET or budget > MAX_BUDGET:
        raise ValueError("Intraday budget must be between ₹500 and ₹1,00,00,000.")
    if not isinstance(max_positions, int) or max_positions < 1:
        raise ValueError("Maximum positions must be a positive whole number.")

    candidates = [
        row for row in recommendations
        if row.get("bias") in ("LONG", "SHORT") and _price(row) > 0
    ]
    target_count = min(max_positions, len(candidates))
    selected = []
    if target_count:
        equal_slot = budget / target_count
        selected = [row for row in candidates if _price(row) <= equal_slot][:target_count]
        # If equal slots cannot buy any candidate, use the full budget for the
        # highest-ranked affordable idea and flag the concentration explicitly.
        if not selected:
            selected = [row for row in candidates if _price(row) <= budget][:1]

    slot_budget = budget / len(selected) if selected else 0
    rows = []
    for rank, recommendation in enumerate(selected, start=1):
        plan = recommendation.get("plan") or {}
        entry = _price(recommendation)
        quantity = int(slot_budget // entry)
        if quantity < 1:
            continue
        stop = float(plan.get("stop") or 0)
        target = float(plan.get("target") or 0)
        deployed = entry * quantity
        risk_per_share = abs(entry - stop) if stop else 0
        reward_per_share = abs(target - entry) if target else 0
        rows.append({
            "rank": rank,
            "symbol": recommendation["symbol"],
            "name": recommendation.get("name"),
            "side": recommendation["bias"],
            "confidence": recommendation.get("confidence"),
            "last_price": recommendation.get("last_price"),
            "order_type": plan.get("order_type"),
            "entry": round(entry, 2),
            "stop": round(stop, 2) if stop else None,
            "target": round(target, 2) if target else None,
            "quantity": quantity,
            "slot_budget": round(slot_budget, 2),
            "deployed": round(deployed, 2),
            "risk": round(risk_per_share * quantity, 2),
            "reward": round(reward_per_share * quantity, 2),
            "rr": round(reward_per_share / risk_per_share, 2) if risk_per_share else None,
            "action": recommendation.get("action"),
        })

    deployed = round(sum(row["deployed"] for row in rows), 2)
    total_risk = round(sum(row["risk"] for row in rows), 2)
    total_reward = round(sum(row["reward"] for row in rows), 2)
    if not candidates:
        note = "No directional recommendations are available; keep the budget in cash."
    elif not rows:
        note = "No current recommendation is affordable within this cash budget; do not force a trade."
    elif len(rows) == 1 and len(candidates) > 1:
        note = "Only one ranked idea fits the allocation; this is a concentrated plan, so skipping is valid."
    else:
        note = "Capital is split equally across the highest-ranked affordable ideas using whole shares."
    return {
        "budget": round(budget, 2),
        "max_positions": max_positions,
        "slot_budget": round(slot_budget, 2),
        "deployed": deployed,
        "unused_cash": round(budget - deployed, 2),
        "total_risk": total_risk,
        "risk_pct": round(total_risk / budget * 100, 2) if budget else 0,
        "total_reward": total_reward,
        "rows": rows,
        "note": note,
    }
