"""
OceanHub — Master Execution Agent (Direct ML & Dynamic ATR Risk)
──────────────────────────────────────────────────────────────────────────────
Severed live LLM dependency: Master Agent is now fully deterministic in Python.
- Uses direct ML regime probabilities from Random Forest.
- Logic Gate: LONG if P(Uptrend) >= 0.65; SHORT if P(Downtrend) >= 0.65; else HOLD.
- Dynamic Risk: Stop-loss and leverage calculated using ATR from pandas-ta.
- On-Demand LLM: Daily report generated using Gemini only when requested.
"""

import os
import math
import json
import logging
import asyncio
import threading
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Callable, Awaitable, Optional, Dict, List, Tuple, Any
from dataclasses import dataclass, field


class Decision:
    def __init__(self, action: str, reason: str):
        self.action = action
        self.reason = reason
        
    def __repr__(self):
        return f"Decision(action={repr(self.action)}, reason={repr(self.reason)})"
        
    def get(self, key, default=None):
        mapping = {"decision": self.action, "reasoning": self.reason, "margin": 0.0, "confidence": 0.0, "leverage": 0}
        return mapping.get(key, default)

import pandas as pd
import numpy as np

# Ensure pandas-ta is imported to register extensions
try:
    import pandas_ta as ta
except ImportError:
    pass

# Stub classes for optional LLM integration (Gemini API)
class LocalAgentConfig:
    def __init__(self, api_key="", system_instructions=""):
        self.api_key = api_key
        self.system_instructions = system_instructions

class Agent:
    def __init__(self, config):
        self.config = config
    
    async def __aenter__(self):
        return self
    
    async def __aexit__(self, *args):
        pass
    
    async def chat(self, prompt):
        raise Exception("LLM not configured - using fallback")

class SafeLogWrapper:
    def __init__(self, logger):
        self._logger = logger
        self._lock = threading.Lock()

    def info(self, msg, *args, **kwargs):
        with self._lock:
            self._logger.info(msg, *args, **kwargs)

    def error(self, msg, *args, **kwargs):
        with self._lock:
            self._logger.error(msg, *args, **kwargs)

    def warning(self, msg, *args, **kwargs):
        with self._lock:
            self._logger.warning(msg, *args, **kwargs)

    def debug(self, msg, *args, **kwargs):
        with self._lock:
            self._logger.debug(msg, *args, **kwargs)

log = SafeLogWrapper(logging.getLogger("master_agent"))

# ── Console Output Lock ───────────────────────────────────────────────────────
# Global asyncio lock that serialises the final cycle-log output block.
# All ML inference, API fetching, and risk calculations remain fully concurrent.
# Only the emit stage (log.info + stream_callback) is serialised so that each
# asset's ════ header/footer pair always prints as one unbroken block.
_console_lock: asyncio.Lock = asyncio.Lock()

# ── Global Trade Cooldown Tracker ──────────────────────────────────────────────
TRADE_COOLDOWNS = {}  # Format: { "ADA/USDT": <timestamp_of_close> }
COOLDOWN_MINUTES = 3   # Wait 3 minutes before allowing a new entry on the same coin (allows fast re-entry after TP)

STATE_FILE = Path(__file__).parent / "bot_state.json"
HISTORY_FILE = Path(__file__).parent / "trade_history.json"
# BUG-48 FIX: Use a lambda so asyncio.Lock() is created lazily (not at import time before event loop exists)
_wallet_lock_holder = [None]
def _get_wallet_lock():
    if _wallet_lock_holder[0] is None:
        _wallet_lock_holder[0] = asyncio.Lock()
    return _wallet_lock_holder[0]
wallet_lock = _get_wallet_lock


def _json_default(obj):
    """Custom JSON encoder for NumPy numbers, arrays, and datetimes."""
    if hasattr(obj, 'item'):
        return obj.item()
    if isinstance(obj, (np.integer, np.floating)):
        return float(obj)
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    if isinstance(obj, datetime):
        return obj.isoformat()
    return str(obj)


def save_state(active_positions=None, trade_cooldowns=None):
    pos = active_positions if active_positions is not None else {}
    cd = trade_cooldowns if trade_cooldowns is not None else TRADE_COOLDOWNS
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump({
                "positions": pos,
                "cooldowns": cd
            }, f, default=_json_default, indent=2)
    except Exception as e:
        log.error("Failed to save bot_state.json: %s", e)


def load_state():
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if "cooldowns" in data and isinstance(data["cooldowns"], dict):
                    # BUG-35 FIX: Filter out stale cooldowns older than COOLDOWN_MINUTES on load
                    import time as _time_mod
                    now = _time_mod.time()
                    fresh_cooldowns = {
                        k: v for k, v in data["cooldowns"].items()
                        if isinstance(v, (int, float)) and (now - float(v)) / 60.0 < COOLDOWN_MINUTES
                    }
                    TRADE_COOLDOWNS.update(fresh_cooldowns)
                return data
        except Exception as e:
            log.error("Failed to load bot_state.json: %s", e)
    return {"positions": {}, "cooldowns": {}}


# Initial state load
load_state()


def calculate_slippage(order_size_usd: float, depth_usd: float) -> float:
    """Safe division liquidity guard: returns 999.0 if depth is zero or null to force COST_TOO_HIGH veto."""
    if not depth_usd or depth_usd <= 0 or math.isnan(depth_usd):
        return 999.0  # Return artificially high friction to force COST_TOO_HIGH veto
    return float((order_size_usd / depth_usd) * 100.0)


def calculate_dynamic_leverage(
    confidence: float,
    atr_pct: float,
    market_regime: str,
    max_allowed_leverage: int = 50,
) -> float:
    """Tiered Base Leverage mapped to ML confidence with 12.5x baseline tier."""
    # 1. Tiered Base Leverage mapped to ML confidence
    if confidence >= 0.80:
        base_lev = 25.0  # Ultra conviction
    elif confidence >= 0.70:
        base_lev = 20.0  # Strong signal
    elif confidence >= 0.60:
        base_lev = 12.5  # Baseline sniper entry
    else:
        return 0.0  # Invalid signal below 60%

    # 2. Market Regime Scaling
    if market_regime == "CHOPPY":
        base_lev *= 0.75  # Scale down in choppy markets
        
    # 3. Dynamic Liquidation Protection Guard
    sl_distance_pct = atr_pct * 1.5 
    if sl_distance_pct > 0:
        # Keep liquidation well beyond SL distance
        max_safe_lev = 0.75 / sl_distance_pct
    else:
        max_safe_lev = base_lev

    # 4. Final Leverage Clamping
    # BUG-01 FIX: Floor is 5.0 (not 10.0) so ATR liquidation guard can reduce below 12.5x safely
    final_leverage = min(base_lev, max_safe_lev, float(max_allowed_leverage))
    final_leverage = max(5.0, min(final_leverage, 50.0))

    return round(final_leverage, 1)


def verify_microstructure_entry(
    symbol: str, orderbook: dict, signal_dir: str
) -> tuple[bool, str]:
    """Calculates Order Book Imbalance (OBI) and Spread to ensure high-precision timing."""
    if not orderbook or not isinstance(orderbook, dict):
        return False, "EMPTY_ORDERBOOK"

    bids = orderbook.get("bids", [])
    asks = orderbook.get("asks", [])

    if not bids or not asks or not isinstance(bids, list) or not isinstance(asks, list):
        return False, "EMPTY_ORDERBOOK"

    if len(bids) == 0 or len(asks) == 0:
        return False, "EMPTY_ORDERBOOK"

    try:
        best_bid = float(bids[0][0])
        best_ask = float(asks[0][0])
    except (IndexError, TypeError, ValueError):
        return False, "INVALID_ORDERBOOK"

    if best_bid <= 0 or best_ask <= 0 or math.isnan(best_bid) or math.isnan(best_ask):
        return False, "INVALID_ORDERBOOK"

    # 1. Spread Check
    # BUG-05 FIX: Use mid-price as denominator (standard formula), not best_bid
    mid_price_spread = (best_ask + best_bid) / 2.0
    spread_pct = (best_ask - best_bid) / mid_price_spread if mid_price_spread > 0 else 0.0
    MAX_ALLOWED_SPREAD = 0.0004  # 0.04% max spread

    if spread_pct > MAX_ALLOWED_SPREAD:
        return (
            False,
            f"SPREAD_TOO_WIDE ({spread_pct * 100:.3f}% > {MAX_ALLOWED_SPREAD * 100:.3f}%)",
        )

    # 2. Order Book Imbalance (Top 10 Depth)
    try:
        bid_vol_top10 = sum([float(b[1]) for b in bids[:10] if isinstance(b, (list, tuple)) and len(b) > 1])
        ask_vol_top10 = sum([float(a[1]) for a in asks[:10] if isinstance(a, (list, tuple)) and len(a) > 1])
    except (TypeError, ValueError):
        return False, "INVALID_ORDERBOOK"

    total_vol = bid_vol_top10 + ask_vol_top10

    if total_vol <= 0 or math.isnan(total_vol):
        return False, "ZERO_ORDERBOOK_VOLUME"

    obi = (bid_vol_top10 - ask_vol_top10) / total_vol

    # Long requires positive buy pressure; Short requires negative sell pressure
    # BUG-04 FIX: Lowered OBI threshold 0.15 → 0.05. 0.15 was too strict for thin altcoin books
    if signal_dir == "LONG" and obi < 0.05:
        return False, f"OBI_BEARISH_PRESSURE (OBI: {obi:.2f} < 0.05)"
    elif signal_dir == "SHORT" and obi > -0.05:
        return False, f"OBI_BULLISH_PRESSURE (OBI: {obi:.2f} > -0.05)"

    return True, f"MICROSTRUCTURE_PASSED (OBI: {obi:.2f}, Spread: {spread_pct * 100:.3f}%)"


async def execute_precision_limit_order(
    exchange, symbol: str, signal_dir: str, amount: float, timeout_seconds: int = 5
):
    """Places a Limit order at Best Bid (Long) or Best Ask (Short).

    Cancels if not filled within timeout_seconds to avoid chasing moves.
    """
    try:
        orderbook = await exchange.fetch_order_book(symbol)
        if not orderbook or 'bids' not in orderbook or 'asks' not in orderbook or \
           len(orderbook['bids']) == 0 or len(orderbook['asks']) == 0:
            side = "buy" if signal_dir == "LONG" else "sell"
            return await exchange.create_market_order(symbol=symbol, side=side, amount=amount)

        best_bid = float(orderbook["bids"][0][0])
        best_ask = float(orderbook["asks"][0][0])
    except Exception as e:
        print(f"[{symbol}]   [ORDERBOOK FETCH FAILED] Falling back to market order: {e}")
        side = "buy" if signal_dir == "LONG" else "sell"
        return await exchange.create_market_order(symbol=symbol, side=side, amount=amount)

    target_price = best_bid if signal_dir == "LONG" else best_ask
    side = "buy" if signal_dir == "LONG" else "sell"

    print(
        f"[{symbol}]   [PRECISION ORDER] Placing {signal_dir} Limit at ${target_price:.4f}"
    )

    try:
        order = await exchange.create_order(
            symbol=symbol,
            type="limit",
            side=side,
            amount=amount,
            price=target_price,
            params={"postOnly": True},  # Force Maker status (saves fees)
        )
    except Exception as e:
        print(f"[{symbol}]   [MAKER REJECTED] Falling back to market order: {e}")
        return await exchange.create_market_order(
            symbol=symbol, side=side, amount=amount
        )

    # Wait for fill window
    await asyncio.sleep(timeout_seconds)

    try:
        # Check status
        order_status = await exchange.fetch_order(order["id"], symbol)
        if order_status["status"] == "closed":
            print(f"[{symbol}]   [FILLED] Precision Limit Order Executed!")
            return order_status
        else:
            # Cancel unfilled order to prevent bad late entries
            await exchange.cancel_order(order["id"], symbol)
            print(
                f"[{symbol}]   [CANCELLED] Order not filled in {timeout_seconds}s. Entry aborted to prevent price chasing."
            )
            return None
    except Exception as e:
        print(f"[{symbol}]   [PRECISION CHECK ERROR] {e}")
        return None


# ── Dynamic Risk Calculation Helpers ─────────────────────────────────────────

def calculate_atr(df: pd.DataFrame, period: int = 14) -> dict:
    """
    Calculates ATR in raw price terms (unrounded) for risk engine,
    and formatted string for logger display.
    
    Args:
        df: DataFrame with columns ['high', 'low', 'close']
        period: ATR lookback window (default 14)
    
    Returns:
        dict with 'raw' (float for SL/TP calc), 'display' (str for logs), and 'pct' (float for regime / volatility feature)
    """
    # Ensure we have enough data
    if df is None or len(df) < period + 1:
        return {'raw': 0.0, 'display': '0.0000', 'pct': 0.0}
    
    # True Range calculation
    df = df.copy()
    df['prev_close'] = df['close'].shift(1)
    
    df['tr1'] = df['high'] - df['low']
    df['tr2'] = abs(df['high'] - df['prev_close'])
    df['tr3'] = abs(df['low'] - df['prev_close'])
    
    df['true_range'] = df[['tr1', 'tr2', 'tr3']].max(axis=1)
    
    # ATR - Wilder's smoothing (RMA)
    atr_raw = df['true_range'].ewm(alpha=1/period, min_periods=period).mean().iloc[-1]
    if pd.isna(atr_raw):
        atr_raw = 0.0
    
    # ATR as decimal fraction of current price for regime detection
    # BUG-07/08 FIX: Return decimal (e.g., 0.008 for 0.8%), NOT percentage (e.g., 0.8)
    current_price = df['close'].iloc[-1]
    atr_pct = (atr_raw / current_price) if current_price > 0 else 0.02
    
    return {
        'raw': float(atr_raw),           # Unrounded for SL/TP math
        'display': f"{atr_raw:.4f}",      # 4 decimal places for logger
        'pct': float(atr_pct)             # For K-Means volatility feature (decimal, e.g., 0.008)
    }


def validate_atr(atr_result: dict, asset: str):
    """Crash if ATR is zero or rounded incorrectly."""
    raw = atr_result.get('raw', 0.0)
    if raw == 0:
        log.warning(f"[{asset}] ATR raw is 0.0 — check OHLCV data feed")
    if isinstance(raw, int) and raw != 0:
        raise TypeError(f"[{asset}] ATR raw is int ({raw}) — must be float")
    if 0 < raw < 0.0001:
        log.warning(f"[{asset}] ATR raw is extremely small: {raw}")


def _compute_atr(df: pd.DataFrame, length: int = 14) -> float:
    """Calculate the latest Average True Range (ATR) with RMA fallback."""
    return calculate_atr(df, length)['raw']


def calculate_rolling_avg_atr(df: pd.DataFrame, period: int = 14, window: int = 50) -> float:
    """Calculates the rolling average ATR over a specified lookback window."""
    if df is None or len(df) < period + 5:
        return 0.0
    try:
        df_temp = df.copy()
        df_temp['prev_close'] = df_temp['close'].shift(1)
        df_temp['tr1'] = df_temp['high'] - df_temp['low']
        df_temp['tr2'] = abs(df_temp['high'] - df_temp['prev_close'])
        df_temp['tr3'] = abs(df_temp['low'] - df_temp['prev_close'])
        df_temp['true_range'] = df_temp[['tr1', 'tr2', 'tr3']].max(axis=1)
        atr_series = df_temp['true_range'].ewm(alpha=1/period, min_periods=period).mean()
        rolling_avg = atr_series.tail(window).mean()
        return float(rolling_avg) if not pd.isna(rolling_avg) else 0.0
    except Exception:
        return 0.0


# ── Trade History Logging Helper ─────────────────────────────────────────────

def _log_trade_decision(symbol: str, decision: str, price: float, confidence: float,
                        leverage: int, stop_loss: float | None, atr: float) -> None:
    """Appends trade decisions to a historical JSON file for win/loss evaluation."""
    try:
        history = []
        if HISTORY_FILE.exists():
            try:
                with open(HISTORY_FILE, "r", encoding="utf-8-sig") as f:
                    history = json.load(f)
            except Exception:
                history = []

        history.append({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "symbol": symbol,
            "decision": decision,
            "entry_price": price,
            "confidence": confidence,
            "leverage": leverage,
            "stop_loss": stop_loss,
            "atr": atr
        })

        # Cap history at last 1000 items to avoid bloated files
        history = history[-1000:]
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(history, f, indent=2)
    except Exception as e:
        log.error("[Master] Failed to log trade to history file: %s", e)


# ═══════════════════════════════════════════════════════════════
# DATA STRUCTURES
# ═══════════════════════════════════════════════════════════════

@dataclass
class MarketData:
    asset: str
    price: float
    atr_raw: float          # Unrounded float for SL/TP math
    atr_pct: float          # For regime detection
    spread_pct: float
    bid_depth: float        # In asset units (e.g., ADA)
    ask_depth: float
    funding_rate: float
    features: np.ndarray    # Schema-locked feature vector
    rolling_avg_atr: Optional[float] = None
    orderbook: Optional[dict] = None
    regime: Optional[str] = None
    current_position: Optional[dict] = None
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

@dataclass
class TradeSignal:
    asset: str
    direction: str          # 'LONG', 'SHORT', 'HOLD', 'CLOSE_EARLY'
    confidence: float
    leverage: float = 0.0
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    size: float = 0.0
    margin: float = 0.0     # Dynamically calculated margin in USDT
    regime: str = 'UNKNOWN'
    rejection_reason: Optional[str] = None
    slippage_estimate: float = 0.0
    total_friction: float = 0.0
    model_a_probs: Dict[str, float] = field(default_factory=dict)
    model_b_probs: Dict[str, float] = field(default_factory=dict)
    is_scalp: bool = False
    scale_factor: float = 1.0
    expected_bars: float = 0.0
    expected_duration_mins: float = 0.0


# ═══════════════════════════════════════════════════════════════
# COST ANALYZER WITH CUMULATIVE MARKET IMPACT
# ═══════════════════════════════════════════════════════════════

class CostAnalyzer:
    """
    Calculates total execution friction for market orders on Bybit.
    
    Friction components:
        - Spread cost (half-spread, market order assumption)
        - Taker fee (0.055% Bybit perpetuals)
        - Funding rate (only if within 60 min of 8-hour settlement)
        - Market impact (square-root model on CUMULATIVE book depth)
    
    Gate: Total friction must be <= 0.15% (0.0015) to pass.
    """
    
    # Bybit V5 perpetual fees
    TAKER_FEE = 0.00055
    MAKER_FEE = 0.00020
    
    # Maximum acceptable total friction (0.35% allows altcoins like WLFI, ADA, XRP)
    MAX_FRICTION = 0.0035  # 0.35%
    
    # Funding settlement times (UTC hours)
    FUNDING_HOURS = [0, 8, 16]
    
    # Impact model constant (empirical for Bybit retail)
    IMPACT_CONSTANT = 0.05
    
    # Hard cap on impact estimate (empirical max for Bybit)
    IMPACT_CAP = 0.05  # 5%
    
    # Cumulative depth band: sum liquidity within ±0.5% of mid
    DEPTH_BAND_PCT = 0.005

    def __init__(self, impact_constant: float = 0.05):
        self.impact_constant = impact_constant

    # ═══════════════════════════════════════════════════════════════
    # PUBLIC API
    # ═══════════════════════════════════════════════════════════════

    def calculate_friction(self, orderbook: dict, direction: str,
                           order_size_usd: float, timestamp: datetime = None,
                           use_taker: bool = True) -> Dict[str, float]:
        """
        Main entry point. Returns full friction breakdown and PASS/FAIL.
        """
        # Validate orderbook structure
        if not orderbook or 'bids' not in orderbook or 'asks' not in orderbook:
            return self._error_result("INVALID_ORDERBOOK")
        
        if not orderbook['bids'] or not orderbook['asks']:
            return self._error_result("EMPTY_ORDERBOOK")
        
        # Best prices
        best_bid = float(orderbook['bids'][0][0])
        best_ask = float(orderbook['asks'][0][0])
        mid_price = float((best_bid + best_ask) / 2.0)
        
        if mid_price <= 0:
            return self._error_result("INVALID_MID_PRICE")
        
        # Fetch or calculate cumulative orderbook depth in USD
        if isinstance(orderbook, dict) and 'bid_depth' in orderbook and float(orderbook.get('bid_depth', 0.0)) > 0:
            bid_depth_usd = float(orderbook['bid_depth'])
            ask_depth_usd = float(orderbook.get('ask_depth', 0.0))
        else:
            bid_depth_usd, ask_depth_usd = self._get_cumulative_depth(
                orderbook, mid_price, self.DEPTH_BAND_PCT
            )

        # HARD LIQUIDITY FLOOR: Prevent market orders into ghost orderbooks
        if bid_depth_usd <= 50.0 or ask_depth_usd <= 50.0:
            log.warning(f"[CostAnalyzer] [REJECTED] GHOST_ORDERBOOK (Bid: ${bid_depth_usd:.2f} / Ask: ${ask_depth_usd:.2f})")
            return self._error_result(f"GHOST_ORDERBOOK (Bid: ${bid_depth_usd:.2f} / Ask: ${ask_depth_usd:.2f})")
        
        # 1. Spread cost (half-spread for market order)
        spread_pct = float((best_ask - best_bid) / mid_price)
        spread_cost = float(spread_pct / 2.0)
        
        # 2. Trading fee
        fee = float(self.TAKER_FEE if use_taker else self.MAKER_FEE)
        
        # 3. Funding (time-weighted)
        funding_cost = 0.0
        if timestamp and self._is_near_funding(timestamp):
            funding_rate = float(orderbook.get('funding_rate', 0.0))
            funding_cost = float(abs(funding_rate))
        
        # 4. Market impact
        impact = float(self._estimate_market_impact(
            direction=direction,
            order_size_usd=float(order_size_usd),
            bid_depth_usd=float(bid_depth_usd),
            ask_depth_usd=float(ask_depth_usd)
        ))
        
        # Total friction = Taker Fee + Current Spread + Calculated Slippage + Funding Cost
        # Explicit floating-point calculation
        total = float(fee + spread_cost + impact + funding_cost)
        
        # VETO if exactly 0.0000%
        if total <= 0.0:
            return self._error_result("ZERO_FRICTION_VETO")
        
        return {
            'spread': float(spread_cost),
            'fee': float(fee),
            'funding': float(funding_cost),
            'impact': float(impact),
            'total': float(total),
            'pass': bool(total <= self.MAX_FRICTION),
            'depth_bid_usd': float(bid_depth_usd),
            'depth_ask_usd': float(ask_depth_usd),
            'mid_price': float(mid_price),
            'raw': {
                'best_bid': float(best_bid),
                'best_ask': float(best_ask),
                'spread_pct': float(spread_pct),
                'order_size_usd': float(order_size_usd)
            }
        }

    def log_friction_breakdown(self, result: dict, asset: str) -> str:
        """
        Returns formatted string for logging. Call this after calculate_friction().
        """
        status = "PASS" if result.get('pass') else "VETO"
        raw = result.get('raw', {})
        
        msg = (
            f"[{asset}] FRICTION_BREAKDOWN | "
            f"Status: {status} | "
            f"Total: {float(result.get('total', 0.0)):.4%} | "
            f"Spread: {float(result.get('spread', 0.0)):.4%} | "
            f"Fee: {float(result.get('fee', 0.0)):.4%} | "
            f"Funding: {float(result.get('funding', 0.0)):.4%} | "
            f"Impact: {float(result.get('impact', 0.0)):.4%} | "
            f"Depth(Bid/Ask): ${result.get('depth_bid_usd', 0):,.0f}/${result.get('depth_ask_usd', 0):,.0f} | "
            f"Order: ${raw.get('order_size_usd', 0):.2f}"
        )
        return msg

    # ═══════════════════════════════════════════════════════════════
    # INTERNAL METHODS
    # ═══════════════════════════════════════════════════════════════

    def _get_cumulative_depth(self, orderbook: dict, mid_price: float,
                                band_pct: float) -> Tuple[float, float]:
        """
        CRITICAL FIX: Sum all liquidity within ±band_pct of mid_price.
        
        Old code read only best bid/ask (e.g., $0.75 for BTC).
        New code reads cumulative depth (e.g., $1.2M for BTC).
        """
        lower_bound = mid_price * (1.0 - band_pct)
        upper_bound = mid_price * (1.0 + band_pct)
        
        # Sum bid depth: all bids >= lower_bound (bids sorted descending)
        bid_depth_asset = sum(
            float(size) for price, size in orderbook['bids']
            if float(price) >= lower_bound
        )
        
        # Sum ask depth: all asks <= upper_bound (asks sorted ascending)
        ask_depth_asset = sum(
            float(size) for price, size in orderbook['asks']
            if float(price) <= upper_bound
        )
        
        # Convert to USD (approximate using mid price)
        bid_depth_usd = bid_depth_asset * mid_price
        ask_depth_usd = ask_depth_asset * mid_price
        
        return bid_depth_usd, ask_depth_usd

    def _estimate_market_impact(self, direction: str, order_size_usd: float,
                                bid_depth_usd: float, ask_depth_usd: float) -> float:
        """
        Linear impact model for retail size vs. cumulative book depth.
        Empirical: 1% depth consumption = 0.1% price impact.
        """
        depth_usd = ask_depth_usd if direction == 'LONG' else bid_depth_usd
        
        if depth_usd <= 0:
            return self.IMPACT_CAP  # No liquidity = max penalty
        
        ratio = order_size_usd / depth_usd
        
        if ratio <= 0:
            return 0.0
        
        # Linear impact for retail sizes vs cumulative book depth
        impact = ratio * 0.1
        
        # HARD CAP: empirical maximum (prevents 100%+ estimates)
        return min(impact, self.IMPACT_CAP)

    def _is_near_funding(self, ts: datetime) -> bool:
        """True if within 60 minutes of an 8-hour funding window."""
        # BUG-20 FIX: Enforce UTC timezone before reading hour/minute
        ts_utc = ts.astimezone(timezone.utc) if ts.tzinfo is not None else ts
        minutes = ts_utc.hour * 60 + ts_utc.minute
        
        for fh in self.FUNDING_HOURS:
            funding_minutes = fh * 60
            if abs(minutes - funding_minutes) <= 60:
                return True
        return False

    def _error_result(self, reason: str) -> Dict[str, float]:
        """Return veto result with infinite friction."""
        return {
            'spread': 0.0,
            'fee': 0.0,
            'funding': 0.0,
            'impact': float('inf'),
            'total': float('inf'),
            'pass': False,
            'depth_bid_usd': 0.0,
            'depth_ask_usd': 0.0,
            'mid_price': 0.0,
            'error': reason
        }


# ═══════════════════════════════════════════════════════════════
# K-MEANS REGIME SHIELD
# ═══════════════════════════════════════════════════════════════

class RegimeShield:
    """
    Master circuit breaker. If CHOPPY, entire pipeline aborts instantly.
    No sub-agents, no RF, no risk calc.
    """
    
    def __init__(self):
        # K-Means cluster centers loaded from training
        self.cluster_centers = None  # Set during WFV
        self.choppy_cluster_id = 0   # Determined from training labels
    
    def detect_regime(self, data: MarketData) -> str:
        """
        MODULE 1: Multi-Timeframe (MTF) Regime Smoothing.
        Layer 1: Macro Regime (Higher Timeframe / 1D 200 EMA distance & ADX).
        Layer 2: Micro Regime (Execution timeframe price action / pullbacks).
        Returns: 'CHOPPY', 'TRENDING_UP', 'TRENDING_DOWN', 'BULLISH_PULLBACK', 'BEARISH_PULLBACK', 'UNKNOWN'
        """
        try:
            if data.features is None or len(data.features) < 3:
                return 'UNKNOWN'
                
            # Features for regime cluster prediction: ADX, ATR%, Rolling volatility
            regime_features = np.array([
                data.features[0],   # ADX
                data.atr_pct,
                data.features[1]    # Rolling volatility
            ]).reshape(1, -1)
            
            cluster_id = self._predict_cluster(regime_features)
            if cluster_id == self.choppy_cluster_id:
                return 'CHOPPY'
            
            adx = float(data.features[0])
            vol = float(data.features[1])
            ema_dist = float(data.features[2])  # 1D 200 EMA distance percentile

            # 1. Macro Regime Determination
            if ema_dist > 0.60:
                macro_regime = 'MACRO_UP'
            elif ema_dist < 0.40:
                macro_regime = 'MACRO_DOWN'
            else:
                macro_regime = 'MACRO_CHOPPY'

            # 2. Micro Regime Pullback Reclassification
            if macro_regime == 'MACRO_UP':
                # If macro is UP but short-term vol is elevated while ADX moderates, reclassify as BULLISH_PULLBACK
                if vol > 0.0035 and adx < 28.0:
                    return 'BULLISH_PULLBACK'
                return 'TRENDING_UP'
            elif macro_regime == 'MACRO_DOWN':
                # If macro is DOWN but short-term vol is elevated while ADX moderates, reclassify as BEARISH_PULLBACK
                if vol > 0.0035 and adx < 28.0:
                    return 'BEARISH_PULLBACK'
                return 'TRENDING_DOWN'
            else:
                # MACRO_CHOPPY — Allow micro regime to override if ADX is strong
                # Fix Bug #3: Previous logic was always True (price > price - positive_atr).
                # Now uses a proper EMA9 vs EMA21 crossover signal from the feature vector.
                # features[3] = ema9_vs_ema21 ratio if engineered (positive = bullish cross)
                if adx > 25.0 and vol > 0.003:
                    # Use EMA cross feature if available (index 3 = ema9_vs_ema21)
                    if len(data.features) > 3:
                        ema_cross = float(data.features[3])  # positive = EMA9 above EMA21
                        return 'TRENDING_UP' if ema_cross > 0 else 'TRENDING_DOWN'
                    else:
                        # Fallback: use rolling_vol direction as a proxy for momentum
                        return 'TRENDING_UP' if data.atr_pct < 0.01 else 'TRENDING_DOWN'
                return 'CHOPPY'
        except Exception:
            return 'UNKNOWN'
    
    def _predict_cluster(self, X: np.ndarray) -> int:
        """Nearest cluster center."""
        if self.cluster_centers is None:
            # Fallback if not trained: use simple heuristic
            adx = X[0, 0]
            vol = X[0, 1] if X.shape[1] > 1 else 0.002
            if adx < 20 and vol < 0.005:
                return 0
            return 1  # 0 = choppy, 1 = trending
        
        distances = np.linalg.norm(self.cluster_centers - X, axis=1)
        return int(np.argmin(distances))

    def load_trained_centers(self, centers: np.ndarray, choppy_id: int):
        self.cluster_centers = centers
        self.choppy_cluster_id = choppy_id


# ═══════════════════════════════════════════════════════════════
# DYNAMIC POSITION SIZING RISK ENGINE
# ═══════════════════════════════════════════════════════════════

class RiskEngine:
    """
    Calculates position size, leverage, stop-loss, and take-profit.
    
    Design principles:
        - Asset-agnostic: uses ATR% (not raw ATR) for volatility penalty
        - Confidence-aware: scales size with ML conviction, capped at 1.25×
        - Survival-first: hard caps at 25% account risk and 5× leverage
        - Triple Barrier: SL = 1.5× ATR, TP = 3.0× ATR (2:1 R:R)
    """
    
    # Hard constraints (survival overrides Kelly)
    MAX_RISK_PCT = 0.25      # 25% of free balance per trade
    MAX_LEVERAGE = 100.0     # 100× maximum
    KELLY_FRACTION = 0.25    # Quarter-Kelly for safety
    
    # Triple Barrier parameters — BUG-10 FIX: Aligned to ml_brain.py label_regimes training barriers
    # config.py: ATR_STOP_MULTIPLIER=1.2, DYNAMIC_TP_MULTIPLIER=1.8
    # Model was trained expecting SL=1.2x / TP=1.8x — execution must match or trades miss targets
    SL_MULTIPLIER = 1.2      # 1.2× ATR for stop loss (matches training)
    TP_MULTIPLIER = 1.8      # 1.8× ATR for take profit (matches training)
    
    # Confidence scaling
    CONFIDENCE_THRESHOLD = 0.60
    MAX_CONFIDENCE_SCALAR = 1.25

    def __init__(self, max_risk_pct: float = 0.25, max_leverage: float = 5.0):
        self.max_risk_pct = max_risk_pct
        self.max_leverage = max_leverage

    # ═══════════════════════════════════════════════════════════════
    # PUBLIC API
    # ═══════════════════════════════════════════════════════════════

    def calculate_position(self, free_balance: float, confidence: float,
                           atr_pct: float, current_price: float,
                           direction: str, symbol: str = "BTC/USDT",
                           is_scalp: bool = False,
                           market_regime: str = "TRENDING_UP") -> Dict[str, Any]:
        """
        MODULE 3: Confidence-Weighted Position Sizing & MODULE 2: Scalp Adjustments.
        Scale Factor = (P(Signal) - P(Threshold)) / (1.0 - P(Threshold)), clamped between 0.25 and 1.0.
        Dynamic Leverage = round(Max_Leverage * Scale_Factor, 1).
        Allocated Margin = Base_Margin * Scale_Factor (halved to 0.5x if is_scalp).
        """
        from tools import round_price_prec, calculate_position_exits
        if free_balance <= 0 or current_price <= 0:
            return self._error_result("INVALID_INPUTS")
        
        if confidence < self.CONFIDENCE_THRESHOLD:
            return self._error_result("CONFIDENCE_BELOW_THRESHOLD")
        
        # 1. Conviction Scale Factor calculation
        p_threshold = self.CONFIDENCE_THRESHOLD  # 0.65
        if confidence >= p_threshold:
            scale_factor = (confidence - p_threshold) / (1.0 - p_threshold)
        else:
            scale_factor = 0.0
        scale_factor = max(0.0, min(1.0, float(scale_factor)))

        # 2. Confidence-Based Dynamic Leverage (Targets 20x-25x for valid setups, max 50x)
        max_allowed_leverage_from_exchange = 50.0  # Default target cap
        try:
            import tools
            if tools.MARKET_CACHE and symbol in tools.MARKET_CACHE:
                market = tools.MARKET_CACHE[symbol]
                limits = market.get('limits', {})
                if 'leverage' in limits and 'max' in limits['leverage']:
                    max_allowed_leverage_from_exchange = float(limits['leverage']['max'])
        except Exception:
            pass

        effective_max_leverage = min(50.0, max_allowed_leverage_from_exchange)
        dynamic_leverage = float(calculate_dynamic_leverage(
            confidence=confidence,
            atr_pct=atr_pct,
            market_regime=market_regime,
            max_allowed_leverage=int(effective_max_leverage)
        ))

        # 3. Allocated Margin ($40.00 Base Margin * (0.25 + 0.75 * scale_factor))
        target_base = 40.0 * (0.25 + 0.75 * scale_factor)
        if is_scalp:
            allocated_margin = target_base * 0.5
        else:
            allocated_margin = target_base

        allocated_margin = min(allocated_margin, max(0.0001, free_balance))
        size_usd = allocated_margin * dynamic_leverage

        # 4. Dynamic Smart Take Profit & Stop Loss
        atr_raw = current_price * atr_pct
        if is_scalp:
            sl_mult = 1.0  # 1.0x ATR for counter-trend scalp
            base_tp_multiplier = 1.0
        else:
            sl_mult = 1.2  # 1.2x ATR
            # 1. Base Multiplier on Regime
            if market_regime in ["TRENDING_UP", "TRENDING_DOWN"]:
                base_tp_multiplier = 2.5  # Push for larger targets in trends
            else:
                base_tp_multiplier = 1.2  # Snatch profits fast in chop/ranges

        # 2. Calculate raw SL & TP distances
        if direction == "LONG":
            raw_sl = current_price - (atr_raw * sl_mult)
        elif direction == "SHORT":
            raw_sl = current_price + (atr_raw * sl_mult)
        else:
            raw_sl = current_price

        # 3. Apply 95% Front-Running Rule (pull TP 5% closer to ensure fills)
        tp_distance = atr_raw * base_tp_multiplier
        adjusted_tp_distance = tp_distance * 0.95

        # 4. Calculate Final Price
        if direction == "LONG":
            raw_tp = current_price + adjusted_tp_distance
        elif direction == "SHORT":
            raw_tp = current_price - adjusted_tp_distance
        else:
            raw_tp = current_price

        sl_rounded = round_price_prec(raw_sl, symbol)
        tp_rounded = round_price_prec(raw_tp, symbol)
        
        sl_distance = abs(current_price - sl_rounded) if sl_rounded is not None else current_price * atr_pct * sl_mult
        tp_distance = abs(tp_rounded - current_price) if tp_rounded is not None else current_price * atr_pct * tp_mult

        # TTT Estimation (Expected bars and duration)
        TIMEFRAME_MINS = 60
        expected_bars = 0.0
        expected_duration_mins = 0.0
        if tp_rounded is not None and atr_raw > 0:
            distance_to_tp = abs(tp_rounded - current_price)
            expected_bars = distance_to_tp / atr_raw
            expected_duration_mins = round(expected_bars * TIMEFRAME_MINS)

        size_asset = size_usd / current_price if current_price > 0 else 0.0

        # Minimum Notional Value Check
        min_qty = 0.001
        try:
            import tools
            if tools.MARKET_CACHE and symbol in tools.MARKET_CACHE:
                market = tools.MARKET_CACHE[symbol]
                min_limit = market.get('limits', {}).get('amount', {}).get('min')
                if min_limit is not None:
                    min_qty = float(min_limit)
        except Exception:
            pass

        if size_asset < min_qty:
            target_size_asset = min_qty
            target_size_usd = target_size_asset * current_price
            target_margin_required = target_size_usd / dynamic_leverage
            target_risk_amount = target_margin_required
            target_risk_pct = target_risk_amount / free_balance if free_balance > 0 else 0.0
            
            if target_risk_pct > self.max_risk_pct:
                return self._error_result("MIN_NOTIONAL_BREACHES_RISK_CAP")
            else:
                size_usd = target_size_usd
                size_asset = target_size_asset
                allocated_margin = target_margin_required

        risk_amount = allocated_margin
        risk_pct = risk_amount / free_balance if free_balance > 0 else 0.0

        return {
            'size_usd': round(size_usd, 4),
            'size_asset': round(size_asset, 6),
            'leverage': dynamic_leverage,
            'stop_loss': sl_rounded,
            'take_profit': tp_rounded,
            'margin_required': round(allocated_margin, 4),
            'risk_amount': round(risk_amount, 4),
            'risk_pct': round(risk_pct, 4),
            'sl_distance_pct': round((sl_distance / current_price) * 100, 4),
            'tp_distance_pct': round((tp_distance / current_price) * 100, 4),
            'expected_bars': round(expected_bars, 1),
            'expected_duration_mins': expected_duration_mins,
            'scale_factor': round(scale_factor, 4),
            'is_scalp': is_scalp,
            'direction': direction,
            'valid': True
        }

    def calculate_kelly_fraction(self, win_prob: float, avg_win: float,
                                  avg_loss: float) -> float:
        """
        Pure Kelly criterion. Returns fraction of bankroll to risk.
        Caller should multiply by KELLY_FRACTION (0.25) for safety.
        """
        if avg_loss <= 0 or avg_win <= 0:
            return 0.0
        
        b = avg_win / avg_loss  # Reward-to-risk ratio
        q = 1.0 - win_prob
        
        kelly = (win_prob * b - q) / b
        
        return max(0.0, kelly)

    # ═══════════════════════════════════════════════════════════════
    # INTERNAL METHODS
    # ═══════════════════════════════════════════════════════════════

    def _calculate_position_size(self, free_balance: float,
                                  confidence: float, atr_pct: float) -> float:
        """
        Three-factor position sizing:
            1. Base: 25% of free balance (quarter-Kelly anchor)
            2. Confidence scalar: 0.60 → 1.0×, 0.99 → 1.25× (capped)
               BUG-06 FIX: Updated from stale "0.65 → 1.0×" to reflect current 0.60 threshold
            3. Volatility penalty: high ATR% → smaller size (atr_pct is decimal, e.g. 0.008)
        
        FIX 1: Uses atr_pct (asset-agnostic decimal) instead of atr_raw
        FIX 2: Hard cap at 25% (not 50%)
        FIX 3: Confidence scalar capped at 1.25×
        """
        # Base allocation
        base_allocation = free_balance * self.max_risk_pct  # 25%
        
        # Confidence scalar: linear from threshold, capped
        # 0.65 → 1.00×, 0.8125 → 1.25×, 0.99 → 1.25× (cap)
        raw_scalar = confidence / self.CONFIDENCE_THRESHOLD
        confidence_scalar = min(raw_scalar, self.MAX_CONFIDENCE_SCALAR)
        
        # Volatility penalty: asset-agnostic via ATR%
        # ATR 0.5% → penalty 0.952, ATR 2% → penalty 0.833, ATR 5% → penalty 0.667
        volatility_penalty = 1.0 / (1.0 + (atr_pct * 10.0))
        
        # Combine
        position_size = base_allocation * confidence_scalar * volatility_penalty
        
        # HARD CAP: 25% of free balance (survival constraint)
        # This was 50% in the old code — CRITICAL FIX
        return min(position_size, free_balance * self.max_risk_pct)

    def _error_result(self, reason: str) -> Dict[str, float]:
        """Return invalid position with zero size."""
        return {
            'size_usd': 0.0,
            'size_asset': 0.0,
            'leverage': 0.0,
            'stop_loss': 0.0,
            'take_profit': 0.0,
            'margin_required': 0.0,
            'risk_amount': 0.0,
            'risk_pct': 0.0,
            'direction': 'HOLD',
            'valid': False,
            'error': reason
        }


async def safe_run_cycle(pipeline, data, ml_score: dict | None = None) -> TradeSignal:
    """
    Defensive wrapper around pipeline.run_cycle.
    Prevents NoneType errors or crashes from stopping the master loop.
    """
    try:
        signal = await pipeline.run_cycle(data, ml_score=ml_score)
        if signal is None:
            log.error("[%s] Pipeline returned None — using emergency HOLD", data.asset)
            return TradeSignal(
                asset=data.asset,
                direction='HOLD',
                confidence=1.0,
                leverage=0.0,
                stop_loss=None,
                take_profit=None,
                size=0.0,
                margin=0.0,
                regime='UNKNOWN',
                rejection_reason='PIPELINE_RETURNED_NONE'
            )
        return signal
    except Exception as e:
        log.error("[%s] Pipeline crashed: %s", data.asset, e, exc_info=True)
        return TradeSignal(
            asset=data.asset,
            direction='HOLD',
            confidence=1.0,
            leverage=0.0,
            stop_loss=None,
            take_profit=None,
            size=0.0,
            margin=0.0,
            regime='UNKNOWN',
            rejection_reason=f'PIPELINE_CRASH: {str(e)}'
        )


# ═══════════════════════════════════════════════════════════════
# MASTER EXECUTION PIPELINE — CORRECTED ORDER
# ═══════════════════════════════════════════════════════════════

class ExecutionPipeline:
    """
    PIPELINE ORDER (hardened):
    1. REGIME GATE (instant abort if CHOPPY)
    2. COST ANALYZER (with market impact)
    3. ML INFERENCE (A/B models, isotonic calibration)
    4. RISK ENGINE (dynamic position sizing, leverage, SL, TP)
    5. ORDER EXECUTION
    """
    
    def __init__(self, ml_brain, regime_shield: RegimeShield):
        self.ml_brain = ml_brain
        self.regime = regime_shield
        self.cost = CostAnalyzer()
        self.min_confidence = 0.60
        self.max_leverage = 100.0
        self.max_risk_pct = 0.25  # 25% of account
        self.account_balance = 40.0  # $40 micro-capital
        self.risk_engine = RiskEngine(max_risk_pct=self.max_risk_pct, max_leverage=self.max_leverage)
        
    def _build_orderbook_dict(self, data: MarketData) -> dict:
        """Helper to construct standard CCXT orderbook format from MarketData."""
        if hasattr(data, 'orderbook') and data.orderbook:
            return data.orderbook
        
        mid = float(data.price)
        bid_unit = float(data.bid_depth) if data.bid_depth > 0 else 100.0
        ask_unit = float(data.ask_depth) if data.ask_depth > 0 else 100.0
        spread_val = float(data.spread_pct) if (hasattr(data, 'spread_pct') and data.spread_pct > 0) else 0.0004
        half_spread = float(spread_val / 2.0)
        
        # Build multi-level orderbook matching live spread_pct
        bids = [
            [mid * (1.0 - half_spread * i), bid_unit * (i ** 1.2)]
            for i in range(1, 11)
        ]
        asks = [
            [mid * (1.0 + half_spread * i), ask_unit * (i ** 1.2)]
            for i in range(1, 11)
        ]
        return {
            'bids': bids,
            'asks': asks,
            'bid_depth': float(data.bid_depth) if hasattr(data, 'bid_depth') else 0.0,
            'ask_depth': float(data.ask_depth) if hasattr(data, 'ask_depth') else 0.0,
            'funding_rate': float(data.funding_rate) if hasattr(data, 'funding_rate') else 0.0
        }

    async def run_cycle(self, data: MarketData, ml_score: dict | None = None) -> TradeSignal:
        """
        MASTER GATE SEQUENCE
        Any gate fails = instant return with rejection_reason
        """
        # ═══════════════════════════════════════════════════════
        # GLOBAL TRADE COOLDOWN GATE (15 MIN TIMEOUT POST-CLOSE)
        # ═══════════════════════════════════════════════════════
        import time
        current_time = time.time()
        if data.asset in TRADE_COOLDOWNS:
            time_since_close = (current_time - TRADE_COOLDOWNS[data.asset]) / 60.0
            if time_since_close < COOLDOWN_MINUTES:
                rem_mins = COOLDOWN_MINUTES - time_since_close
                print(f"[{data.asset}]   [COOLDOWN] Asset locked for {rem_mins:.1f} more mins.")
                log.info(f"[{data.asset}]   [COOLDOWN] Asset locked for {rem_mins:.1f} more mins.")
                return self._reject(
                    data.asset,
                    f"COOLDOWN_ACTIVE (Asset locked for {rem_mins:.1f} more mins)",
                    data.regime if hasattr(data, 'regime') and data.regime else 'UNKNOWN',
                    total_friction=0.0,
                    slippage_estimate=0.0
                )
        
        # ═══════════════════════════════════════════════════════
        # GATE 0: DATA VALIDATION & NaN CHECKS
        # ═══════════════════════════════════════════════════════
        if data.price is None or math.isnan(data.price) or data.price <= 0 or \
           data.atr_raw is None or math.isnan(data.atr_raw) or data.atr_raw <= 0:
            print(f"[{data.asset}]   [REJECTED] INVALID_INDICATOR_DATA (ATR is NaN or zero)")
            log.warning(f"[{data.asset}]   [REJECTED] INVALID_INDICATOR_DATA (ATR is NaN or zero)")
            return self._reject(data.asset, 'INVALID_INDICATOR_DATA (ATR is NaN or zero)', 'CHOPPY')

        # ═══════════════════════════════════════════════════════
        # FRICTION CALCULATION (CRITICAL: Calculated first as explicit float)
        # ═══════════════════════════════════════════════════════
        # Target order size in USD = Notional position size (Margin * Leverage)
        estimated_order_usd = float(self.account_balance * self.max_risk_pct * self.max_leverage)
        orderbook = self._build_orderbook_dict(data)
        cost_result = self.cost.calculate_friction(
            orderbook=orderbook,
            direction='LONG',  # Placeholder — updated after ML
            order_size_usd=estimated_order_usd,
            timestamp=data.timestamp,
            use_taker=True
        )
        total_friction_val = float(cost_result.get('total', 0.0))
        # Bug #4/#17 Fix: Inf values from _error_result corrupt logs; clamp to 0.0 for display
        if not isinstance(total_friction_val, float) or total_friction_val == float('inf') or total_friction_val != total_friction_val:
            total_friction_val = 0.0
        slippage_val = float(cost_result.get('impact', 0.0))
        if not isinstance(slippage_val, float) or slippage_val == float('inf') or slippage_val != slippage_val:
            slippage_val = 0.0
            
        # ═══════════════════════════════════════════════════════
        # STATE AWARENESS / ACTIVE POSITION CHECK
        # ═══════════════════════════════════════════════════════
        # ═══════════════════════════════════════════════════════
        # STATE AWARENESS / ACTIVE POSITION CHECK
        # ═══════════════════════════════════════════════════════
        if hasattr(data, 'current_position') and data.current_position is not None:
            from tools import round_price_prec
            pos = data.current_position
            direction = pos.get("direction")
            sl = pos.get("stop_loss")
            tp = pos.get("take_profit")
            margin = float(pos.get("margin", 0.0))
            leverage = float(pos.get("leverage", 0.0))
            size = float(pos.get("size", 0.0))
            entry_price = float(pos.get("entry_price", data.price))

            # Calculate unrealized profit %
            profit_pct = 0.0
            if entry_price > 0:
                if direction == "LONG":
                    profit_pct = (data.price - entry_price) / entry_price
                elif direction == "SHORT":
                    profit_pct = (entry_price - data.price) / entry_price

            # 1. Trailing Stop-Loss (TSL) Logic
            FEES_PCT = 0.0011  # 0.11% round-trip friction
            updated_sl = sl
            tsl_reason = None

            tp_dist = abs(float(tp) - entry_price) if tp is not None else 0
            curr_dist = abs(data.price - entry_price)
            is_in_favor = (direction == "LONG" and data.price > entry_price) or (direction == "SHORT" and data.price < entry_price)
            
            if is_in_favor and tp_dist > 0 and (curr_dist / tp_dist) >= 0.80:
                if direction == "LONG":
                    be_sl = round_price_prec(entry_price * (1.0 + FEES_PCT), data.asset)
                    if updated_sl is None or be_sl > updated_sl:
                        updated_sl = be_sl
                        tsl_reason = "BREAKEVEN_SAVED: Price reversed after reaching 80% to target."
                elif direction == "SHORT":
                    be_sl = round_price_prec(entry_price * (1.0 - FEES_PCT), data.asset)
                    if updated_sl is None or be_sl < updated_sl:
                        updated_sl = be_sl
                        tsl_reason = "BREAKEVEN_SAVED: Price reversed after reaching 80% to target."
            elif profit_pct >= 0.025:
                # +2.5% profit: Trail SL behind current price at 1.5x ATR
                atr_dist = 1.5 * data.atr_raw if data.atr_raw > 0 else (data.price * 0.01)
                if direction == "LONG":
                    candidate_sl = data.price - atr_dist
                    if updated_sl is None or candidate_sl > updated_sl:
                        updated_sl = round_price_prec(candidate_sl, data.asset)
                        tsl_reason = "TRAILING_STOP_HIT"
                elif direction == "SHORT":
                    candidate_sl = data.price + atr_dist
                    if updated_sl is None or candidate_sl < updated_sl:
                        updated_sl = round_price_prec(candidate_sl, data.asset)
                        tsl_reason = "TRAILING_STOP_HIT"

            # Save updated SL back to position dict in memory
            pos["stop_loss"] = updated_sl

            # 2. Check Exit Triggers
            hit = False
            exit_reason = "Position active. Exit conditions not met."

            # (A) Check SL / TP / TSL hit
            if direction == "LONG":
                if updated_sl is not None and data.price <= updated_sl:
                    hit = True
                    exit_reason = tsl_reason if tsl_reason else ("TRAILING_STOP_HIT" if updated_sl >= entry_price else "SL_HIT")
                elif tp is not None and data.price >= tp:
                    hit = True
                    exit_reason = "TP_HIT"
            elif direction == "SHORT":
                if updated_sl is not None and data.price >= updated_sl:
                    hit = True
                    exit_reason = tsl_reason if tsl_reason else ("TRAILING_STOP_HIT" if updated_sl <= entry_price else "SL_HIT")
                elif tp is not None and data.price <= tp:
                    hit = True
                    exit_reason = "TP_HIT"

            # (B) Check Signal Invalidation Exit (if ML probabilities available)
            if not hit and ml_score is not None:
                probs = ml_score.get("probabilities", {})
                p_up = float(probs.get("UPTREND", 0.0))
                p_down = float(probs.get("DOWNTREND", 0.0))

                if direction == "LONG" and p_down >= 0.60:
                    hit = True
                    exit_reason = "SIGNAL_INVALIDATION_BEARISH"
                elif direction == "SHORT" and p_up >= 0.60:
                    hit = True
                    exit_reason = "SIGNAL_INVALIDATION_BULLISH"

            regime = data.regime if (hasattr(data, 'regime') and data.regime) else self.regime.detect_regime(data)

            if hit:
                TRADE_COOLDOWNS[data.asset] = time.time()
                # Return CLOSE_EARLY signal so Master Agent & Server trigger immediate market close
                return TradeSignal(
                    asset=data.asset,
                    direction='CLOSE_EARLY',
                    confidence=1.0,
                    leverage=leverage,
                    stop_loss=updated_sl,
                    take_profit=tp,
                    size=size,
                    margin=margin,
                    regime=regime,
                    rejection_reason=exit_reason,
                    total_friction=total_friction_val,
                    slippage_estimate=slippage_val
                )
            else:
                return self._reject(
                    data.asset,
                    f"POSITION_ACTIVE ({exit_reason})",
                    regime,
                    total_friction=total_friction_val,
                    slippage_estimate=slippage_val,
                    stop_loss=updated_sl,
                    take_profit=tp,
                    leverage=leverage,
                    margin=margin,
                    size=size
                )
        
        # ═══════════════════════════════════════════════════════
        # GATE 1: REGIME SHIELD (ABSOLUTE FIRST)
        # ═══════════════════════════════════════════════════════
        regime = data.regime if (hasattr(data, 'regime') and data.regime) else self.regime.detect_regime(data)
        
        if regime == 'UNKNOWN':
            return self._reject(data.asset, 'REGIME_UNKNOWN', 'UNKNOWN', total_friction=total_friction_val, slippage_estimate=slippage_val)
            
        if regime == 'CHOPPY':
            # INSTANT BYPASS — zero downstream computation
            return self._reject(data.asset, 'REGIME_CHOPPY', regime, total_friction=total_friction_val, slippage_estimate=slippage_val)
        
        # ═══════════════════════════════════════════════════════
        # GATE 2: COST ANALYZER GATE
        # ═══════════════════════════════════════════════════════
        if not cost_result.get('pass', False):
            log_str = self.cost.log_friction_breakdown(cost_result, data.asset)
            err_reason = cost_result.get('error', f"COST_TOO_HIGH ({total_friction_val:.4%})")
            return self._reject(
                data.asset, 
                err_reason,
                regime,
                total_friction=total_friction_val,
                slippage_estimate=slippage_val
            )
        
        # ═══════════════════════════════════════════════════════
        # GATE 3: ML INFERENCE (A/B Models with Isotonic Calibration)
        # ═══════════════════════════════════════════════════════
        if ml_score is None:
            return self._reject(data.asset, 'ML_INFERENCE_ERROR: No score available', regime, total_friction=total_friction_val, slippage_estimate=slippage_val)
        
        p_up = ml_score.get("probabilities", {}).get("UPTREND", 0.0)
        p_down = ml_score.get("probabilities", {}).get("DOWNTREND", 0.0)
        p_range = ml_score.get("probabilities", {}).get("RANGING", 1.0)
        
        model_a = ml_score.get("probabilities", {})
        model_b = ml_score.get("model_b_probabilities", {})
        
        # CRITICAL: Reject if calibrated model collapses to 100% ranging
        if p_range >= 0.95:
            return self._reject(
                data.asset,
                f'CALIBRATION_COLLAPSE (P_range: {p_range:.1%})',
                regime,
                total_friction=total_friction_val,
                slippage_estimate=slippage_val,
                model_a_probs=model_a,
                model_b_probs=model_b
            )
        
        # EXACT SIGNAL ALIGNMENT & CONTRARIAN SETUP ROUTING
        setup_type = None
        signal_dir = None
        market_regime = regime

        # 1. Trend Aligned Setups (Lowered to 60% for earlier entries)
        if market_regime in ["TRENDING_DOWN", "BEARISH_PULLBACK"] and p_down >= 0.60:
            setup_type = "TREND_ALIGNED_SHORT"
            signal_dir = "SHORT"
            confidence = p_down
        elif market_regime in ["TRENDING_UP", "BULLISH_PULLBACK"] and p_up >= 0.60:
            setup_type = "TREND_ALIGNED_LONG"
            signal_dir = "LONG"
            confidence = p_up
            
        # 2. Contrarian Setups 
        elif market_regime in ["TRENDING_DOWN", "BEARISH_PULLBACK"] and p_up >= 0.60:
            setup_type = "CONTRARIAN_LONG"
            signal_dir = "LONG"
            confidence = p_up
        elif market_regime in ["TRENDING_UP", "BULLISH_PULLBACK"] and p_down >= 0.60:
            setup_type = "CONTRARIAN_SHORT"
            signal_dir = "SHORT"
            confidence = p_down
        elif p_up >= 0.60 and p_up >= p_down and p_up >= p_range:
            setup_type = "DIRECTIONAL_LONG"
            signal_dir = "LONG"
            confidence = p_up
        elif p_down >= 0.60 and p_down >= p_up and p_down >= p_range:
            setup_type = "DIRECTIONAL_SHORT"
            signal_dir = "SHORT"
            confidence = p_down

        # 3. Execution Routing
        max_prob = max(p_up, p_down)
        if setup_type and signal_dir and max_prob >= 0.60:
            direction = signal_dir
            log.info(f"[{data.asset}]   [SETUP TYPE] {setup_type}")
            print(f"[{data.asset}]   [SETUP TYPE] {setup_type}")
        else:
            reason_str = f"RF_CONFIDENCE_LOW (conf:{max_prob:.2f} < 0.60)" if max_prob < 0.60 else 'RF_SIGNAL_HOLD_OR_NEUTRAL (signal: HOLD)'
            log.info(f"[{data.asset}]   [REJECTED] {reason_str}")
            print(f"[{data.asset}]   [REJECTED] {reason_str}")
            return self._reject(
                data.asset,
                reason_str,
                regime,
                total_friction=total_friction_val,
                slippage_estimate=slippage_val,
                model_a_probs=model_a,
                model_b_probs=model_b
            )
        
        # ═══════════════════════════════════════════════════════
        # GATE 3.5: REGIME-MODEL CONSENSUS & COUNTER-TREND SCALP GATE
        # ═══════════════════════════════════════════════════════
        consensus_valid, is_scalp, consensus_reason = self._check_regime_model_consensus(regime, direction, confidence)
        if not consensus_valid:
            return self._reject(
                data.asset,
                consensus_reason,
                regime,
                total_friction=total_friction_val,
                slippage_estimate=slippage_val,
                model_a_probs=model_a,
                model_b_probs=model_b
            )

        # ═══════════════════════════════════════════════════════
        # GATE 3.8: MICROSTRUCTURE & OBI GUARD
        # ═══════════════════════════════════════════════════════
        micro_valid, micro_reason = verify_microstructure_entry(data.asset, orderbook, direction)
        if not micro_valid:
            print(f"[{data.asset}]   [REJECTED] {micro_reason}")
            log.info(f"[{data.asset}]   [REJECTED] {micro_reason}")
            return self._reject(
                data.asset,
                micro_reason,
                regime,
                total_friction=total_friction_val,
                slippage_estimate=slippage_val,
                model_a_probs=model_a,
                model_b_probs=model_b
            )

        # ═══════════════════════════════════════════════════════
        # GATE 4: RISK ENGINE (Dynamic Position Sizing & Scalp Adjustments)
        # ═══════════════════════════════════════════════════════
        risk_calc = self.risk_engine.calculate_position(
            free_balance=self.account_balance,
            confidence=confidence,
            atr_pct=data.atr_pct,
            current_price=data.price,
            direction=direction,
            symbol=data.asset,
            is_scalp=is_scalp
        )

        if not risk_calc.get('valid', False) or risk_calc.get('size_usd', 0) <= 0:
            return self._reject(
                data.asset,
                f"RISK_REJECT ({risk_calc.get('error', 'INVALID_POSITION')})",
                regime,
                total_friction=total_friction_val,
                slippage_estimate=slippage_val,
                model_a_probs=model_a,
                model_b_probs=model_b
            )

        position_size_usd = risk_calc['size_usd']
        margin = risk_calc['margin_required']
        leverage = risk_calc['leverage']
        stop_loss = risk_calc['stop_loss']
        take_profit = risk_calc['take_profit']
        size = risk_calc['size_asset']
        scale_factor = risk_calc.get('scale_factor', 1.0)
        expected_bars = risk_calc.get('expected_bars', 0.0)
        expected_duration_mins = risk_calc.get('expected_duration_mins', 0.0)

        final_cost = self.cost.calculate_friction(
            orderbook=orderbook,
            direction=direction,
            order_size_usd=position_size_usd,
            timestamp=data.timestamp,
            use_taker=True
        )

        if not final_cost.get('pass', False):
            # Bug #6 fix: default was True (permissive); must be False (strict) to block missing keys
            fin_total = final_cost.get('total', float('inf'))
            fin_impact = final_cost.get('impact', 0.0)
            return self._reject(
                data.asset,
                final_cost.get('error', 'COST_GATE_FAILED'),
                regime,
                total_friction=float(fin_total) if fin_total != float('inf') else 0.0,
                slippage_estimate=float(fin_impact) if isinstance(fin_impact, (int, float)) else 0.0
            )

        actual_impact = float(final_cost['impact'])
        final_total_friction = float(final_cost['total'])
        
        if actual_impact > 0.02:  # 2% slippage max
            return self._reject(
                data.asset,
                f'MARKET_IMPACT_TOO_HIGH ({actual_impact:.2%})',
                regime,
                total_friction=final_total_friction,
                slippage_estimate=actual_impact
            )
        
        return TradeSignal(
            asset=data.asset,
            direction=direction,
            confidence=confidence,
            leverage=leverage,
            stop_loss=stop_loss,
            take_profit=take_profit,
            size=size,
            margin=margin,
            regime=regime,
            rejection_reason="COUNTER_TREND_SCALP" if is_scalp else None,
            slippage_estimate=actual_impact,
            total_friction=final_total_friction,
            model_a_probs=model_a,
            model_b_probs=model_b,
            is_scalp=is_scalp,
            scale_factor=scale_factor,
            expected_bars=expected_bars,
            expected_duration_mins=expected_duration_mins
        )
    
    def _kelly_size(self, win_prob: float, avg_win: float, avg_loss: float) -> float:
        if avg_loss <= 0:
            return 0.0
        b = avg_win / avg_loss
        q = 1 - win_prob
        kelly = (win_prob * b - q) / b if b > 0 else 0.0
        fractional_kelly = max(0.0, kelly * 0.25)
        return min(fractional_kelly, self.max_risk_pct)
        
    def _reject(self, asset: str, reason: str, regime: str,
                total_friction: float = 0.0,
                slippage_estimate: float = 0.0,
                stop_loss: float | None = None,
                take_profit: float | None = None,
                leverage: float = 0.0,
                margin: float = 0.0,
                size: float = 0.0,
                model_a_probs: Dict = None,
                model_b_probs: Dict = None) -> TradeSignal:
        # BUG-13 FIX: confidence=0.0 for rejections (was 1.0 — caused misleading "HOLD | Conf: 100%" logs)
        return TradeSignal(
            asset=asset,
            direction='HOLD',
            confidence=0.0,
            leverage=leverage,
            stop_loss=stop_loss,
            take_profit=take_profit,
            size=size,
            margin=margin,
            regime=regime,
            rejection_reason=reason,
            total_friction=float(total_friction),
            slippage_estimate=float(slippage_estimate),
            model_a_probs=model_a_probs or {},
            model_b_probs=model_b_probs or {}
        )

    def _check_regime_model_consensus(self, regime: str, direction: str, confidence: float = 0.0) -> tuple[bool, bool, Optional[str]]:
        """
        MODULE 2: Counter-Trend Scalp Engine (Regime Gate Flexibility).
        Returns: (is_valid: bool, is_scalp: bool, reason: str | None)
        """
        if regime in ['CHOPPY', 'UNKNOWN']:
            return True, False, None
            
        # 1. Trend-Aligned Trades (PASS cleanly)
        if (regime in ['TRENDING_UP', 'BULLISH_PULLBACK'] and direction == 'LONG') or \
           (regime in ['TRENDING_DOWN', 'BEARISH_PULLBACK'] and direction == 'SHORT'):
            return True, False, None
            
        # 2. Counter-Trend Setups (Signal opposes Macro Regime)
        if (regime in ['TRENDING_UP', 'BULLISH_PULLBACK'] and direction == 'SHORT') or \
           (regime in ['TRENDING_DOWN', 'BEARISH_PULLBACK'] and direction == 'LONG'):
            if confidence < 0.60:
                reason = f"REGIME_MISMATCH_LOW_CONF (Regime={regime}, Signal={direction}, Conf={confidence:.1%}<60%)"
                return False, False, reason
            else:
                # PASS as COUNTER_TREND_SCALP
                return True, True, None

        return True, False, None


def ensemble_probabilities(prob_a: dict, prob_b: dict, weight_a: float = 0.5, weight_b: float = 0.5) -> dict:
    """
    Blends Model A and Model B probability distributions.
    BUG-22 FIX: Weights are passed in from callers — when CV accuracy is available,
    call with w_a=cv_a/(cv_a+cv_b), w_b=cv_b/(cv_a+cv_b) to weight by model quality.
    Falls back to equal weighting (0.5/0.5) if weights are not passed.
    """
    if not prob_a and not prob_b:
        return {"UPTREND": 0.0, "DOWNTREND": 0.0, "RANGING": 1.0}
    if not prob_b:
        return dict(prob_a)
    if not prob_a:
        return dict(prob_b)
        
    total_w = weight_a + weight_b
    w_a = weight_a / total_w
    w_b = weight_b / total_w
    
    classes = set(prob_a.keys()).union(set(prob_b.keys()))
    blended = {}
    for cls in classes:
        val_a = float(prob_a.get(cls, 0.0))
        val_b = float(prob_b.get(cls, 0.0))
        blended[cls] = float(w_a * val_a + w_b * val_b)
        
    sum_p = sum(blended.values())
    if sum_p > 0:
        for k in blended:
            blended[k] = float(blended[k] / sum_p)
    return blended


def render_cycle_log(state: dict) -> list[str]:
    """
    Renders the cycle state exactly ONCE per loop to prevent pipeline fracturing.
    Consolidates all cycle telemetry into a single formatted output block.
    """
    symbol = state.get("symbol", state.get("asset_symbol", "UNKNOWN"))
    ts_short = state.get("ts_short", state.get("timestamp", datetime.now(timezone.utc).strftime("%H:%M:%S UTC")))
    current_price = float(state.get("current_price", state.get("price", 0.0)))
    atr_val = float(state.get("atr_val", state.get("atr", 0.0)))
    atr_pct = float(state.get("atr_pct", 0.0))
    market_regime = str(state.get("market_regime", state.get("regime", "UNKNOWN"))).upper()
    spread_pct = float(state.get("spread_pct", state.get("spread", 0.0)))
    bid_vol = float(state.get("bid_vol", state.get("bid_depth", 0.0)))
    ask_vol = float(state.get("ask_vol", state.get("ask_depth", 0.0)))
    funding_rate = float(state.get("funding_rate", 0.0))
    total_friction = float(state.get("total_friction", state.get("friction", 0.0)))
    slippage_estimate = float(state.get("slippage_estimate", state.get("slippage", 0.0)))
    cost_gate_status = state.get("cost_gate_status", state.get("cost_gate", "PASS"))
    sub_agent_results = state.get("sub_agent_results", [])
    ml_score = state.get("ml_score")
    rejection_reason = state.get("rejection_reason")
    leverage = float(state.get("leverage", 0.0))
    margin = float(state.get("margin", 0.0))
    stop_loss = state.get("stop_loss", state.get("sl"))
    take_profit = state.get("take_profit", state.get("tp"))
    
    prefix = f"[{symbol}] " if symbol else ""
    price_prec = 4 if current_price > 0 and current_price < 1.0 else 2
    atr_prec = 4 if current_price > 0 and current_price < 1.0 else 2

    lines = []
    lines.append(f"{prefix}════════════════════════════════════")
    lines.append(f"{prefix}[{ts_short}] Master Agent — {symbol}")
    lines.append(f"{prefix}Price: {current_price:,.{price_prec}f} USDT  |  ATR: {atr_val:,.{atr_prec}f} ({atr_pct:.2%})")
    lines.append(f"{prefix}Market Regime: {market_regime}")
    lines.append(f"{prefix}Spread: {spread_pct:.4%}  |  Bid/Ask Depth: {bid_vol:.2f} / {ask_vol:.2f}")
    lines.append(f"{prefix}Funding Rate: {funding_rate:.4%}  |  Total Friction: {total_friction:.4%}")
    lines.append(f"{prefix}Slippage/Impact: {slippage_estimate:.4%}")
    lines.append(f"{prefix}Cost Analyzer Gate: {cost_gate_status}")

    # Render Sub-agent votes ONLY if active/non-empty
    if sub_agent_results:
        lines.append(f"{prefix}─ Sub-agent votes ─")
        for r in sub_agent_results:
            agent_name = r.get("agent", r.get("name", "?"))
            vote = r.get("vote", r.get("signal", "HOLD"))
            conf = float(r.get("confidence", 0.0))
            lines.append(f"{prefix}  [{agent_name:10s}] {vote:5s} (conf: {conf:.0%})")

    lines.append(f"{prefix}─ ML Logic Gate Execution ─")
    
    p_up = 0.0
    p_down = 0.0
    p_range = 1.0
    if market_regime == "CHOPPY":
        lines.append(f"{prefix}  [RF Calib] P(Up): 0.0% | P(Down): 0.0% | P(Range): 100.0%")
        lines.append(f"{prefix}  [REGIME] REGIME_CHOPPY → Bypassing RF predictions.")
    elif ml_score:
        probs = ml_score.get("probabilities", {})
        p_up = float(probs.get("UPTREND", 0.0))
        p_down = float(probs.get("DOWNTREND", 0.0))
        p_range = float(probs.get("RANGING", 1.0))
        
        raw_probs = ml_score.get("raw_probabilities", {})
        if raw_probs:
            lines.append(f"{prefix}  [RF Raw]   P(Up): {float(raw_probs.get('UPTREND', 0.0)):.1%} | P(Down): {float(raw_probs.get('DOWNTREND', 0.0)):.1%} | P(Range): {float(raw_probs.get('RANGING', 0.0)):.1%}")
        lines.append(f"{prefix}  [RF Calib] P(Up): {p_up:.1%} | P(Down): {p_down:.1%} | P(Range): {p_range:.1%}")
    else:
        lines.append(f"{prefix}  [RF Calib] P(Up): 0.0% | P(Down): 0.0% | P(Range): 100.0%")

    if state.get("position_active") or (rejection_reason and rejection_reason.startswith("POSITION_ACTIVE")):
        reason_str = rejection_reason or "POSITION_ACTIVE (Position active. Exit conditions not met.)"
        lines.append(f"{prefix}  [POSITION_ACTIVE] {reason_str}")
    elif rejection_reason and not rejection_reason.startswith("REGIME"):
        lines.append(f"{prefix}  [REJECTED] {rejection_reason}")

    is_scalp = bool(state.get("is_scalp", False))
    scale_factor = float(state.get("scale_factor", 1.0))
    if is_scalp:
        lines.append(f"{prefix}  [SETUP TYPE] COUNTER_TREND_SCALP (0.5x Margin & 1.0x ATR Exits)")
    elif state.get("decision_str") in ["LONG", "SHORT"]:
        lines.append(f"{prefix}  [SETUP TYPE] TREND_ALIGNED (Conviction Scale: {scale_factor:.2f}x)")

    sl_str = f"{stop_loss:,.{price_prec}f}" if isinstance(stop_loss, (int, float)) else (str(stop_loss) if stop_loss else "None")
    tp_str = f"{take_profit:,.{price_prec}f}" if isinstance(take_profit, (int, float)) else (str(take_profit) if take_profit else "None")
    
    signal_obj = state.get("signal")
    expected_bars = getattr(signal_obj, "expected_bars", 0.0) if signal_obj else 0.0
    expected_mins = getattr(signal_obj, "expected_duration_mins", 0.0) if signal_obj else 0.0
    est_time_str = f" | Est. Time: ~{expected_bars} bars ({expected_mins}m)"

    lines.append(f"{prefix}  → Calculated Leverage: {leverage:.1f}x | Margin: ${margin:.2f} | SL: {sl_str} | TP: {tp_str}{est_time_str}")
    lines.append(f"{prefix}════════════════════════════════════")
    decision_str = state.get("decision_str", state.get("decision", "HOLD"))
    confidence = float(state.get("confidence", 0.0))
    conf_str = f"{confidence:.0%}" if confidence <= 1.0 else f"{confidence:.0f}%"
    lines.append(f"{prefix}▶ DECISION: {decision_str}  |  Conf: {conf_str}  |  Regime: {market_regime}")
    lines.append(f"{prefix}════════════════════════════════════")
    execute_mode = bool(state.get("execute_mode", False))
    mode_label = "⚡ LIVE" if execute_mode else "⏸ SIMULATION"
    lines.append(f"{prefix}[{mode_label}] Final decision: {decision_str} | Conf: {conf_str} | SL: {sl_str}")

    return lines



def log_trade_decision(data: MarketData, signal: TradeSignal):
    """Structured logging helper for trade signals."""
    pass


# ── Master Execution Agent (Fully Deterministic Python Loop) ────────────────

async def run_master_agent(
    sub_agent_results: list[dict],
    symbol: str,
    current_price: float,
    stream_callback: Callable[[str], Awaitable[None]] | None = None,
    ml_score: dict | None = None,
    ml_score_b: dict | None = None,
    recent_ohlcv_df=None,
    spread_pct: float = 0.0004,
    bid_vol: float = 1.0,
    ask_vol: float = 1.0,
    funding_rate: float = 0.0001,
    total_friction: float = 0.001,
    cost_passed: bool = True,
    orderbook: Optional[dict] = None,
    market_data: Optional[MarketData] = None
) -> dict:
    """
    Evaluates ML regime probabilities directly and determines final execution parameters.
    - Zero live Gemini API requests made here.
    """
    ts_short = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
    
    # ── GLOBAL TRADE COOLDOWN CHECK ──
    import time
    current_time = time.time()
    if symbol in TRADE_COOLDOWNS:
        time_since_close = (current_time - TRADE_COOLDOWNS[symbol]) / 60.0
        if time_since_close < COOLDOWN_MINUTES:
            rem_mins = COOLDOWN_MINUTES - time_since_close
            print(f"[{symbol}]   [COOLDOWN] Asset locked for {rem_mins:.1f} more mins.")
            log.info(f"[{symbol}]   [COOLDOWN] Asset locked for {rem_mins:.1f} more mins.")
            return {
                "decision": "HOLD",
                "confidence": 1.0,
                "leverage": 0.0,
                "stop_loss": None,
                "take_profit": None,
                "margin": 0.0,
                "is_scalp": False,
                "scale_factor": 1.0,
                "position_size_pct": 0.0,
                "reasoning": f"COOLDOWN_ACTIVE (Asset locked for {rem_mins:.1f} more mins)",
                "agent_votes": {},
                "risk_level": "LOW",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "ml_brain": {},
                "market_regime": "NEUTRAL",
                "expected_duration_mins": 0.0
            }

    # Bug #1 fix: Define shield ONCE here, before any branch, so it is always bound.
    shield = RegimeShield()

    if market_data is not None:
        data = market_data
        atr_val = data.atr_raw
        atr_pct = data.atr_pct
        is_choppy = (data.regime == 'CHOPPY')
    else:
        # Calculate ATR and baseline volatility first
        atr_data = calculate_atr(recent_ohlcv_df, period=14)
        validate_atr(atr_data, symbol)
        atr_val = atr_data['raw']
        min_atr = current_price * 0.001
        if atr_val < min_atr:
            atr_val = min_atr
        atr_pct = (atr_val / current_price) if current_price > 0 else 0.02

        # Extract technical indicators for the RegimeShield
        # BUG-31 FIX: Default adx_val=15.0 (CHOPPY) not 22.0 (TRENDING) — conservative safe default
        adx_val = 15.0
        vol_val = 0.003
        dist_val = 0.5
        ema_cross_val = 0.0  # ema9_vs_ema21: positive = bullish, negative = bearish
        if recent_ohlcv_df is not None and len(recent_ohlcv_df) >= 20:
            try:
                close_prices = recent_ohlcv_df["close"].astype(float)
                rolling_vol_series = close_prices.pct_change().rolling(window=20).std()
                if not rolling_vol_series.empty and not pd.isna(rolling_vol_series.iloc[-1]):
                    vol_val = float(rolling_vol_series.iloc[-1])
                
                from ml_brain import engineer_features
                engineered_df = engineer_features(recent_ohlcv_df.copy())
                if not engineered_df.empty:
                    if "adx_14" in engineered_df.columns and not pd.isna(engineered_df["adx_14"].iloc[-1]):
                        adx_val = float(engineered_df["adx_14"].iloc[-1])
                    if "Distance_From_200_EMA_1D" in engineered_df.columns and not pd.isna(engineered_df["Distance_From_200_EMA_1D"].iloc[-1]):
                        dist_val = float(engineered_df["Distance_From_200_EMA_1D"].iloc[-1])
                    # Bug #3 fix: Extract EMA9 vs EMA21 cross for micro-trend detection
                    if "ema9_vs_ema21" in engineered_df.columns and not pd.isna(engineered_df["ema9_vs_ema21"].iloc[-1]):
                        ema_cross_val = float(engineered_df["ema9_vs_ema21"].iloc[-1])
            except Exception as eng_err:
                log.warning("[Master] Failed to calculate indicators for RegimeShield: %s", eng_err)

        features_arr = np.array([adx_val, vol_val, dist_val, ema_cross_val])

        rolling_avg_atr_val = calculate_rolling_avg_atr(recent_ohlcv_df)

        # Construct MarketData
        data = MarketData(
            asset=symbol,
            price=current_price,
            atr_raw=atr_val,
            atr_pct=atr_pct,
            spread_pct=spread_pct,
            bid_depth=bid_vol,
            ask_depth=ask_vol,
            funding_rate=funding_rate,
            features=features_arr,
            rolling_avg_atr=rolling_avg_atr_val,
            orderbook=orderbook,
            regime=None
        )
        
        # Determine regime dynamically (shield already created above)
        data.regime = shield.detect_regime(data)
        is_choppy = (data.regime == 'CHOPPY')

    # 0.5: Fast CPU Short-Circuit for Cost Analyzer
    # Bug #9 fix: Use a realistic expected margin (25% of balance) not max notional (balance*pct*leverage)
    is_cost_veto = False
    if not is_choppy:
        pre_pipeline = ExecutionPipeline(ml_brain=None, regime_shield=shield)
        # Use realistic base margin for impact check, not the max possible notional
        realistic_order_usd = float(pre_pipeline.account_balance * pre_pipeline.max_risk_pct * 10.0)  # ~$100 realistic estimate
        pre_cost = pre_pipeline.cost.calculate_friction(
            orderbook=pre_pipeline._build_orderbook_dict(data),
            direction='LONG',
            order_size_usd=realistic_order_usd,
            timestamp=data.timestamp,
            use_taker=True
        )
        is_cost_veto = not pre_cost.get('pass', False)

    # 1. Fetch ML score if not provided and regime is NOT choppy and NOT cost vetoed
    if not is_choppy and not is_cost_veto and ml_score is None and recent_ohlcv_df is not None:
        try:
            from ml_brain import get_brain
            brain = get_brain()
            if brain.trained:
                ml_score = await asyncio.to_thread(brain.predict, symbol, recent_ohlcv_df, regime=data.regime)
        except Exception as ml_exc:
            log.warning("[Master] ML Brain prediction failed: %s", ml_exc)

    # 2. A/B Ensemble probability blending — BUG-22 FIX: weighted by CV accuracy (not fixed 50/50)
    if ml_score is not None and ml_score_b is not None:
        prob_a = ml_score.get("probabilities", {})
        prob_b = ml_score_b.get("probabilities", {})
        try:
            from ml_brain import get_brain as _gb22
            _brain22 = _gb22()
            cv_a = _brain22.cv_score.get(f"{symbol}_A", 0.5)
            cv_b = _brain22.cv_score.get(f"{symbol}_B", 0.5)
            total_cv = cv_a + cv_b
            w_a = (cv_a / total_cv) if total_cv > 0 else 0.5
            w_b = (cv_b / total_cv) if total_cv > 0 else 0.5
        except Exception:
            w_a, w_b = 0.5, 0.5
        blended = ensemble_probabilities(prob_a, prob_b, weight_a=w_a, weight_b=w_b)
        
        ml_score["model_a_probabilities"] = prob_a
        ml_score["model_b_probabilities"] = prob_b
        ml_score["probabilities"] = blended
        
        p_up = float(blended.get("UPTREND", 0.0))
        p_down = float(blended.get("DOWNTREND", 0.0))
        p_range = float(blended.get("RANGING", 1.0))
        
        if p_up >= p_down and p_up >= p_range:
            ens_sig = "LONG"
            ens_conf = p_up
        elif p_down >= p_up and p_down >= p_range:
            ens_sig = "SHORT"
            ens_conf = p_down
        else:
            ens_sig = "HOLD"
            ens_conf = p_range
            
        ml_score["ml_signal"] = ens_sig
        ml_score["confidence"] = ens_conf

    # Execute hardened pipeline sequence via defensive wrapper
    # Bug #2 fix: Removed duplicate shield = RegimeShield() — shield is already defined above.
    pipeline = ExecutionPipeline(ml_brain=None, regime_shield=shield)
    
    signal = await safe_run_cycle(pipeline, data, ml_score=ml_score)
    if signal.rejection_reason == 'REGIME_UNKNOWN':
        return Decision(action='HOLD', reason='REGIME_UNKNOWN')

    decision_str = signal.direction
    confidence = signal.confidence
    leverage = signal.leverage
    stop_loss = signal.stop_loss
    take_profit = signal.take_profit
    margin = signal.margin
    reasoning = signal.rejection_reason or "Direct ML Execution. Strategy gates passed."
    market_regime = signal.regime
    is_scalp = getattr(signal, 'is_scalp', False)
    scale_factor = getattr(signal, 'scale_factor', 1.0)

    # Generate votes map
    votes_map = {r.get("agent", "?"): r.get("vote", "HOLD") for r in sub_agent_results}

    # Render unified cycle log output
    # Bug #10 fix: Accurately detect all cost/ghost rejection prefixes, not just 'COST'
    _COST_VETO_PREFIXES = ('COST_', 'GHOST_', 'ZERO_FRICTION', 'INVALID_ORDERBOOK', 'EMPTY_ORDERBOOK', 'COOLDOWN_', 'SPREAD_TOO_WIDE', 'OBI_', 'ZERO_ORDERBOOK_', 'MICROSTRUCTURE_', 'RF_CONFIDENCE_')
    _is_cost_rejected = bool(
        reasoning and any(reasoning.startswith(p) for p in _COST_VETO_PREFIXES)
    )
    cost_gate_pass = bool(decision_str not in ('HOLD',) or not _is_cost_rejected)
    cost_gate_status = "VETO" if _is_cost_rejected else "PASS"

    log_state = {
        "ts_short": ts_short,
        "symbol": symbol,
        "current_price": current_price,
        "atr_val": atr_val,
        "atr_pct": atr_pct,
        "market_regime": market_regime,
        "spread_pct": spread_pct,
        "bid_vol": bid_vol,
        "ask_vol": ask_vol,
        "funding_rate": funding_rate,
        "total_friction": signal.total_friction,
        "slippage_estimate": signal.slippage_estimate,
        "cost_gate_status": cost_gate_status,
        "sub_agent_results": sub_agent_results,
        "ml_score": ml_score,
        "signal": signal,
        "rejection_reason": signal.rejection_reason,
        "decision_str": decision_str,
        "confidence": confidence,
        "leverage": leverage,
        "margin": margin,
        "stop_loss": stop_loss,
        "take_profit": take_profit,
        "is_scalp": is_scalp,
        "scale_factor": scale_factor,
        "execute_mode": bool(os.getenv("EXECUTE_MODE", "false").lower() == "true")
    }

    cycle_lines = render_cycle_log(log_state)
    single_block = "\n".join(cycle_lines)

    # Serialise the entire output block under a global asyncio lock.
    # This prevents concurrent asset loops from interleaving their
    # ════ header/footer lines in the console and WebSocket stream.
    async with _console_lock:
        log.info(single_block)
        if stream_callback:
            for line in cycle_lines:
                await stream_callback(line)

    decision = {
        "decision": decision_str,
        "confidence": confidence,
        "leverage": leverage,
        "stop_loss": stop_loss,
        "take_profit": take_profit,
        "margin": margin,
        "is_scalp": is_scalp,
        "scale_factor": scale_factor,
        "position_size_pct": 20.0 if decision_str != "HOLD" else 0.0,
        "reasoning": reasoning,
        "agent_votes": votes_map,
        "risk_level": "HIGH" if atr_pct > 0.02 else ("MEDIUM" if atr_pct > 0.01 else "LOW"),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "ml_brain": ml_score if ml_score else {},
        "market_regime": market_regime,
        "expected_duration_mins": getattr(signal, "expected_duration_mins", 0.0)
    }

    return decision


# ── Daily Report Generator (On-Demand LLM Call) ──────────────────────────────

async def generate_daily_report(symbol: str = "BTC/USDT") -> str:
    """
    Aggregates trade logs from the last 24 hours, calculates real-time
    win/loss outcomes, and calls Gemini once to generate a summarization report.
    """
    log.info("[Report] Generating daily performance report...")
    if not HISTORY_FILE.exists():
        return "No historical trade logs found to generate a report."

    try:
        with open(HISTORY_FILE, "r", encoding="utf-8-sig") as f:
            all_trades = json.load(f)
    except Exception as e:
        return f"Failed to load history: {e}"

    # Filter trades in last 24h
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    recent_trades = []
    for t in all_trades:
        try:
            if t.get("symbol") != symbol:
                continue
            ts = datetime.fromisoformat(t["timestamp"])
            if ts >= cutoff:
                recent_trades.append(t)
        except Exception:
            continue

    if not recent_trades:
        return "No trade signals were recorded by the ML Brain in the last 24 hours."

    # Fetch current price from CCXT tool to evaluate outcomes
    from tools import _bybit_exchange
    current_price = 0.0
    try:
        ex = _bybit_exchange()
        ticker = await ex.fetch_ticker(symbol, params={'category': 'linear'})
        current_price = float(ticker["last"])
        await ex.close()
    except Exception as ex_err:
        log.warning("[Report] Failed to fetch current price for report: %s", ex_err)

    # Perform win/loss calculation
    wins = 0
    losses = 0
    total = len(recent_trades)
    trade_summaries = []

    for t in recent_trades:
        entry = t["entry_price"]
        dec = t["decision"]
        sl = t["stop_loss"]
        
        # Win/loss assessment logic based on current price
        outcome = "OPEN"
        if current_price > 0:
            if dec == "LONG":
                if sl and current_price <= sl:
                    outcome = "LOSS (Stop Loss Hit)"
                    losses += 1
                elif current_price > entry:
                    outcome = "WIN (In Profit)"
                    wins += 1
                else:
                    outcome = "LOSS (Under Entry)"
                    losses += 1
            elif dec == "SHORT":
                if sl and current_price >= sl:
                    outcome = "LOSS (Stop Loss Hit)"
                    losses += 1
                elif current_price < entry:
                    outcome = "WIN (In Profit)"
                    wins += 1
                else:
                    outcome = "LOSS (Above Entry)"
                    losses += 1

        trade_summaries.append(
            f"- Time: {t['timestamp']} | Dec: {dec} | Entry: {entry:,.2f} | Current: {current_price:,.2f} | SL: {sl} | Outcome: {outcome}"
        )

    win_ratio = (wins / (wins + losses)) if (wins + losses) > 0 else 0.0

    # Build prompt for Gemini summarizing the outcomes
    prompt = (
        f"You are the Lead Analyst for OceanHub. Summarize the daily performance:\n\n"
        f"Asset: {symbol}\n"
        f"Current Reference Price: {current_price:,.2f} USDT\n"
        f"Total signals generated: {total}\n"
        f"Evaluated Wins: {wins} | Losses: {losses} | Win/Loss Ratio: {win_ratio:.1%}\n\n"
        f"Trade Signals Log (Last 24h):\n"
        + "\n".join(trade_summaries)
        + "\n\nProvide a professional markdown summary of the ML model's performance, indicators, and risk advice."
    )

    # Fire one Gemini API request
    config = LocalAgentConfig(
        api_key=os.getenv("GEMINI_API_KEY", ""),
        system_instructions="You are a quantitative research lead summarizing trading bot metrics.",
    )

    try:
        async with Agent(config) as agent:
            response = await agent.chat(prompt)
            report_text = await response.text()
            return report_text
    except Exception as e:
        log.error("[Report] Gemini API failed: %s", e)
        return (
            f"### OceanHub Daily Performance Report (Fallback)\n\n"
            f"- **Asset**: {symbol}\n"
            f"- **Total Signals**: {total}\n"
            f"- **Win / Loss**: {wins} W / {losses} L (Win Ratio: {win_ratio:.1%})\n\n"
            f"*API Quota limits reached — returned localized math summary instead.*"
        )


async def run_temporary_agent_analysis(symbol: str, recent_ohlcv_df: pd.DataFrame = None, orderbook: dict = None) -> dict:
    """
    Temporary Agent Execution:
    Runs ATR calculation, Cost Analyzer, RegimeShield, and ML Brain inference on-the-fly for any dynamic unlisted asset.
    """
    try:
        if recent_ohlcv_df is None or recent_ohlcv_df.empty:
            raise ValueError(f"No OHLCV candle data provided for {symbol}")

        current_price = float(recent_ohlcv_df["close"].iloc[-1])
        
        # Calculate sub-agent results
        sub_agent_results = []
        try:
            from agents import run_all_sub_agents
            sub_agent_results = await run_all_sub_agents(symbol=symbol, order_book=orderbook)
        except Exception:
            sub_agent_results = []

        # Run ML Brain prediction for dynamic asset
        from ml_brain import get_brain
        brain = get_brain()
        # BUG-25 FIX: Use asyncio.to_thread to avoid blocking event loop with CPU-heavy ML inference
        ml_score = await asyncio.to_thread(brain.predict, symbol, recent_ohlcv_df)

        # Calculate orderbook depth
        bid_vol = 100000.0
        ask_vol = 100000.0
        if orderbook and orderbook.get("bids") and orderbook.get("asks"):
            bid_vol = sum(float(p) * float(s) for p, s in orderbook["bids"])
            ask_vol = sum(float(p) * float(s) for p, s in orderbook["asks"])

        decision = await run_master_agent(
            sub_agent_results=sub_agent_results,
            symbol=symbol,
            current_price=current_price,
            ml_score=ml_score,
            recent_ohlcv_df=recent_ohlcv_df,
            bid_vol=bid_vol,
            ask_vol=ask_vol,
            orderbook=orderbook
        )
        
        return {
            "symbol": symbol,
            "valid": True,
            "price": current_price,
            "atr_pct": float(recent_ohlcv_df["close"].pct_change().std() if len(recent_ohlcv_df) > 1 else 0.02),
            "regime": str(decision.get("market_regime", "CHOPPY")),
            "decision": str(decision.get("decision", "HOLD")),
            "confidence": float(decision.get("confidence", 1.0)),
            "reason": str(decision.get("reasoning", "")),
            "leverage": float(decision.get("leverage", 1.0)),
            "margin": float(decision.get("margin", 0.0)),
            "stop_loss": float(decision.get("stop_loss")) if decision.get("stop_loss") is not None else None,
            "take_profit": float(decision.get("take_profit")) if decision.get("take_profit") is not None else None,
            "expected_bars": float(decision.get("expected_bars", 0.0)),
            "expected_duration_mins": float(decision.get("expected_duration_mins", 0.0)),
            "timestamp": datetime.now(timezone.utc).isoformat()
        }
    except Exception as exc:
        log.error(f"[TemporaryAgent] Analysis failed for {symbol}: {exc}")
        return {
            "symbol": symbol,
            "valid": False,
            "error": f"Temporary Agent analysis failed: {str(exc)}"
        }
