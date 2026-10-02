"""Read-only NSE candle access through the Kite Connect Python SDK.

This module deliberately exposes market-data calls only.  Nothing here imports
or calls an order endpoint.  Symbols may be written in the project's existing
``RELIANCE.NS`` form; Kite instrument tokens are resolved from the NSE
instrument master at runtime.
"""

import datetime as dt
import os
import time
from pathlib import Path

import pandas as pd


IST = "Asia/Kolkata"
REQUEST_PAUSE_SECONDS = 0.35  # stay below Kite's historical-data request rate

# Kite's gateway drops the odd request ("Request failed (kt-common)", HTTP 429
# or 5xx), and a long backtest makes hundreds of them. Those are retried after
# these pauses; anything else (an expired token, a bad argument) fails at once.
RETRY_DELAYS = (2, 4, 8, 16)
# Matched by class name, so the SDK is not needed to classify an error. Kite's
# NetworkException covers 429 and gateway failures, DataException a garbled
# response; the rest are the transport errors underneath the SDK.
TRANSIENT_ERRORS = {"NetworkException", "DataException", "ConnectionError",
                    "Timeout", "ReadTimeout", "ConnectTimeout", "ChunkedEncodingError"}


def is_transient(exc):
    """True for failures worth retrying: the request may succeed if resent."""
    return any(c.__name__ in TRANSIENT_ERRORS for c in type(exc).__mro__)

# Kite caps the span one historical_data request may cover, by interval. A
# request over the cap is rejected outright, so anything longer is split into
# consecutive windows. Conservative on purpose (the documented limits are
# somewhat higher); the cost of a smaller window is one extra request.
MAX_DAYS_PER_CALL = {
    "minute": 60, "3minute": 90, "5minute": 90, "10minute": 90,
    "15minute": 180, "30minute": 180, "60minute": 365, "day": 2000,
}


def request_windows(start, end, max_days):
    """Split [start, end] into consecutive inclusive windows of <= max_days.

    Windows touch but do not overlap: each starts one step after the last ended
    (a day for dates, a minute for datetimes). Kite treats both bounds as
    inclusive, so overlapping windows would return the boundary candle twice.

    The input type is preserved, and a span that fits in one window comes back
    as the original (start, end) untouched, so short requests reach Kite
    exactly as they did before chunking existed.
    """
    is_date = (isinstance(start, dt.date) and not isinstance(start, dt.datetime)
               and isinstance(end, dt.date) and not isinstance(end, dt.datetime))
    if is_date:
        span, step = dt.timedelta(days=max_days - 1), dt.timedelta(days=1)
    else:
        start, end = pd.Timestamp(start).to_pydatetime(), pd.Timestamp(end).to_pydatetime()
        span, step = dt.timedelta(days=max_days) - dt.timedelta(minutes=1), dt.timedelta(minutes=1)
    out, s = [], start
    while s <= end:
        stop = min(s + span, end)
        out.append((s, stop))
        s = stop + step
    return out


class KiteConfigurationError(RuntimeError):
    """Kite authentication has not been supplied for this run."""


class KiteInstrumentError(RuntimeError):
    """A requested NSE symbol is absent from Kite's current instrument list."""


class KiteDataError(RuntimeError):
    """Kite returned an unexpected market-data response."""


def _read_credentials(path):
    """Read simple ``key=value`` or ``key: value`` credentials without logging them."""
    values = {}
    if not path:
        return values
    source = Path(path)
    if not source.is_file():
        raise KiteConfigurationError(f"KITE_CREDENTIALS_FILE does not exist: {source}")
    for line in source.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        delimiter = "=" if "=" in line else ":" if ":" in line else None
        if delimiter:
            key, value = line.split(delimiter, 1)
            values[key.strip().lower()] = value.strip()
    return values


def _credential(values, *names):
    for name in names:
        value = os.getenv(name.upper()) or values.get(name.lower())
        if value:
            return value
    return None


def kite_client(credentials_file=None):
    """Build an authenticated Kite client from environment variables/a local file.

    ``KITE_ACCESS_TOKEN`` is preferred.  If a fresh ``KITE_REQUEST_TOKEN`` is
    supplied instead, the SDK exchanges it using ``api_secret`` from the same
    credentials source.  Tokens are intentionally never written back to disk.
    """
    values = _read_credentials(credentials_file or os.getenv("KITE_CREDENTIALS_FILE"))
    api_key = _credential(values, "kite_api_key", "api_key")
    access_token = _credential(values, "kite_access_token", "access_token")
    request_token = os.getenv("KITE_REQUEST_TOKEN")
    api_secret = _credential(values, "kite_api_secret", "api_secret")
    if not api_key:
        raise KiteConfigurationError(
            "Set KITE_API_KEY or KITE_CREDENTIALS_FILE with an api_key entry.")

    try:
        from kiteconnect import KiteConnect
    except ImportError as exc:
        raise KiteConfigurationError(
            "Kite Connect SDK is not installed. Run: pip install -r requirements.txt") from exc

    client = KiteConnect(api_key=api_key)
    if not access_token and request_token:
        if not api_secret:
            raise KiteConfigurationError(
                "KITE_REQUEST_TOKEN requires KITE_API_SECRET (or api_secret in the credentials file).")
        access_token = client.generate_session(request_token, api_secret=api_secret)["access_token"]
    if not access_token:
        raise KiteConfigurationError(
            "Set KITE_ACCESS_TOKEN (or access_token in KITE_CREDENTIALS_FILE). "
            "It expires at 06:00 IST; alternatively provide a fresh KITE_REQUEST_TOKEN.")
    client.set_access_token(access_token)
    return client


def kite_symbol(symbol):
    """Convert the project's existing NSE names to Kite trading symbols."""
    symbol = str(symbol).strip().upper()
    aliases = {"^NSEI": "NIFTY 50", "^NSEBANK": "NIFTY BANK"}
    symbol = aliases.get(symbol, symbol)
    if symbol.startswith("NSE:"):
        symbol = symbol[4:]
    return symbol[:-3] if symbol.endswith(".NS") else symbol


def interval_for_kite(interval):
    """Translate the project's interval spelling to Kite's API spelling."""
    intervals = {"1m": "minute", "3m": "3minute", "5m": "5minute",
                 "10m": "10minute", "15m": "15minute", "30m": "30minute",
                 "60m": "60minute", "1d": "day", "day": "day"}
    try:
        return intervals[interval]
    except KeyError as exc:
        raise ValueError(f"Unsupported interval {interval!r}; use one of {sorted(intervals)}") from exc


def _candle_frame(candles):
    """Turn Kite SDK candle dictionaries into this project's standard columns."""
    if not candles:
        return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])
    frame = pd.DataFrame(candles).rename(columns={
        "date": "Datetime", "open": "Open", "high": "High", "low": "Low",
        "close": "Close", "volume": "Volume",
    })
    required = ["Datetime", "Open", "High", "Low", "Close", "Volume"]
    missing = [column for column in required if column not in frame]
    if missing:
        raise KiteDataError(f"Kite candle response is missing {', '.join(missing)}")
    frame = frame[required].copy()
    frame["Datetime"] = pd.to_datetime(frame["Datetime"], utc=True).dt.tz_convert(IST)
    for column in required[1:]:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    return frame.dropna(subset=["Datetime", "Open", "High", "Low", "Close"])


class KiteMarketData:
    """NSE instrument lookup and historical OHLCV retrieval for one run."""

    def __init__(self, client=None, credentials_file=None, request_pause=REQUEST_PAUSE_SECONDS):
        self.client = client or kite_client(credentials_file)
        self.request_pause = request_pause
        self._tokens = None
        self._instrument_details = None
        self._last_request = 0.0
        self._sleep = time.sleep

    def _wait_turn(self):
        remaining = self.request_pause - (time.monotonic() - self._last_request)
        if remaining > 0:
            time.sleep(remaining)
        self._last_request = time.monotonic()

    def _instrument_tokens(self):
        if self._tokens is None:
            self._wait_turn()
            instruments = self.client.instruments("NSE")
            self._instrument_details = {
                str(row["tradingsymbol"]).upper(): row
                for row in instruments
                if row.get("exchange") == "NSE" and row.get("instrument_token")
            }
            self._tokens = {
                str(row["tradingsymbol"]).upper(): int(row["instrument_token"])
                for row in instruments
                if row.get("exchange") == "NSE" and row.get("instrument_token")
            }
        return self._tokens

    def instrument_token(self, symbol):
        trading_symbol = kite_symbol(symbol)
        try:
            return self._instrument_tokens()[trading_symbol]
        except KeyError as exc:
            raise KiteInstrumentError(
                f"{symbol!r} is not a current NSE Kite instrument (looked for {trading_symbol!r}).") from exc

    def instrument_info(self, symbol):
        """Return current NSE instrument metadata, including exchange tick size."""
        trading_symbol = kite_symbol(symbol)
        self._instrument_tokens()
        try:
            return self._instrument_details[trading_symbol]
        except KeyError as exc:
            raise KiteInstrumentError(
                f"{symbol!r} is not a current NSE Kite instrument (looked for {trading_symbol!r}).") from exc

    def latest_prices(self, symbols):
        """Return one batched Kite LTP snapshot keyed by plain trading symbol."""
        trading_symbols = list(dict.fromkeys(kite_symbol(symbol) for symbol in symbols))
        if not trading_symbols:
            return {}
        keys = [f"NSE:{symbol}" for symbol in trading_symbols]
        quotes = self.client.ltp(keys)
        return {
            symbol: float(quotes[f"NSE:{symbol}"]["last_price"])
            for symbol in trading_symbols
            if f"NSE:{symbol}" in quotes and quotes[f"NSE:{symbol}"].get("last_price") is not None
        }

    def order_books(self, symbols, batch=200):
        """Live five-level market depth, keyed by plain trading symbol.

        Each value is {"last_price", "buy": [{price, quantity, orders}, ...],
        "sell": [...]}, best price first. Kite's quote endpoint takes up to 500
        instruments a call; 200 keeps well inside that.
        """
        trading_symbols = list(dict.fromkeys(kite_symbol(symbol) for symbol in symbols))
        out = {}
        for i in range(0, len(trading_symbols), batch):
            chunk = trading_symbols[i:i + batch]
            self._wait_turn()
            try:
                quotes = self.client.quote([f"NSE:{s}" for s in chunk]) or {}
            except Exception as exc:  # SDK has several exception classes; retain its message.
                raise KiteDataError(f"Kite quote failed: {exc}") from exc
            for s in chunk:
                q = quotes.get(f"NSE:{s}")
                if not q:
                    continue
                depth = q.get("depth") or {}
                out[s] = {"last_price": q.get("last_price"),
                          "buy": list(depth.get("buy") or []),
                          "sell": list(depth.get("sell") or [])}
        return out

    def candles(self, symbol, interval, start, end):
        """Return OHLCV candles between naive IST datetimes/dates, inclusive.

        Spans longer than Kite allows for the interval are fetched in
        consecutive windows and stitched, so callers can ask for a year of
        15-minute bars without knowing the per-request cap.
        """
        token = self.instrument_token(symbol)
        kite_interval = interval_for_kite(interval)
        cap = MAX_DAYS_PER_CALL.get(kite_interval, 60)
        candles = []
        for lo, hi in request_windows(start, end, cap):
            candles.extend(self._historical(symbol, token, lo, hi, kite_interval))
        frame = _candle_frame(candles)
        if frame.empty:
            return frame
        # Belt and braces: windows do not overlap, but a candle must never be
        # counted twice even if Kite's boundary handling differs from ours.
        return (frame.drop_duplicates(subset="Datetime", keep="last")
                     .sort_values("Datetime").reset_index(drop=True))

    def _historical(self, symbol, token, lo, hi, kite_interval):
        """One historical_data request, resent after a transient failure."""
        for attempt in range(len(RETRY_DELAYS) + 1):
            self._wait_turn()
            try:
                return self.client.historical_data(
                    token, lo, hi, kite_interval, continuous=False, oi=False) or []
            except Exception as exc:  # SDK has several exception classes; retain its message.
                if attempt < len(RETRY_DELAYS) and is_transient(exc):
                    self._sleep(RETRY_DELAYS[attempt])
                    continue
                tries = f" after {attempt + 1} attempts" if attempt else ""
                raise KiteDataError(
                    f"Kite historical data failed for {symbol}{tries}: {exc}") from exc

    def daily(self, symbol, trading_days):
        end = pd.Timestamp.now(tz=IST).date()
        # Calendar buffer preserves the requested number of sessions across
        # weekends and Indian exchange holidays.
        start = end - dt.timedelta(days=int(trading_days * 1.7) + 14)
        frame = self.candles(symbol, "day", start, end)
        if frame.empty:
            return frame.set_index(pd.DatetimeIndex([], name="Datetime"))
        return frame.set_index("Datetime").sort_index().tail(trading_days)

    def intraday(self, symbol, interval, calendar_days):
        # Kite interprets naive timestamps as exchange-local time. Do not use
        # the host clock directly: GitHub runners, for example, run in UTC.
        end = (pd.Timestamp.now(tz=IST).tz_localize(None).to_pydatetime()
               .replace(second=0, microsecond=0))
        start = end - dt.timedelta(days=calendar_days)
        return self.candles(symbol, interval, start, end)
