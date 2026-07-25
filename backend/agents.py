"""
OceanHub — Sub-Agents (Hybrid Deterministic Architecture)
──────────────────────────────────────────────────────────────────────────────
Four specialist sub-agents that evaluate their rules entirely in local Python
using pandas-ta and ccxt OHLCV data.  NO LLM API calls are made here.

Each agent returns a structured vote dict that is forwarded to the single
Master Gemini Agent call in master_agent.py.

Rules:
  Macro Agent    – 1D  200 EMA: price > EMA → LONG, else SHORT
  Trend Agent    – 4H  Bollinger Bands (20,2): price < lower → LONG,
                        price > upper → SHORT, else HOLD
  Momentum Agent – 15m RSI (14): < 35 → LONG, > 65 → SHORT, else HOLD
  Liquidity Agent – Live bid/ask spread: < 0.1% → GO, else HOLD
"""

import asyncio
import logging
from datetime import datetime, timezone

import pandas as pd

from tools import fetch_ohlcv

log = logging.getLogger("agents")

# ── Indicator helpers (no external TA lib required) ────────────────────────────

def _ema(series: pd.Series, period: int) -> float:
    """Return the latest EMA value for a close-price Series."""
    if len(series) < period:
        return float(series.iloc[-1])
    return float(series.ewm(span=period, adjust=False).mean().iloc[-1])


def _bollinger(series: pd.Series, period: int = 20, std_dev: float = 2.0):
    """Return (middle, upper, lower) Bollinger Band values."""
    rolling_mean = series.rolling(period).mean()
    rolling_std  = series.rolling(period).std()
    middle = float(rolling_mean.iloc[-1])
    upper  = float((rolling_mean + std_dev * rolling_std).iloc[-1])
    lower  = float((rolling_mean - std_dev * rolling_std).iloc[-1])
    return middle, upper, lower


def _rsi(series: pd.Series, period: int = 14) -> float:
    """Return the latest RSI value."""
    if len(series) < period + 1:
        return 50.0
    delta = series.diff()
    gain  = delta.clip(lower=0).rolling(period).mean()
    loss  = (-delta.clip(upper=0)).rolling(period).mean()
    rs    = gain / loss.replace(0, 1e-9)
    rsi   = 100 - (100 / (1 + rs))
    return float(rsi.iloc[-1])


def _build_series(ohlcv_result: dict) -> pd.Series:
    """Extract close-price Series from a fetch_ohlcv result dict."""
    import io
    df_json = ohlcv_result.get("_df_json")
    if df_json:
        # Use StringIO so pandas reads the JSON array without path interpretation
        df = pd.read_json(io.StringIO(df_json))
    else:
        # Fall back to candles_sample (≤10 candles — less accurate)
        candles = ohlcv_result.get("candles_sample", [])
        df = pd.DataFrame(candles)
    return df["close"].astype(float).reset_index(drop=True)


# ── Individual deterministic agents ───────────────────────────────────────────

async def run_macro_agent(symbol: str = "BTC/USDT") -> dict:
    """
    Macro Agent — 1D 200 EMA rule.
    vote: LONG if price > 200 EMA, else SHORT.
    """
    try:
        data  = await fetch_ohlcv(symbol=symbol, timeframe="1d", limit=220, params={'category': 'linear'})
        close = _build_series(data)
        price = float(close.iloc[-1])
        ema200 = _ema(close, 200)

        vote = "LONG" if price > ema200 else "SHORT"
        confidence = min(abs(price - ema200) / ema200 * 10, 1.0)  # relative distance

        result = {
            "agent":       "Macro",
            "timeframe":   "1D",
            "indicator":   "200 EMA",
            "price":       round(price, 2),
            "ema_200":     round(ema200, 2),
            "vote":        vote,
            "confidence":  round(confidence, 3),
            "note":        f"Price {'above' if vote == 'LONG' else 'below'} 1D 200 EMA ({ema200:.2f})",
        }
        log.debug("[Macro Agent] vote=%s  price=%.2f  EMA200=%.2f", vote, price, ema200)
        return result

    except Exception as exc:
        log.warning("[Macro Agent] Error: %s", exc)
        return {"agent": "Macro", "vote": "HOLD", "confidence": 0.0, "error": str(exc)}


async def run_trend_agent(symbol: str = "BTC/USDT") -> dict:
    """
    Trend Agent — 4H Bollinger Bands (20, 2) rule.
    vote: LONG if price < lower band, SHORT if price > upper band, else HOLD.
    """
    try:
        data  = await fetch_ohlcv(symbol=symbol, timeframe="4h", limit=60, params={'category': 'linear'})
        close = _build_series(data)
        price = float(close.iloc[-1])
        middle, upper, lower = _bollinger(close, period=20, std_dev=2.0)

        band_width = upper - lower
        if price < lower:
            vote = "LONG"
            confidence = min((lower - price) / band_width, 1.0)
            note = f"Price below lower BB ({lower:.2f})"
        elif price > upper:
            vote = "SHORT"
            confidence = min((price - upper) / band_width, 1.0)
            note = f"Price above upper BB ({upper:.2f})"
        else:
            # How far into the band is price? Closer to edges = higher neutral conf
            mid_dist = abs(price - middle) / (band_width / 2)
            vote = "HOLD"
            confidence = round(1.0 - mid_dist, 3)
            note = f"Price inside BB [{lower:.2f} – {upper:.2f}]"

        result = {
            "agent":      "Trend",
            "timeframe":  "4H",
            "indicator":  "Bollinger Bands (20,2)",
            "price":      round(price, 2),
            "bb_upper":   round(upper, 2),
            "bb_middle":  round(middle, 2),
            "bb_lower":   round(lower, 2),
            "vote":       vote,
            "confidence": round(min(confidence, 1.0), 3),
            "note":       note,
        }
        log.debug("[Trend Agent] vote=%s  price=%.2f  BB[%.2f / %.2f / %.2f]",
                  vote, price, lower, middle, upper)
        return result

    except Exception as exc:
        log.warning("[Trend Agent] Error: %s", exc)
        return {"agent": "Trend", "vote": "HOLD", "confidence": 0.0, "error": str(exc)}


async def run_momentum_agent(symbol: str = "BTC/USDT") -> dict:
    """
    Momentum Agent — 15m RSI (14) rule.
    vote: LONG if RSI < 35, SHORT if RSI > 65, else HOLD.
    """
    try:
        data  = await fetch_ohlcv(symbol=symbol, timeframe="15m", limit=60, params={'category': 'linear'})
        close = _build_series(data)
        rsi   = _rsi(close, period=14)

        if rsi < 35:
            vote = "LONG"
            confidence = round((35 - rsi) / 35, 3)
            note = f"RSI oversold ({rsi:.1f} < 35)"
        elif rsi > 65:
            vote = "SHORT"
            confidence = round((rsi - 65) / 35, 3)
            note = f"RSI overbought ({rsi:.1f} > 65)"
        else:
            # Neutral — confidence inversely proportional to distance from thresholds
            dist_to_threshold = min(abs(rsi - 35), abs(rsi - 65))
            confidence = round(1.0 - (dist_to_threshold / 30), 3)
            vote = "HOLD"
            note = f"RSI neutral ({rsi:.1f})"

        result = {
            "agent":      "Momentum",
            "timeframe":  "15m",
            "indicator":  "RSI-14",
            "rsi":        round(rsi, 2),
            "vote":       vote,
            "confidence": round(min(confidence, 1.0), 3),
            "note":       note,
        }
        log.debug("[Momentum Agent] vote=%s  RSI=%.2f", vote, rsi)
        return result

    except Exception as exc:
        log.warning("[Momentum Agent] Error: %s", exc)
        return {"agent": "Momentum", "vote": "HOLD", "confidence": 0.0, "error": str(exc)}


async def run_liquidity_agent(symbol: str = "BTC/USDT", order_book: dict = None) -> dict:
    """
    Liquidity Agent — bid/ask spread check.
    vote: GO if spread < 0.1%, else HOLD.

    Uses the live order book dict broadcast by server.py if provided,
    or falls back to a synthetic spread estimate from recent OHLCV.
    """
    try:
        bid, ask = None, None

        if order_book and "bids" in order_book and "asks" in order_book:
            bids = order_book.get("bids", [])
            asks = order_book.get("asks", [])
            if bids and asks:
                bid = float(bids[0][0])
                ask = float(asks[0][0])

        if bid is None or ask is None:
            # Fallback: estimate from 1m OHLCV high/low of last candle
            data  = await fetch_ohlcv(symbol=symbol, timeframe="1m", limit=2, params={'category': 'linear'})
            close = float(data["latest_close"])
            bid   = close * 0.9995   # synthetic tight estimate
            ask   = close * 1.0005

        mid    = (bid + ask) / 2
        spread = (ask - bid) / mid

        vote = "GO" if spread < 0.001 else "HOLD"
        confidence = round(max(0.0, 1.0 - (spread / 0.002)), 3)

        result = {
            "agent":      "Liquidity",
            "indicator":  "Bid-Ask Spread",
            "bid":        round(bid, 4),
            "ask":        round(ask, 4),
            "spread_pct": round(spread * 100, 4),
            "vote":       vote,
            "confidence": confidence,
            "note":       f"Spread {spread*100:.4f}% ({'tight ✓' if vote == 'GO' else 'wide — caution'})",
        }
        log.debug("[Liquidity Agent] vote=%s  spread=%.4f%%", vote, spread * 100)
        return result

    except Exception as exc:
        log.warning("[Liquidity Agent] Error: %s", exc)
        return {"agent": "Liquidity", "vote": "HOLD", "confidence": 0.0, "error": str(exc)}


# ── Orchestrator: run all 4 agents concurrently ───────────────────────────────

async def run_all_sub_agents(
    symbol: str = "BTC/USDT",
    order_book: dict = None,
    regime: str = "CHOPPY"
) -> list[dict]:
    """
    Run all 4 deterministic sub-agents concurrently (no stagger needed — no API rate limits).
    Returns a list of vote dicts to pass to the Master Agent.
    """
    log.debug("[Sub-Agents] Running 4 deterministic agents concurrently for %s under regime %s...", symbol, regime)

    results = await asyncio.gather(
        run_macro_agent(symbol),
        run_trend_agent(symbol),
        run_momentum_agent(symbol),
        run_liquidity_agent(symbol, order_book=order_book),
        return_exceptions=True,
    )

    # Replace any exception objects with a safe fallback dict
    safe = []
    names = ["Macro", "Trend", "Momentum", "Liquidity"]
    for name, r in zip(names, results):
        if isinstance(r, Exception):
            log.error("[Sub-Agents] %s agent raised: %s", name, r)
            safe.append({"agent": name, "vote": "HOLD", "confidence": 0.0, "error": str(r)})
        else:
            safe.append(r)

    for r in safe:
        log.debug("[Sub-Agents] %-10s  vote=%-5s  conf=%.0f%%",
                  r.get("agent", "?"), r.get("vote", "?"), r.get("confidence", 0) * 100)

    return safe
