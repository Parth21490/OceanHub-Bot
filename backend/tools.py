"""
OceanHub — Technical Analysis Tools
────────────────────────────────────
Pure-Python TA functions using pandas + ta library.
These are registered as callable tools for the sub-agents.
"""

import os
import asyncio
import pandas as pd
import ccxt.async_support as ccxt_async

# ── RSI / EMA / MACD via pandas-ta (imported lazily) ────────────────────
try:
    import pandas_ta as pta
    _USE_PTA = True
except ImportError:
    _USE_PTA = False

try:
    import ta as ta_lib
    _USE_TA = True
except ImportError:
    _USE_TA = False


MARKET_CACHE = None


def get_tick_decimals(symbol: str) -> int:
    """
    Returns the exact tick decimal precision for an asset symbol.
    TASK 1: Uses exact tick precision dictionary as primary resolver:
        "BTC/USDT": 1,
        "ETH/USDT": 2,
        "SOL/USDT": 2,
        "BNB/USDT": 2,
        "HYPE/USDT": 2,
        "XRP/USDT": 4,
        "ADA/USDT": 4,
        "DOGE/USDT": 5
    """
    tick_decimals = {
        "BTC/USDT": 1,
        "ETH/USDT": 2,
        "SOL/USDT": 2,
        "BNB/USDT": 2,
        "HYPE/USDT": 2,
        "XRP/USDT": 4,
        "ADA/USDT": 4,
        "DOGE/USDT": 5
    }
    if symbol in tick_decimals:
        return tick_decimals[symbol]

    global MARKET_CACHE
    try:
        if MARKET_CACHE and symbol in MARKET_CACHE:
            market = MARKET_CACHE[symbol]
            prec = market.get('precision', {}).get('price')
            if prec is not None:
                if isinstance(prec, float) and prec > 0:
                    import math
                    return int(round(-math.log10(prec)))
                elif isinstance(prec, int):
                    return max(0, prec)
    except Exception:
        pass

    return 2


def round_price_prec(price: float, symbol: str) -> float:
    if price is None:
        return None

    try:
        from server import MARKET_CACHE
        if symbol in MARKET_CACHE:
            prec = MARKET_CACHE[symbol].get('precision', {}).get('price')
            if prec is not None and prec > 0:
                return float(round(price / prec) * prec)
    except Exception:
        pass

    decimals = get_tick_decimals(symbol)
    return float(round(price, decimals))


def calculate_position_exits(
        entry_price: float,
        side: str,
        atr: float,
        asset_symbol: str,
        sl_mult: float = 1.5,
        tp_mult: float = 3.0):
    """
    BUG 1 FIX: Mathematical In-Memory Float Precision Rounding.
    Calculates raw SL/TP floats and mathematically rounds them in memory to the asset's specific tick size.
    Returns actual float objects, NOT formatted strings.
    """
    tick_decimals = get_tick_decimals(asset_symbol)

    # BUG-11 FIX: Remove 0.95 front-running multiplier — RiskEngine.calculate_position already applies it.
    # Having it here AND there caused TP = tp_mult * 0.95 * 0.95 = 90.25% of intended distance.
    if side == "LONG":
        raw_sl = entry_price - (atr * sl_mult)
        raw_tp = entry_price + (atr * tp_mult)
    elif side == "SHORT":
        raw_sl = entry_price + (atr * sl_mult)
        raw_tp = entry_price - (atr * tp_mult)
    else:
        return None, None

    # THE FIX: Mathematically round the float in memory before returning
    rounded_sl = float(round(raw_sl, tick_decimals))
    rounded_tp = float(round(raw_tp, tick_decimals))

    return rounded_sl, rounded_tp


def ccxt_symbol_format(symbol: str, exchange=None) -> str:
    """Normalize a display symbol (e.g. 'BTC/USDT') to CCXT linear format (e.g. 'BTCUSDT')."""
    if exchange is not None:
        try:
            market = exchange.market(symbol)
            return market['id']
        except Exception:
            pass
    normalized = symbol.replace(":", "").replace("/", "")
    if normalized.endswith("USDTUSDT"):
        normalized = normalized[:-4]
    return normalized


def _bybit_exchange():
    """Return a configured Bybit exchange instance.

    Uses Testnet public endpoint when API keys are present.
    Falls back to mainnet public data (no auth needed) for OHLCV.
    """
    key = os.getenv("BYBIT_API_KEY", "")
    has_keys = bool(key) and not key.startswith("your_")
    params = {
        "options": {
            "defaultType": "linear",
            "fetchMarkets": ["linear"],
            "defaultMarginMode": "isolated"},
        "enableRateLimit": True,
        "timeout": 15000,
    }
    if has_keys:
        params["apiKey"] = key
        params["secret"] = os.getenv("BYBIT_API_SECRET", "")
        params["urls"] = {"api": {
            "public": "https://api-testnet.bybit.com",
            "private": "https://api-testnet.bybit.com",
        }}
    else:
        # Use bytick.com domain mirror for public data to bypass CloudFront US IP 403 Forbidden on Railway/AWS
        params["urls"] = {
            "api": {
                "public": "https://api.bytick.com",
                "private": "https://api.bytick.com",
            }
        }
    ex = ccxt_async.bybit(params)

    # Apply global market cache if populated to avoid instruments-info API
    # calls
    if MARKET_CACHE is not None:
        ex.set_markets(MARKET_CACHE)

    # Monkey-patch to enforce 'linear' market symbol formatting and category
    # parameter
    def wrap_symbol_method(method_name):
        orig_method = getattr(ex, method_name)

        async def wrapped(symbol, *args, **kwargs):
            symbol = ccxt_symbol_format(symbol, ex)
            args_list = list(args)
            params_idx = None
            if method_name == 'fetch_ohlcv':
                params_idx = 3
            elif method_name == 'fetch_order_book':
                params_idx = 1
            elif method_name == 'fetch_ticker' or method_name == 'fetch_funding_rate':
                params_idx = 0

            if 'params' in kwargs:
                p = kwargs['params'] or {}
                kwargs['params'] = {**p, 'category': 'linear'}
            elif params_idx is not None and len(args_list) > params_idx:
                p = args_list[params_idx] or {}
                args_list[params_idx] = {**p, 'category': 'linear'}
            else:
                kwargs['params'] = {'category': 'linear'}

            import ccxt
            import logging
            logger = logging.getLogger("oceanhub")
            wait_time = 2.0
            for attempt in range(4):
                try:
                    return await orig_method(symbol, *args_list, **kwargs)
                except Exception as exc:
                    if "403" in str(exc) or "CloudFront" in str(exc):
                        ex.urls["api"]["public"] = "https://api.bytick.com"
                        ex.urls["api"]["private"] = "https://api.bytick.com"
                    if attempt == 3:
                        logger.warning(
                            "[WARNING] Max retries reached for %s on %s. Skipping: %s",
                            method_name,
                            symbol,
                            exc)
                        raise exc
                    logger.warning(
                        "[WARNING] %s failed for %s (attempt %d/4). Retrying in %.1fs... Error: %s",
                        method_name,
                        symbol,
                        attempt + 1,
                        wait_time,
                        exc)
                    await asyncio.sleep(wait_time)
                    wait_time *= 2.0
        setattr(ex, method_name, wrapped)

    for method in [
        'fetch_ohlcv',
        'fetch_ticker',
        'fetch_order_book',
            'fetch_funding_rate']:
        try:
            wrap_symbol_method(method)
        except AttributeError:
            pass

    return ex


async def fetch_ohlcv(
    symbol: str = "BTC/USDT",
    timeframe: str = "1h",
    limit: int = 200,
    params: dict = None,
) -> dict:
    """
    Fetch OHLCV candle data from Bybit Testnet via ccxt.

    Args:
        symbol:    Trading pair, e.g. 'BTC/USDT'
        timeframe: Candle interval, e.g. '1h', '15m', '4h'
        limit:     Number of candles to fetch
        params:    Optional query parameters

    Returns:
        dict with keys: symbol, timeframe, candles (list of OHLCV dicts),
        latest_close, latest_volume
    """
    exchange = _bybit_exchange()
    try:
        # Explicitly specify linear/perpetual market to avoid option market
        # errors
        if params is None:
            params = {'category': 'linear'}
        else:
            params = {**params, 'category': 'linear'}
        raw = await exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit, params=params)
        await exchange.close()
    except Exception as exc:
        await exchange.close()
        raise RuntimeError(f"ccxt fetch_ohlcv failed: {exc}") from exc

    df = pd.DataFrame(
        raw,
        columns=[
            "timestamp",
            "open",
            "high",
            "low",
            "close",
            "volume"])
    df["timestamp"] = pd.to_datetime(
        df["timestamp"], unit="ms", utc=True).map(
        lambda x: x.isoformat())

    candles = df.tail(10).to_dict(
        orient="records")  # return last 10 for context
    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "candle_count": len(df),
        "latest_close": float(df["close"].iloc[-1]),
        "latest_volume": float(df["volume"].iloc[-1]),
        "candles_sample": candles,
        "_df_json": df.to_json(orient="records"),  # internal, full data
    }


def calculate_rsi(ohlcv_result: dict, period: int = 14) -> dict:
    """
    Calculate RSI for the fetched OHLCV data.

    Args:
        ohlcv_result: Output from fetch_ohlcv()
        period:       RSI period (default 14)

    Returns:
        dict with rsi_current, rsi_signal, interpretation
    """
    df = pd.read_json(ohlcv_result["_df_json"])
    close = df["close"]

    if _USE_PTA:
        rsi_series = pta.rsi(close, length=period)
    elif _USE_TA:
        rsi_series = ta_lib.momentum.RSIIndicator(close, window=period).rsi()
    else:
        # Manual RSI
        delta = close.diff()
        gain = delta.clip(lower=0).rolling(period).mean()
        loss = (-delta.clip(upper=0)).rolling(period).mean()
        rs = gain / loss.replace(0, 1e-9)
        rsi_series = 100 - (100 / (1 + rs))

    rsi_current = float(rsi_series.iloc[-1]) if not rsi_series.empty else 50.0

    if rsi_current >= 70:
        signal, interpretation = "OVERBOUGHT", "Strong sell pressure likely; consider SHORT or HOLD."
    elif rsi_current <= 30:
        signal, interpretation = "OVERSOLD", "Strong buy pressure likely; consider LONG or HOLD."
    elif rsi_current >= 60:
        signal, interpretation = "BULLISH", "Moderate bullish momentum."
    elif rsi_current <= 40:
        signal, interpretation = "BEARISH", "Moderate bearish momentum."
    else:
        signal, interpretation = "NEUTRAL", "No strong RSI signal."

    return {
        "indicator": "RSI",
        "period": period,
        "rsi_current": round(rsi_current, 2),
        "rsi_signal": signal,
        "interpretation": interpretation,
    }


def calculate_macd(
        ohlcv_result: dict,
        fast: int = 12,
        slow: int = 26,
        signal: int = 9) -> dict:
    """
    Calculate MACD for the fetched OHLCV data.

    Args:
        ohlcv_result: Output from fetch_ohlcv()
        fast:         Fast EMA period (default 12)
        slow:         Slow EMA period (default 26)
        signal:       Signal line period (default 9)

    Returns:
        dict with macd_line, signal_line, histogram, crossover, interpretation
    """
    df = pd.read_json(ohlcv_result["_df_json"])
    close = df["close"]

    if _USE_PTA:
        macd_df = pta.macd(close, fast=fast, slow=slow, signal=signal)
        macd_line = float(macd_df.iloc[-1, 0])
        histogram = float(macd_df.iloc[-1, 1])
        signal_line = float(macd_df.iloc[-1, 2])
    elif _USE_TA:
        ind = ta_lib.trend.MACD(
            close,
            window_fast=fast,
            window_slow=slow,
            window_sign=signal)
        macd_line = float(ind.macd().iloc[-1])
        signal_line = float(ind.macd_signal().iloc[-1])
        histogram = float(ind.macd_diff().iloc[-1])
    else:
        ema_fast = close.ewm(span=fast, adjust=False).mean()
        ema_slow = close.ewm(span=slow, adjust=False).mean()
        macd_line_s = ema_fast - ema_slow
        signal_s = macd_line_s.ewm(span=signal, adjust=False).mean()
        macd_line = float(macd_line_s.iloc[-1])
        signal_line = float(signal_s.iloc[-1])
        histogram = float((macd_line_s - signal_s).iloc[-1])

    prev_hist = histogram - \
        (float(close.diff().iloc[-1]) * 0.001)  # approx prev
    if histogram > 0 and histogram > prev_hist:
        crossover, interpretation = "BULLISH_MOMENTUM", "MACD above signal and rising — bullish."
    elif histogram > 0:
        crossover, interpretation = "BULLISH_WEAKENING", "MACD positive but momentum slowing."
    elif histogram < 0 and histogram < prev_hist:
        crossover, interpretation = "BEARISH_MOMENTUM", "MACD below signal and falling — bearish."
    else:
        crossover, interpretation = "BEARISH_WEAKENING", "MACD negative but momentum slowing."

    return {
        "indicator": "MACD",
        "fast": fast,
        "slow": slow,
        "signal_period": signal,
        "macd_line": round(macd_line, 4),
        "signal_line": round(signal_line, 4),
        "histogram": round(histogram, 4),
        "crossover": crossover,
        "interpretation": interpretation,
    }


def calculate_ema(ohlcv_result: dict, periods: list = None) -> dict:
    """
    Calculate multiple EMAs for the fetched OHLCV data.

    Args:
        ohlcv_result: Output from fetch_ohlcv()
        periods:      List of EMA periods (default [9, 21, 50, 200])

    Returns:
        dict with ema values, trend_bias, support/resistance level
    """
    if periods is None:
        periods = [9, 21, 50, 200]

    df = pd.read_json(ohlcv_result["_df_json"])
    close = df["close"]
    price = float(close.iloc[-1])

    emas = {}
    for p in periods:
        if len(close) >= p:
            val = float(close.ewm(span=p, adjust=False).mean().iloc[-1])
            emas[f"ema_{p}"] = round(val, 2)

    # Trend bias: price vs key EMAs
    above = [p for p in periods if price > emas.get(f"ema_{p}", float("inf"))]
    below = [p for p in periods if price < emas.get(f"ema_{p}", 0)]

    if len(above) >= 3:
        trend_bias = "STRONGLY_BULLISH"
        trend_desc = f"Price above {
            len(above)}/{
            len(periods)} EMAs — strong uptrend."
    elif len(above) >= 2:
        trend_bias = "BULLISH"
        trend_desc = f"Price above {
            len(above)}/{
            len(periods)} EMAs — moderate uptrend."
    elif len(below) >= 3:
        trend_bias = "STRONGLY_BEARISH"
        trend_desc = f"Price below {
            len(below)}/{
            len(periods)} EMAs — strong downtrend."
    elif len(below) >= 2:
        trend_bias = "BEARISH"
        trend_desc = f"Price below {
            len(below)}/{
            len(periods)} EMAs — moderate downtrend."
    else:
        trend_bias = "NEUTRAL"
        trend_desc = "Price mixed across EMAs — no clear trend."

    # Nearest EMA support / resistance
    all_ema_vals = sorted(emas.values())
    support = max((v for v in all_ema_vals if v <= price), default=None)
    resistance = min((v for v in all_ema_vals if v >= price), default=None)

    return {
        "indicator": "EMA",
        "current_price": round(price, 2),
        **emas,
        "trend_bias": trend_bias,
        "description": trend_desc,
        "support_ema": round(support, 2) if support else None,
        "resistance_ema": round(resistance, 2) if resistance else None,
    }


async def sync_exchange_positions(symbols: list[str]) -> dict:
    """
    State Recovery Protocol: On-Startup Exchange Sync.
    Queries the exchange API (Bybit linear perpetuals) for live open positions.
    Populates local state object with Entry Price, Quantity, SL, and TP (rounded to tick size).
    Resumes normal monitoring without double-opening trades.
    """
    positions_map = {}
    from tools import _bybit_exchange, round_price_prec
    from datetime import datetime, timezone
    exchange = _bybit_exchange()
    try:
        raw_positions = await exchange.fetch_positions(symbols, params={'category': 'linear'})
        for pos in raw_positions:
            contracts = float(
                pos.get(
                    'contracts',
                    0.0) or pos.get(
                    'size',
                    0.0) or 0.0)
            if contracts > 0:
                ccxt_sym = str(pos.get('symbol', ''))
                matched_sym = next(
                    (s for s in symbols if s == ccxt_sym or ccxt_sym.startswith(
                        s.split('/')[0])), None)
                if not matched_sym:
                    matched_sym = ccxt_sym.split(
                        ':')[0] if ':' in ccxt_sym else ccxt_sym

                side = str(pos.get('side', '')).upper()
                direction = "LONG" if side in ["LONG", "BUY"] else "SHORT"
                entry_price = float(
                    pos.get(
                        'entryPrice',
                        0.0) or pos.get(
                        'markPrice',
                        0.0) or 0.0)
                entry_rounded = round_price_prec(entry_price, matched_sym)

                sl_val = pos.get('stopLoss') or pos.get(
                    'info', {}).get('stopLoss')
                tp_val = pos.get('takeProfit') or pos.get(
                    'info', {}).get('takeProfit')

                sl_rounded = round_price_prec(
                    float(sl_val), matched_sym) if sl_val and float(sl_val) > 0 else None
                tp_rounded = round_price_prec(
                    float(tp_val), matched_sym) if tp_val and float(tp_val) > 0 else None

                leverage = float(pos.get('leverage', 5.0) or 5.0)
                notional = float(
                    pos.get(
                        'notional',
                        0.0) or (
                        contracts *
                        entry_price))
                margin = round(
                    notional / leverage,
                    2) if leverage > 0 else round(
                    notional,
                    2)

                positions_map[matched_sym] = {
                    "direction": direction,
                    "entry_price": entry_rounded,
                    "leverage": leverage,
                    "stop_loss": sl_rounded,
                    "take_profit": tp_rounded,
                    "margin": margin,
                    "size": contracts,
                    "timestamp": datetime.now(timezone.utc).isoformat()
                }
    except Exception:
        pass
    finally:
        await exchange.close()

    return positions_map
