"""Local API combining research signals, live quotes, and reviewed orders.

Run it on localhost only. The one-time Kite request token comes from the React
UI, is exchanged server-side using the local credentials file, and lives only
in this process's memory. Order submission is limited to current directional
recommendations and requires an explicitly confirmed review ticket.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
import time
from dataclasses import asdict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

import daily_plan
import orb_backtest as ob
import symbol_screener as sc
from budget_plan import DEFAULT_BUDGET, build_intraday_budget_plan
from kite_data import IST, KiteConfigurationError, KiteMarketData, _read_credentials
from market_context import build_market_context
from order_service import place_recommendation_order
from strategy_analysis import execution_plan, momentum_metrics, unified_recommendations


ROOT = Path(__file__).resolve().parent
ALGO_ROOT = Path(os.getenv("ALGO_TRADING_ROOT", r"C:\Users\Kiran\Documents\ChatGPT\algotrading"))
CREDENTIALS_FILE = Path(os.getenv("KITE_CREDENTIALS_FILE", ALGO_ROOT / "creds.txt"))
if str(ALGO_ROOT) not in sys.path:
    sys.path.insert(0, str(ALGO_ROOT))


class AppState:
    market_data: KiteMarketData | None = None
    profile: dict | None = None
    latest_scan = {"signals": [], "errors": [], "scan_date": None}
    latest_momentum = {"rows": [], "scan_date": None}
    latest_execution = {"rows": [], "scan_date": None}
    latest_orb_plan = None
    latest_market_context = {
        "generated_at": None, "markets": [], "volatility": [], "news": [],
        "errors": [], "regime": "Unavailable", "score": 0,
        "summary": "Refresh global context to load the overnight backdrop.",
    }
    latest_recommendations = {
        "date": None, "market_regime": "Unavailable", "market_score": 0,
        "prices_asof": None, "recommendations": [],
    }
    latest_budget_plan = build_intraday_budget_plan([], DEFAULT_BUDGET)
    latest_orders = []


STATE = AppState()


def _scanner_module():
    if not ALGO_ROOT.is_dir():
        raise RuntimeError(f"ALGO_TRADING_ROOT was not found: {ALGO_ROOT}")
    try:
        import nifty100_sma_scanner as scanner
    except ImportError as exc:
        raise RuntimeError("Could not load the companion NIFTY 100 scanner.") from exc
    return scanner


def _api_key():
    credentials = _read_credentials(CREDENTIALS_FILE)
    api_key = credentials.get("api_key") or credentials.get("kite_api_key")
    if not api_key:
        raise KiteConfigurationError(f"{CREDENTIALS_FILE} has no api_key entry.")
    return api_key


def login_url():
    return f"https://kite.zerodha.com/connect/login?v=3&api_key={quote(_api_key())}"


def connect(request_token):
    """Exchange the browser-supplied one-time token without persisting it."""
    credentials = _read_credentials(CREDENTIALS_FILE)
    api_key = _api_key()
    api_secret = credentials.get("api_secret") or credentials.get("kite_api_secret")
    if not api_secret:
        raise KiteConfigurationError(f"{CREDENTIALS_FILE} has no api_secret entry.")
    try:
        from kiteconnect import KiteConnect
    except ImportError as exc:
        raise KiteConfigurationError("Install the Python dependencies before starting the API.") from exc
    client = KiteConnect(api_key=api_key)
    session = client.generate_session(request_token, api_secret=api_secret)
    client.set_access_token(session["access_token"])
    profile = client.profile()
    STATE.market_data = KiteMarketData(client=client)
    STATE.profile = {key: profile.get(key) for key in ("user_id", "user_name", "user_shortname")}
    return STATE.profile


def require_session():
    if not STATE.market_data:
        raise ValueError("Connect a fresh Kite request token first.")
    return STATE.market_data


def scan_nifty100():
    """Run the companion project's SMA scanner with the shared in-memory session."""
    scanner, market = _scanner_module(), require_session()
    constituents = scanner.nifty100_constituents()
    instruments = {
        row["tradingsymbol"]: row["instrument_token"]
        for row in market.client.instruments("NSE")
        if row.get("tradingsymbol") in constituents and row.get("instrument_type") == "EQ"
    }
    start = dt.date.today() - dt.timedelta(days=400)
    signals, momentum_rows, errors = [], [], []
    for position, (symbol, name) in enumerate(constituents.items(), start=1):
        token = instruments.get(symbol)
        if not token:
            errors.append(symbol)
            continue
        try:
            candles = market.client.historical_data(token, start, dt.date.today(), "day")
            complete = scanner.latest_complete_candle(candles)
            signal = scanner.calculate_signal(symbol, name, complete)
            if signal:
                item = asdict(signal)
                item["crossover_date"] = item["crossover_date"].isoformat() if item["crossover_date"] else None
                signals.append(item)
            momentum = momentum_metrics(symbol, name, complete)
            if momentum:
                momentum_rows.append(momentum)
        except Exception:
            errors.append(symbol)
        if position < len(constituents):
            time.sleep(scanner.REQUEST_PAUSE_SECONDS)
    scan_date = dt.datetime.now(ZoneInfo(IST)).date().isoformat()
    STATE.latest_scan = {
        "signals": signals, "errors": sorted(set(errors)),
        "scan_date": scan_date,
    }
    momentum_rows.sort(key=lambda row: row["score"], reverse=True)
    STATE.latest_momentum = {"rows": momentum_rows, "scan_date": scan_date}
    STATE.latest_execution = {
        "rows": [execution_plan(row, budget=ob.slot_budget()) for row in momentum_rows],
        "scan_date": scan_date,
    }
    return STATE.latest_scan


def orb_plan(premarket=False):
    day, symbols, rows, note = daily_plan.build(premarket=premarket, market_data=require_session())
    plan = {"date": str(day), "symbols": symbols, "rows": rows, "note": note, "premarket": premarket}
    STATE.latest_orb_plan = plan
    return plan


def refresh_all():
    """Refresh each daily strategy, then calculate one cross-strategy view."""
    scan_nifty100()
    plan = orb_plan(premarket=False)
    STATE.latest_market_context = build_market_context(require_session())
    STATE.latest_recommendations = unified_recommendations(
        STATE.latest_scan["signals"], STATE.latest_momentum["rows"], plan,
        market_context=STATE.latest_market_context)
    refresh_recommendation_prices()
    return dashboard_state()


def refresh_market_context():
    """Refresh global context and re-score any existing daily consensus."""
    STATE.latest_market_context = build_market_context(require_session())
    if STATE.latest_orb_plan:
        STATE.latest_recommendations = unified_recommendations(
            STATE.latest_scan["signals"], STATE.latest_momentum["rows"],
            STATE.latest_orb_plan, market_context=STATE.latest_market_context)
        refresh_recommendation_prices()
    return dashboard_state()


def refresh_recommendation_prices():
    """Attach one batched Kite LTP snapshot and current tick sizes to rows."""
    market = require_session()
    rows = STATE.latest_recommendations.get("recommendations") or []
    symbols = [row["symbol"] for row in rows]
    prices = market.latest_prices(symbols)
    asof = dt.datetime.now(ZoneInfo(IST)).isoformat(timespec="seconds")
    for row in rows:
        symbol = str(row["symbol"]).upper().removesuffix(".NS")
        row["last_price"] = prices.get(symbol)
        try:
            row["tick_size"] = float(market.instrument_info(symbol).get("tick_size") or 0.05)
        except Exception:
            row["tick_size"] = 0.05
    STATE.latest_recommendations["prices_asof"] = asof
    refresh_budget_plan(STATE.latest_budget_plan.get("budget", DEFAULT_BUDGET))
    return STATE.latest_recommendations


def refresh_budget_plan(budget=DEFAULT_BUDGET):
    """Recalculate a cash-only plan from the current ranked recommendations."""
    STATE.latest_budget_plan = build_intraday_budget_plan(
        STATE.latest_recommendations.get("recommendations") or [], budget,
        max_positions=ob.MAX_POSITIONS)
    STATE.latest_budget_plan["date"] = STATE.latest_recommendations.get("date")
    return STATE.latest_budget_plan


def submit_recommendation_order(payload):
    """Place a reviewed order for one row using a fresh server-side LTP."""
    market = require_session()
    today = dt.datetime.now(ZoneInfo(IST)).date().isoformat()
    if STATE.latest_recommendations.get("date") != today:
        raise ValueError("Refresh all strategies today before placing a live order.")
    symbol = str(payload.get("symbol") or "").upper().removesuffix(".NS")
    recommendation = next(
        (row for row in STATE.latest_recommendations.get("recommendations") or []
         if str(row.get("symbol") or "").upper().removesuffix(".NS") == symbol),
        None,
    )
    if not recommendation:
        raise ValueError("Refresh all strategies before placing an order for this symbol.")
    prices = market.latest_prices([symbol])
    if symbol not in prices:
        raise ValueError(f"Kite did not return a latest price for {symbol}.")
    info = market.instrument_info(symbol)
    tick_size = float(info.get("tick_size") or 0.05)
    result = place_recommendation_order(
        market.client, recommendation, payload, prices[symbol], tick_size)
    result["last_price"] = prices[symbol]
    result["submitted_at"] = dt.datetime.now(ZoneInfo(IST)).isoformat(timespec="seconds")
    STATE.latest_orders.append(result)
    STATE.latest_orders[:] = STATE.latest_orders[-20:]
    recommendation["last_price"] = prices[symbol]
    STATE.latest_recommendations["prices_asof"] = result["submitted_at"]
    return result


def dashboard_state():
    return {
        "scan": STATE.latest_scan,
        "momentum": STATE.latest_momentum,
        "execution": STATE.latest_execution,
        "orb_plan": STATE.latest_orb_plan,
        "market_context": STATE.latest_market_context,
        "budget_plan": STATE.latest_budget_plan,
        "recommendations": STATE.latest_recommendations,
    }


def orb_backtest():
    """Run the ORB engine using point-in-time daily rankings and Kite candles."""
    market = require_session()
    pool = sc.load_universe()
    daily = sc.fetch_daily(pool, days=sc.LOOKBACK_DAYS + 120, market_data=market)
    sessions = sorted({day for frame in daily.values() for day in frame.index.date})
    cutoff = sessions[-1] - dt.timedelta(days=ob._period_days())
    sessions = [day for day in sessions if day > cutoff]
    budget, picks, metrics = ob.slot_budget(), {}, {}
    for day in sessions:
        ranked = sc.screen_asof(daily, day, max_price=budget)
        if ranked.empty:
            continue
        top = ranked.head(ob.MAX_POSITIONS)
        picks[day] = top.symbol.tolist()
        for row in top.itertuples():
            metrics[(day, row.symbol)] = {"atr_pct": row.atr_pct, "turnover_cr": row.turnover_cr}
    needed = sorted({symbol for selected in picks.values() for symbol in selected})
    intraday = ob.load_many(needed, market_data=market)
    trades, unaffordable = ob.backtest_watchlist(intraday, picks, metrics)
    if trades.empty:
        return {"summary": {"trades": 0}, "trades": [], "unaffordable": unaffordable}
    daily_net = trades.groupby("date").pnl.sum().sort_index()
    summary = {
        "trades": int(len(trades)), "sessions": int(trades.date.nunique()),
        "win_rate": round(float((trades.pnl > 0).mean() * 100), 1),
        "gross": round(float(trades.gross.sum()), 1), "cost": round(float(trades.cost.sum()), 1),
        "net": round(float(trades.pnl.sum()), 1),
        "max_drawdown": round(float((daily_net.cumsum() - daily_net.cumsum().cummax()).min()), 1),
    }
    rows = json.loads(trades.to_json(orient="records", date_format="iso"))
    return {"summary": summary, "trades": rows, "unaffordable": [[str(d), s, p] for d, s, p in unaffordable]}


class ApiHandler(BaseHTTPRequestHandler):
    def send_json(self, status, data):
        body = json.dumps(data, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def body(self):
        size = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(size) or b"{}")

    def do_GET(self):
        try:
            if self.path == "/api/config":
                return self.send_json(HTTPStatus.OK, {"login_url": login_url(), "connected": bool(STATE.market_data), "profile": STATE.profile})
            if self.path == "/api/latest":
                return self.send_json(HTTPStatus.OK, STATE.latest_scan)
            if self.path == "/api/dashboard":
                return self.send_json(HTTPStatus.OK, dashboard_state())
            return self.send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
        except Exception as exc:
            return self.send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})

    def do_POST(self):
        try:
            body = self.body()
            if self.path == "/api/connect":
                token = str(body.get("refreshToken", "")).strip()
                if not token:
                    raise ValueError("Enter the one-time Kite request token.")
                return self.send_json(HTTPStatus.OK, {"profile": connect(token)})
            if self.path == "/api/scan":
                return self.send_json(HTTPStatus.OK, scan_nifty100())
            if self.path == "/api/refresh-all":
                return self.send_json(HTTPStatus.OK, refresh_all())
            if self.path == "/api/global-context":
                return self.send_json(HTTPStatus.OK, refresh_market_context())
            if self.path == "/api/recommendation-prices":
                return self.send_json(HTTPStatus.OK, refresh_recommendation_prices())
            if self.path == "/api/intraday-plan":
                return self.send_json(
                    HTTPStatus.OK, refresh_budget_plan(body.get("budget", DEFAULT_BUDGET)))
            if self.path == "/api/orders":
                return self.send_json(HTTPStatus.OK, submit_recommendation_order(body))
            if self.path == "/api/orb/plan":
                return self.send_json(HTTPStatus.OK, orb_plan(bool(body.get("premarket"))))
            if self.path == "/api/orb/backtest":
                return self.send_json(HTTPStatus.OK, orb_backtest())
            return self.send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
        except (ValueError, RuntimeError, KiteConfigurationError) as exc:
            return self.send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except Exception as exc:
            return self.send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"Request failed: {exc}"})

    def log_message(self, format, *args):
        return


if __name__ == "__main__":
    print("Unified trading API: http://127.0.0.1:8788")
    ThreadingHTTPServer(("127.0.0.1", 8788), ApiHandler).serve_forever()
