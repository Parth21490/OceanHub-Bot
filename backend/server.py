"""
OceanHub — WebSocket Server & Main Orchestrator (Multi-Asset Tabbed System)
──────────────────────────────────────────────────────────────────────────────
Runs a WebSocket server on localhost:8080 and orchestrates the trading
analysis pipeline concurrently for BTC/USDT, ETH/USDT, SOL/USDT, and PEPE/USDT.
"""

import signal
from aiohttp import web
import math
from pathlib import Path
import numpy as np
import pandas as pd
import threading
import os
import json
import time
import asyncio
import logging
from datetime import datetime, timezone

import websockets
from dotenv import load_dotenv

from agents import run_all_sub_agents
from master_agent import run_master_agent, generate_daily_report
from tools import fetch_ohlcv

# Import Phase 0 Dry Run configuration
try:
    from config import DRY_RUN_MODE
except ImportError:
    DRY_RUN_MODE = True

# ── Config ──────────────────────────────────────────────────────────────
load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"))

# 0.0.0.0 for Docker/Railway; override with 127.0.0.1 for local-only
WS_HOST = os.getenv("WS_HOST", "0.0.0.0")
WS_PORT = int(os.getenv("PORT", os.getenv("WS_PORT", "8000")))
TIMEFRAME = os.getenv("TIMEFRAME", "1h")
CANDLE_LIMIT = int(os.getenv("CANDLE_LIMIT", "200"))
CYCLE_INTERVAL = int(os.getenv("CYCLE_INTERVAL", "60"))

# ── Logging ─────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)


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


log = SafeLogWrapper(logging.getLogger("oceanhub"))

# ── Global state ────────────────────────────────────────────────────────

SYMBOLS = [
    'BTC/USDT',
    'ETH/USDT',
    'SOL/USDT',
    'BNB/USDT',
    'HYPE/USDT',
    'XRP/USDT',
    'ADA/USDT',
    'DOGE/USDT']
CLIENTS: set[websockets.ServerConnection] = set()
EXECUTE_MODE = False  # toggled by frontend
HISTORY_CACHE = {sym: {} for sym in SYMBOLS}     # nested cache
LAST_ORDERBOOKS = {sym: None for sym in SYMBOLS}  # order book per symbol
# recent 1h OHLCV DataFrame per symbol
RECENT_OHLCV_DFS = {sym: None for sym in SYMBOLS}
LATEST_DECISIONS = {sym: {
    "decision": "HOLD",
    "confidence": 1.0,
    "leverage": 1,
    "stop_loss": None,
    "p_up": 0.33,
    "p_down": 0.33,
    "p_range": 0.33
} for sym in SYMBOLS}

AB_TEST_METRICS = {sym: {"A_hits": 0, "B_hits": 0, "total": 0}
                   for sym in SYMBOLS}


class AssetRunner:
    def __init__(self, symbol: str):
        self.symbol = symbol
        self.active_stream_task = None
        self.active_analysis_task = None

    async def start(self):
        log.info("Starting isolated asset runner tasks for %s", self.symbol)
        self.active_stream_task = asyncio.create_task(
            stream_live_ohlcv_for_symbol(self.symbol))
        self.active_analysis_task = asyncio.create_task(
            analysis_loop_for_symbol(self.symbol))

    async def stop(self):
        log.info("Stopping isolated asset runner tasks for %s", self.symbol)
        if self.active_stream_task and not self.active_stream_task.done():
            self.active_stream_task.cancel()
            try:
                await self.active_stream_task
            except asyncio.CancelledError:
                pass
        if self.active_analysis_task and not self.active_analysis_task.done():
            self.active_analysis_task.cancel()
            try:
                await self.active_analysis_task
            except asyncio.CancelledError:
                pass


ACTIVE_RUNNERS = {}  # symbol -> AssetRunner instance
CURRENT_SYMBOL = "ADA/USDT"
LAST_PRICE_UPDATE_TIME = 0.0

# ── OHLCV cache — populated ONLY by the master background loop ──────────
# Keyed by symbol → pane_id → list of candle records
# WebSocket connections read from here instead of calling fetch_ohlcv directly.
LATEST_OHLCV_CACHE: dict = {sym: {} for sym in SYMBOLS}


# ── Virtual Paper Trading Wallet State & Async Lock ──────────────────────
VIRTUAL_WALLET = 40.00
ACTIVE_TRADES = {}  # { symbol: { "direction": "LONG"/"SHORT", ... } }
WALLET_FILE = Path(__file__).parent / "wallet_state.json"
STATE_FILE = Path(__file__).parent / "bot_state.json"
wallet_lock = asyncio.Lock()


async def allocate_margin(symbol: str, margin_amount: float) -> bool:
    """Safely allocates margin from virtual wallet balance under an asyncio Lock to prevent balance over-allocation race conditions."""
    if margin_amount <= 0 or math.isnan(margin_amount) or math.isinf(margin_amount):
        log.warning(f"[{symbol}]   [REJECTED] INVALID_MARGIN_AMOUNT (${margin_amount})")
        return False

    async with wallet_lock:
        global VIRTUAL_WALLET
        if VIRTUAL_WALLET >= margin_amount:
            VIRTUAL_WALLET -= margin_amount
            await asyncio.to_thread(save_wallet_state)
            return True
        log.warning(f"[{symbol}]   [REJECTED] BALANCE_OVERALLOCATED (Available: ${VIRTUAL_WALLET:.2f} < Required: ${margin_amount:.2f})")
        return False


def load_wallet_state():
    global VIRTUAL_WALLET, ACTIVE_TRADES
    target = WALLET_FILE if WALLET_FILE.exists() else (STATE_FILE if STATE_FILE.exists() else None)
    if target:
        try:
            with open(target, "r", encoding="utf-8") as f:
                st = json.load(f)
                if "wallet_balance" in st and st["wallet_balance"] is not None:
                    VIRTUAL_WALLET = float(st["wallet_balance"])
                if "active_trades" in st and isinstance(st["active_trades"], dict):
                    ACTIVE_TRADES.update(st["active_trades"])
                elif "positions" in st and isinstance(st["positions"], dict):
                    ACTIVE_TRADES.update(st["positions"])
                if "cooldowns" in st and isinstance(st["cooldowns"], dict):
                    from master_agent import TRADE_COOLDOWNS
                    TRADE_COOLDOWNS.update(st["cooldowns"])
        except Exception as st_err:
            log.error("Failed to load wallet state from %s: %s", target, st_err)


def save_wallet_state():
    try:
        from master_agent import TRADE_COOLDOWNS, _json_default
        data = {
            "wallet_balance": VIRTUAL_WALLET,
            "active_trades": ACTIVE_TRADES,
            "positions": ACTIVE_TRADES,
            "cooldowns": TRADE_COOLDOWNS
        }
        with open(WALLET_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, default=_json_default, indent=2)
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, default=_json_default, indent=2)
    except Exception as e:
        log.error("Failed to save wallet/bot state: %s", e)


# Initial load on import
load_wallet_state()
if not WALLET_FILE.exists() or not STATE_FILE.exists():
    save_wallet_state()


# ── Global Background Position Guardian ─────────────────────────────────

async def global_position_guardian() -> None:
    """
    BUG FIX: Guardian that monitors ALL ACTIVE_TRADES every 10 seconds regardless of
    which symbol is currently being streamed. Without this, a position opened for ETH
    would never exit if the user switches to view XRP, since the live stream SL/TP
    monitor only runs for the currently active streaming symbol.
    """
    log.info(
        "[Guardian] Global position guardian started. Checking ALL active trades every 10s.")
    from tools import round_price_prec
    while True:
        try:
            await asyncio.sleep(10)
            if not ACTIVE_TRADES:
                continue

            symbols_to_close = []

            for sym, trade in list(ACTIVE_TRADES.items()):
                try:
                    direction = trade.get("direction")
                    entry = float(trade.get("entry_price", 0.0))
                    leverage = float(trade.get("leverage", 1.0))
                    sl = trade.get("stop_loss")
                    tp = trade.get("take_profit")
                    margin = float(trade.get("margin", 2.00))

                    # Resolve latest price from cache (check both HISTORY_CACHE
                    # and LATEST_OHLCV_CACHE)
                    current_price = entry  # fallback to entry price
                    # First try LATEST_OHLCV_CACHE (live streamed price for
                    # active symbol)
                    if sym in LATEST_OHLCV_CACHE and LATEST_OHLCV_CACHE[sym]:
                        for tf in ["1m", "3m", "5m"]:
                            candles = LATEST_OHLCV_CACHE[sym].get(tf, [])
                            if candles:
                                current_price = float(candles[-1]["close"])
                                break
                    # Fallback: HISTORY_CACHE (preloaded historical data —
                    # always available)
                    if current_price == entry and sym in HISTORY_CACHE and HISTORY_CACHE[sym]:
                        for tf in ["1m", "3m", "5m", "1h"]:
                            candles = HISTORY_CACHE[sym].get(tf, [])
                            if candles:
                                current_price = float(candles[-1]["close"])
                                break

                    if current_price <= 0 or entry <= 0:
                        continue

                    if direction == "LONG":
                        pnl_raw = (current_price - entry) / entry
                    else:
                        pnl_raw = (entry - current_price) / entry

                    pnl_usd = margin * leverage * pnl_raw
                    pnl_pct = pnl_raw * 100 * leverage

                    hit = False
                    outcome = "ACTIVE"

                    # SL check
                    if sl is not None:
                        if direction == "LONG" and current_price <= float(sl):
                            hit = True
                            outcome = "TRAILING_STOP_HIT" if float(
                                sl) >= entry else "SL_HIT"
                        elif direction == "SHORT" and current_price >= float(sl):
                            hit = True
                            outcome = "TRAILING_STOP_HIT" if float(
                                sl) <= entry else "SL_HIT"

                    # TP check
                    if not hit and tp is not None:
                        if direction == "LONG" and current_price >= float(tp):
                            hit = True
                            outcome = "TP_HIT"
                        elif direction == "SHORT" and current_price <= float(tp):
                            hit = True
                            outcome = "TP_HIT"

                    # Liquidation check (full margin loss)
                    if not hit and pnl_pct <= -100.0:
                        hit = True
                        outcome = "LIQUIDATED"
                        pnl_usd = -margin
                        pnl_pct = -100.0

                    # Trailing SL update: +1.5% → Break-Even, +2.5% → 1.5x ATR
                    # trail
                    if not hit:
                        FEES_PCT = 0.0011
                        updated_sl = sl
                        tsl_reason = None
                        tp_dist = abs(float(tp) - entry) if tp is not None else 0
                        curr_dist = abs(current_price - entry)
                        is_in_favor = (direction == "LONG" and current_price > entry) or (direction == "SHORT" and current_price < entry)
                        
                        if is_in_favor and tp_dist > 0 and (curr_dist / tp_dist) >= 0.80:
                            if direction == "LONG":
                                be_sl = round_price_prec(entry * (1.0 + FEES_PCT), sym)
                                if updated_sl is None or be_sl > float(updated_sl):
                                    updated_sl = be_sl
                                    tsl_reason = "BREAKEVEN_SAVED: Price reversed after reaching 80% to target."
                            elif direction == "SHORT":
                                be_sl = round_price_prec(entry * (1.0 - FEES_PCT), sym)
                                if updated_sl is None or be_sl < float(updated_sl):
                                    updated_sl = be_sl
                                    tsl_reason = "BREAKEVEN_SAVED: Price reversed after reaching 80% to target."
                        elif pnl_raw >= 0.025:
                            # Use 1% of price as fallback ATR
                            atr_dist = current_price * 0.01
                            try:
                                if sym in HISTORY_CACHE and "1h" in HISTORY_CACHE[sym]:
                                    candles = HISTORY_CACHE[sym]["1h"]
                                    if len(candles) >= 15:
                                        from master_agent import calculate_atr
                                        import pandas as pd
                                        df = pd.DataFrame(candles)
                                        df['close'] = df['close'].astype(float)
                                        df['high'] = df['high'].astype(float)
                                        df['low'] = df['low'].astype(float)
                                        atr_data = calculate_atr(df, period=14)
                                        atr_val = atr_data.get('raw', 0)
                                        if atr_val > 0:
                                            atr_dist = 1.5 * atr_val
                            except Exception as e:
                                log.warning(
                                    "[Guardian] Failed to calc ATR for %s: %s", sym, e)

                            if direction == "LONG":
                                candidate_sl = current_price - atr_dist
                                if updated_sl is None or candidate_sl > float(
                                        updated_sl):
                                    updated_sl = round_price_prec(
                                        candidate_sl, sym)
                                    tsl_reason = "TRAILING_STOP_HIT"
                            elif direction == "SHORT":
                                candidate_sl = current_price + atr_dist
                                if updated_sl is None or candidate_sl < float(
                                        updated_sl):
                                    updated_sl = round_price_prec(
                                        candidate_sl, sym)
                                    tsl_reason = "TRAILING_STOP_HIT"

                        if tsl_reason and updated_sl != sl:
                            ACTIVE_TRADES[sym]["stop_loss"] = updated_sl
                            log.info(
                                "[Guardian] [%s] TSL updated → %s (reason: %s, price: %.4f)",
                                sym,
                                updated_sl,
                                tsl_reason,
                                current_price)
                            await asyncio.to_thread(save_wallet_state)

                    if hit:
                        symbols_to_close.append(
                            (sym, trade.copy(), current_price, pnl_usd, pnl_pct, outcome))

                except Exception as inner_err:
                    log.warning(
                        "[Guardian] Error checking position for %s: %s",
                        sym,
                        inner_err)

            # Close all hit positions atomically
            global VIRTUAL_WALLET
            for sym, trade, close_price, pnl_usd, pnl_pct, outcome in symbols_to_close:
                if sym in ACTIVE_TRADES:
                    margin_returned = float(trade.get("margin", 2.00))
                    VIRTUAL_WALLET += margin_returned + pnl_usd
                    del ACTIVE_TRADES[sym]
                    try:
                        import time
                        from master_agent import TRADE_COOLDOWNS
                        TRADE_COOLDOWNS[sym] = time.time()
                    except Exception:
                        pass
                    await asyncio.to_thread(save_wallet_state)
                    _log_close_trade(
                        sym, trade, close_price, pnl_usd, pnl_pct, outcome)

                    mode_str = "⚡ LIVE" if EXECUTE_MODE else "⏸ SIMULATION"
                    await stream_thought(
                        f"[{sym}] [{mode_str} Guardian] Closed position! Status: {outcome} | "
                        f"Price: {close_price} USDT | PnL: {pnl_usd:+.2f} USD ({pnl_pct:+.2f}%) | "
                        f"New Balance: ${VIRTUAL_WALLET:.2f}"
                    )
                    await broadcast_unified_state()
                    log.info(
                        "[Guardian] [%s] Position closed: %s | PnL: %+.2f USD | Balance: $%.2f",
                        sym,
                        outcome,
                        pnl_usd,
                        VIRTUAL_WALLET)

        except asyncio.CancelledError:
            log.info("[Guardian] Global position guardian cancelled.")
            break
        except Exception as guardian_err:
            log.error("[Guardian] Unexpected error: %s", guardian_err)


def _log_open_trade(
        symbol: str,
        direction: str,
        price: float,
        confidence: float,
        leverage: int,
        stop_loss: float | None,
        take_profit: float | None,
        margin: float,
        kelly_conf: float,
        expected_duration_mins: float = 0.0):
    try:
        from master_agent import HISTORY_FILE
        import json
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
            "decision": direction,
            "entry_price": price,
            "confidence": confidence,
            "leverage": leverage,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "margin": margin,
            "kelly_conf": kelly_conf,
            "status": "ACTIVE",
            "expected_duration_mins": expected_duration_mins
        })
        history = history[-1000:]
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(history, f, indent=2)

        # TRIGGER DISCORD NOTIFICATION
        try:
            from discord_notifier import send_discord_embed
            import asyncio
            # Check if scalp from ACTIVE_TRADES
            is_scalp = False
            if symbol in ACTIVE_TRADES and ACTIVE_TRADES[symbol].get(
                    "is_scalp", False):
                is_scalp = True

            color = 0x800080 if is_scalp else (
                0x00FF00 if direction == "LONG" else 0xFF0000)
            mode = f"COUNTER-TREND SCALP {direction}" if is_scalp else f"TREND {direction}"

            embed = {
                "title": f"🚀 TRADE EXECUTED | {symbol}",
                "color": color,
                "fields": [
                    {"name": "Mode / Action", "value": mode, "inline": True},
                    {"name": "Entry Price", "value": f"${price:.5f}", "inline": True},
                    {"name": "ML Confidence", "value": f"{(confidence * 100):.1f}%", "inline": True},
                    {"name": "Leverage & Margin", "value": f"{leverage}x (${margin:.2f})", "inline": True},
                    {"name": "Stop Loss", "value": f"${stop_loss:.5f}" if stop_loss else "None", "inline": True},
                    {"name": "Take Profit", "value": f"${take_profit:.5f}" if take_profit else "None", "inline": True}
                ]
            }
            asyncio.create_task(send_discord_embed(embed))
        except Exception as e:
            log.error("Failed to trigger Discord open notification: %s", e)

    except Exception as e:
        log.error("Failed to log open trade: %s", e)


def _log_close_trade(
        symbol: str,
        trade: dict,
        close_price: float,
        pnl_usd: float,
        pnl_pct: float,
        outcome: str):
    try:
        from master_agent import HISTORY_FILE
        import json
        history = []
        if HISTORY_FILE.exists():
            try:
                with open(HISTORY_FILE, "r", encoding="utf-8-sig") as f:
                    history = json.load(f)
            except Exception:
                history = []

        found = False
        for item in reversed(history):
            if item.get("symbol") == symbol and item.get("status") == "ACTIVE":
                item["status"] = outcome
                item["close_price"] = close_price
                item["pnl_usd"] = pnl_usd
                item["pnl_pct"] = pnl_pct
                item["close_timestamp"] = datetime.now(timezone.utc).isoformat()
                found = True
                break

        if not found:
            history.append({
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "symbol": symbol,
                "decision": trade.get("direction", "HOLD"),
                "entry_price": trade.get("entry_price", close_price),
                "close_price": close_price,
                "leverage": trade.get("leverage", 1),
                "stop_loss": trade.get("stop_loss"),
                "take_profit": trade.get("take_profit"),
                "margin": trade.get("margin", 0.0),
                "pnl_usd": pnl_usd,
                "pnl_pct": pnl_pct,
                "status": outcome
            })

        history = history[-1000:]
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(history, f, indent=2)

        # TRIGGER DISCORD NOTIFICATION
        try:
            from discord_notifier import send_discord_embed
            import asyncio
            color = 0xFFD700 if pnl_usd > 0 else 0x808080
            embed = {
                "title": f"🏁 POSITION CLOSED | {symbol}",
                "color": color,
                "fields": [
                    {"name": "Exit Reason", "value": outcome, "inline": True},
                    {"name": "Realized PnL %", "value": f"{pnl_pct:+.2f}%", "inline": True},
                    {"name": "Realized ROI / PnL USD", "value": f"${pnl_usd:+.2f}", "inline": True},
                    {"name": "Exit Price", "value": f"${close_price:.5f}", "inline": True}
                ]
            }
            asyncio.create_task(send_discord_embed(embed))
        except Exception as e:
            log.error("Failed to trigger Discord close notification: %s", e)

    except Exception as e:
        log.error("Failed to log closed trade: %s", e)


# ── WebSocket helpers ───────────────────────────────────────────────────


class _NaNSafeEncoder(json.JSONEncoder):
    """Replaces NaN / Infinity with null so browsers can parse the payload."""

    def iterencode(self, o, _one_shot=False):
        # Pre-process to replace NaN/Inf before encoding
        return super().iterencode(self._sanitize(o), _one_shot)

    def _sanitize(self, obj):
        from master_agent import Decision
        if isinstance(obj, Decision):
            return {
                "decision": obj.action,
                "reasoning": obj.reason,
                "confidence": 0.0,
                "leverage": 0,
                "stop_loss": None,
                "take_profit": None,
                "margin": 0.0,
                "risk_level": "LOW",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "ml_brain": {},
                "market_regime": "UNKNOWN"
            }
        if isinstance(obj, float):
            if math.isnan(obj) or math.isinf(obj):
                return None
            return obj
        if isinstance(obj, dict):
            return {k: self._sanitize(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [self._sanitize(v) for v in obj]
        return obj


def safe_json(obj) -> str:
    """Serialize obj to JSON, replacing NaN/Inf with null."""
    return json.dumps(obj, cls=_NaNSafeEncoder)


async def broadcast(message: dict) -> None:
    """Send a JSON message to all connected clients."""
    if not CLIENTS:
        return
    payload = safe_json(message)
    await asyncio.gather(
        *[client.send(payload) for client in CLIENTS],
        return_exceptions=True,
    )


LOG_FILE_PATH = "/app/logs/master_core.log"
LAST_LOG_CLEANUP_TIME = 0.0


def cleanup_old_logs() -> None:
    global LAST_LOG_CLEANUP_TIME
    now_ts = time.time()
    if now_ts - LAST_LOG_CLEANUP_TIME < 3600:
        return
    LAST_LOG_CLEANUP_TIME = now_ts

    if not os.path.exists(LOG_FILE_PATH):
        return

    try:
        from datetime import timedelta
        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
        kept_entries = []
        with open(LOG_FILE_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    entry_time = datetime.fromisoformat(entry["timestamp"])
                    if entry_time >= cutoff:
                        kept_entries.append(line)
                except Exception:
                    pass
        with open(LOG_FILE_PATH, "w", encoding="utf-8") as f:
            for line in kept_entries:
                f.write(line + "\n")
    except Exception as cleanup_err:
        log.error("Failed to clean up master_core.log: %s", cleanup_err)


async def stream_thought(text: str) -> None:
    """Broadcast a single AI thought line to all connected clients and save to persistent logs."""

    # Save log to persistent file
    try:
        os.makedirs(os.path.dirname(LOG_FILE_PATH), exist_ok=True)
        log_entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "text": text
        }
        with open(LOG_FILE_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(log_entry) + "\n")
    except Exception as write_err:
        log.error("Failed to write to master_core.log: %s", write_err)

    # Perform periodic rotation check
    try:
        cleanup_old_logs()
    except Exception as rotate_err:
        log.error("Error running log cleanup check: %s", rotate_err)

    await broadcast({"type": "ai_thought", "text": text})


async def get_unified_state() -> dict:
    """Constructs the unified state dictionary for all assets, including wallet statistics."""
    ledger = []

    def format_pnl_str(pnl_usd: float, pnl_pct: float) -> str:
        sign = "+" if pnl_usd >= 0 else ""
        return f"{sign}${pnl_usd:.2f} ({sign}{pnl_pct:.2f}%)"

    active_syms_in_ledger = set()

    try:
        from master_agent import HISTORY_FILE
        import json
        if HISTORY_FILE.exists():
            with open(HISTORY_FILE, "r", encoding="utf-8-sig") as f:
                raw_trades = json.load(f)

            for t in reversed(raw_trades):
                sym = t.get("symbol")
                dec = t.get("decision")
                entry = t.get("entry_price", 0.0)
                lev = t.get("leverage", 1)
                sl = t.get("stop_loss")
                tp = t.get("take_profit")
                timestamp = t.get("timestamp", "")
                margin = t.get("margin", 2.00)
                kelly_conf = t.get("kelly_conf", 0.0)

                exp_dur_mins = t.get("expected_duration_mins", 0)

                # If currently active, merge with live active state
                if sym in ACTIVE_TRADES and t.get("status") == "ACTIVE":
                    active_t = ACTIVE_TRADES[sym]
                    margin = active_t.get("margin", margin)
                    kelly_conf = active_t.get("kelly_conf", kelly_conf)
                    exp_dur_mins = active_t.get("expected_duration_mins", exp_dur_mins)
                    active_syms_in_ledger.add(sym)

                try:
                    from datetime import timedelta
                    dt = datetime.fromisoformat(timestamp)
                    ist_tz = timezone(timedelta(hours=5, minutes=30))
                    dt_ist = dt.astimezone(ist_tz)
                    time_str = dt_ist.strftime("%H:%M:%S")
                except Exception:
                    time_str = timestamp[:19]

                curr_price = entry
                if sym in HISTORY_CACHE and HISTORY_CACHE[sym]:
                    timeframe_keys = list(HISTORY_CACHE[sym].keys())
                    if timeframe_keys:
                        candles = HISTORY_CACHE[sym][timeframe_keys[0]]
                        if candles:
                            curr_price = candles[-1]["close"]

                status = t.get("status", "CLOSED")
                pnl_usd = t.get("pnl_usd")
                pnl_pct = t.get("pnl_pct")

                if pnl_usd is not None and pnl_pct is not None:
                    pnl_str = format_pnl_str(pnl_usd, pnl_pct)
                elif t.get("status") == "ACTIVE":
                    # Active position
                    status = "ACTIVE"
                    if dec == "LONG":
                        pnl_raw = (curr_price - entry) / \
                            entry if entry > 0 else 0.0
                    else:
                        pnl_raw = (entry - curr_price) / \
                            entry if entry > 0 else 0.0

                    pnl_pct = pnl_raw * 100 * lev
                    pnl_usd = margin * lev * pnl_raw
                    pnl_str = format_pnl_str(pnl_usd, pnl_pct)
                else:
                    # Pre-wallet historical trade fallback
                    pnl_str = "$0.00 (0.00%)"
                    status = "CLOSED"

                ledger.append({
                    "time": time_str,
                    "pair": sym,
                    "direction": dec,
                    "entry_price": entry,
                    "current_price": curr_price,
                    "pnl": pnl_str,
                    "status": status,
                    "margin": margin,
                    "kelly_conf": kelly_conf,
                    "expected_duration_mins": exp_dur_mins
                })

        # CRITICAL FIX: Ensure all active positions in ACTIVE_TRADES are included in ledger
        for sym, active_t in ACTIVE_TRADES.items():
            if sym not in active_syms_in_ledger:
                dec = active_t.get("direction", "LONG")
                entry = active_t.get("entry_price", 0.0)
                lev = active_t.get("leverage", 1)
                margin = active_t.get("margin", 2.00)
                kelly_conf = active_t.get("kelly_conf", 0.0)
                exp_dur_mins = active_t.get("expected_duration_mins", 0)
                timestamp = active_t.get("timestamp", datetime.now(timezone.utc).isoformat())

                try:
                    from datetime import timedelta
                    dt = datetime.fromisoformat(timestamp)
                    ist_tz = timezone(timedelta(hours=5, minutes=30))
                    dt_ist = dt.astimezone(ist_tz)
                    time_str = dt_ist.strftime("%H:%M:%S")
                except Exception:
                    time_str = timestamp[:19]

                curr_price = entry
                if sym in HISTORY_CACHE and HISTORY_CACHE[sym]:
                    timeframe_keys = list(HISTORY_CACHE[sym].keys())
                    if timeframe_keys:
                        candles = HISTORY_CACHE[sym][timeframe_keys[0]]
                        if candles:
                            curr_price = candles[-1]["close"]

                if dec == "LONG":
                    pnl_raw = (curr_price - entry) / entry if entry > 0 else 0.0
                else:
                    pnl_raw = (entry - curr_price) / entry if entry > 0 else 0.0
                pnl_pct = pnl_raw * 100 * lev
                pnl_usd = margin * lev * pnl_raw
                pnl_str = format_pnl_str(pnl_usd, pnl_pct)

                ledger.insert(0, {
                    "time": time_str,
                    "pair": sym,
                    "direction": dec,
                    "entry_price": entry,
                    "current_price": curr_price,
                    "pnl": pnl_str,
                    "status": "ACTIVE",
                    "margin": margin,
                    "kelly_conf": kelly_conf,
                    "expected_duration_mins": exp_dur_mins
                })

    except Exception as e:
        log.error("Failed to construct trade ledger: %s", e)

    assets_state = {}
    for sym in SYMBOLS:
        curr_price = 0.0
        if sym in HISTORY_CACHE and HISTORY_CACHE[sym]:
            timeframe_keys = list(HISTORY_CACHE[sym].keys())
            if timeframe_keys:
                candles = HISTORY_CACHE[sym][timeframe_keys[0]]
                if candles:
                    curr_price = candles[-1]["close"]

        metrics = LATEST_DECISIONS.get(sym, {
            "decision": "HOLD",
            "confidence": 1.0,
            "leverage": 1,
            "stop_loss": None,
            "p_up": 0.33,
            "p_down": 0.33,
            "p_range": 0.33
        })
        metrics = dict(metrics)
        metrics["price"] = curr_price
        assets_state[sym] = metrics

    # Calculate wallet metrics
    margin_in_use = sum(t.get("margin", 2.00) for t in ACTIVE_TRADES.values())
    unrealized_pnl = 0.0
    for sym, t in ACTIVE_TRADES.items():
        curr_price = t.get("entry_price")
        if sym in HISTORY_CACHE and HISTORY_CACHE[sym]:
            timeframe_keys = list(HISTORY_CACHE[sym].keys())
            if timeframe_keys:
                candles = HISTORY_CACHE[sym][timeframe_keys[0]]
                if candles:
                    curr_price = candles[-1]["close"]

        direction = t["direction"]
        entry = t["entry_price"]
        lev = t["leverage"]
        margin = t.get("margin", 2.00)

        if direction == "LONG":
            pnl_raw = (curr_price - entry) / entry if entry > 0 else 0.0
        else:
            pnl_raw = (entry - curr_price) / entry if entry > 0 else 0.0

        unrealized_pnl += margin * lev * pnl_raw

    # Total Balance (Equity) = Available Cash (VIRTUAL_WALLET) + Margin in Use + Active Unrealized PnL
    total_balance = VIRTUAL_WALLET + margin_in_use + unrealized_pnl

    return {
        "assets": assets_state,
        "trade_ledger": ledger[:50],
        "wallet": {
            "balance": round(total_balance, 2),
            "cash": round(VIRTUAL_WALLET, 2),
            "margin_in_use": round(margin_in_use, 2),
            "unrealized_pnl": round(unrealized_pnl, 2)
        }
    }


async def broadcast_unified_state() -> None:
    """Broadcasts unified state containing all asset metrics & ledger history to clients."""
    state = await get_unified_state()
    await broadcast({
        "type": "unified_state",
        "data": state
    })


async def update_sub_agent_ui(
        symbol: str,
        order_book=None,
        regime: str = "CHOPPY") -> None:
    """Runs decorative sub-agents in the background for UI display without blocking execution."""
    try:
        from agents import run_all_sub_agents
        sub_results = await run_all_sub_agents(symbol=symbol, order_book=order_book, regime=regime)
        await broadcast({"type": "sub_results", "symbol": symbol, "data": sub_results})
    except Exception as exc:
        log.debug("Background sub-agents UI update for %s: %s", symbol, exc)


# ── Static File Server Setup ───────────────────────────────────────────
STATIC_DIR = os.path.join(os.path.dirname(__file__), "dist")
if not os.path.exists(STATIC_DIR):
    STATIC_DIR = os.path.join(os.path.dirname(__file__), "..", "out", "renderer")

async def http_process_request(connection, request):
    headers = getattr(request, 'headers', {})
    if headers.get('Upgrade', '').lower() == 'websocket':
        return None

    path = getattr(request, 'path', '/')
    if path == '/health':
        body = json.dumps({"status": "healthy", "service": "OceanHub Bot"}).encode("utf-8")
        return (200, [("Content-Type", "application/json"), ("Content-Length", str(len(body)))], body)

    # Resolve static file path
    req_path = path.lstrip('/').split('?')[0]
    file_path = os.path.join(STATIC_DIR, req_path)

    # Serve static assets (JS, CSS, SVG, PNG, etc.)
    if req_path and os.path.exists(file_path) and os.path.isfile(file_path):
        mime_type = "application/octet-stream"
        if file_path.endswith('.html'):
            mime_type = "text/html; charset=utf-8"
        elif file_path.endswith('.js'):
            mime_type = "application/javascript; charset=utf-8"
        elif file_path.endswith('.css'):
            mime_type = "text/css; charset=utf-8"
        elif file_path.endswith('.svg'):
            mime_type = "image/svg+xml"
        elif file_path.endswith('.png'):
            mime_type = "image/png"
        elif file_path.endswith('.ico'):
            mime_type = "image/x-icon"

        try:
            with open(file_path, "rb") as f:
                content = f.read()
            return (200, [("Content-Type", mime_type), ("Content-Length", str(len(content)))], content)
        except Exception as exc:
            log.error("Failed serving static asset %s: %s", req_path, exc)

    # Fallback to SPA index.html for root or client-side routing
    index_path = os.path.join(STATIC_DIR, "index.html")
    if os.path.exists(index_path) and os.path.isfile(index_path):
        try:
            with open(index_path, "rb") as f:
                content = f.read()
            return (200, [("Content-Type", "text/html; charset=utf-8"), ("Content-Length", str(len(content)))], content)
        except Exception as exc:
            log.error("Failed serving index.html: %s", exc)

    # Status card fallback if dist/ is not built
    html = f"""<!DOCTYPE html>
<html>
<head>
    <title>OceanHub AI Trading Bot</title>
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #0b0e14; color: #e2e8f0; margin: 0; padding: 20px; display: flex; flex-direction: column; align-items: center; justify-content: center; min-height: 90vh; text-align: center; }}
        .card {{ background: #151922; border: 1px solid #222938; border-radius: 16px; padding: 32px; max-width: 480px; width: 100%; box-shadow: 0 10px 25px -5px rgba(0, 0, 0, 0.5); }}
        .badge {{ background: #059669; color: white; padding: 6px 14px; border-radius: 20px; font-size: 13px; font-weight: 600; display: inline-block; margin-bottom: 16px; letter-spacing: 0.5px; }}
        h1 {{ color: #ffffff; font-size: 24px; margin: 0 0 8px 0; font-weight: 700; }}
        p {{ color: #94a3b8; font-size: 14px; margin: 0 0 24px 0; line-height: 1.5; }}
        .metrics {{ display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-bottom: 24px; }}
        .metric-box {{ background: #0b0e14; border: 1px solid #1e293b; padding: 16px; border-radius: 10px; }}
        .metric-val {{ font-size: 18px; font-weight: bold; color: #38bdf8; }}
        .metric-lbl {{ font-size: 12px; color: #64748b; margin-top: 4px; }}
        .footer {{ font-size: 12px; color: #475569; border-top: 1px solid #1e293b; padding-top: 16px; }}
    </style>
</head>
<body>
    <div class="card">
        <div class="badge">● BOT OPERATIONAL</div>
        <h1>OceanHub AI Master Engine</h1>
        <p>Neural Substrate & Real-time Trading Core are online. All WebSocket endpoints active.</p>
        <div class="metrics">
            <div class="metric-box">
                <div class="metric-val">{len(SYMBOLS)}</div>
                <div class="metric-lbl">Active Assets</div>
            </div>
            <div class="metric-box">
                <div class="metric-val">1h / 60s</div>
                <div class="metric-lbl">Analysis Cycle</div>
            </div>
        </div>
        <div class="footer">Deployed on Railway • WebSocket & HTTP Unified</div>
    </div>
</body>
</html>""".encode("utf-8")

    return (200, [("Content-Type", "text/html; charset=utf-8"), ("Content-Length", str(len(html)))], html)


# ── WebSocket connection handler ────────────────────────────────────────

async def handle_client(websocket: websockets.WebSocketServerProtocol) -> None:
    global EXECUTE_MODE

    CLIENTS.add(websocket)
    client_addr = websocket.remote_address
    log.info("Client connected: %s  (total: %d)", client_addr, len(CLIENTS))

    # Send welcome status
    await websocket.send(safe_json({
        "type": "status",
        "text": f"OceanHub multi-asset backend connected. Symbols: {SYMBOLS}",
    }))

    # Push initial unified state
    try:
        state = await get_unified_state()
        await websocket.send(safe_json({
            "type": "unified_state",
            "data": state
        }))
    except Exception as state_err:
        log.error("Failed to send initial unified state: %s", state_err)

    # Push initial log history (last 6 hours) from persistent log file
    try:
        if os.path.exists(LOG_FILE_PATH):
            from datetime import timedelta
            cutoff = datetime.now(timezone.utc) - timedelta(hours=6)
            history = []
            with open(LOG_FILE_PATH, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                        entry_time = datetime.fromisoformat(entry["timestamp"])
                        if entry_time >= cutoff:
                            history.append(entry["text"])
                    except Exception:
                        pass
            await websocket.send(safe_json({
                "type": "INIT_LOG_HISTORY",
                "data": history
            }))
            log.info(
                "[WS] Sent INIT_LOG_HISTORY (%d entries) to new client",
                len(history))
    except Exception as history_err:
        log.error("Failed to seed initial log history: %s", history_err)

    # ── Seed charts immediately on new connection from in-memory cache ──────
    async def seed_connection_history():
        symbol = CURRENT_SYMBOL
        try:
            cached = LATEST_OHLCV_CACHE.get(
                symbol) or HISTORY_CACHE.get(symbol, {})
            if cached:
                seeded_data = {k: v for k, v in cached.items() if v}
                await websocket.send(safe_json({
                    "type": "INIT_CHART_HISTORY",
                    "symbol": symbol,
                    "data": seeded_data
                }))
                log.info(
                    "[WS] Sent INIT_CHART_HISTORY to new client for %s (from cache)",
                    symbol)
            else:
                log.info("[WS] Cache empty for %s on connection", symbol)
        except Exception as e:
            log.error("Failed to seed history on new connection: %s", e)
    asyncio.create_task(seed_connection_history())

    try:
        async for raw in websocket:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue

            msg_type = msg.get("type")

            if msg_type == "ping":
                await websocket.send(safe_json({"type": "pong"}))

            elif msg_type == "get_history":
                symbol = msg.get("symbol", CURRENT_SYMBOL)
                # Serve history from in-memory cache - no Bybit API call

                async def _seed_get_history(sym=symbol):
                    try:
                        cached = LATEST_OHLCV_CACHE.get(
                            sym) or HISTORY_CACHE.get(sym, {})
                        if cached:
                            seeded_data = {
                                k: v for k, v in cached.items() if v}
                            await websocket.send(safe_json({
                                "type": "INIT_CHART_HISTORY",
                                "symbol": sym,
                                "data": seeded_data
                            }))
                            log.info(
                                "[WS] Sent INIT_CHART_HISTORY for get_history request: %s (from cache)", sym)
                        else:
                            log.warning(
                                "[WS] Cache empty for get_history request: %s", sym)
                    except Exception as e:
                        log.error(
                            "Failed to serve history from cache for %s: %s", sym, e)
                asyncio.create_task(_seed_get_history())

            elif msg_type == "SWITCH_SYMBOL":
                symbol = msg.get("symbol")
                if symbol and symbol != "HOME":
                    log.info(
                        "Received SWITCH_SYMBOL event for %s from frontend.", symbol)
                    # Stop previous and start new symbol tasks
                    await start_symbol_tasks(symbol)

                    # Serve from in-memory cache immediately — no API call on
                    # tab switch
                    async def seed_switch_history(sym=symbol):
                        try:
                            cached = LATEST_OHLCV_CACHE.get(
                                sym) or HISTORY_CACHE.get(sym, {})
                            if cached:
                                seeded_data = {
                                    k: v for k, v in cached.items() if v}
                                await websocket.send(safe_json({
                                    "type": "INIT_CHART_HISTORY",
                                    "symbol": sym,
                                    "data": seeded_data
                                }))
                                log.info(
                                    "[WS] Pushed cache candles for switch to %s", sym)
                            else:
                                log.warning(
                                    "[WS] Cache empty for SWITCH_SYMBOL: %s", sym)
                        except Exception as e:
                            log.error(
                                "Failed to serve cache on SWITCH_SYMBOL for %s: %s", sym, e)
                    asyncio.create_task(seed_switch_history())

            elif msg_type == "DYNAMIC_ASSET_SEARCH":
                raw_symbol = str(msg.get("symbol", "")).strip().upper()
                if not raw_symbol:
                    await websocket.send(safe_json({
                        "type": "DYNAMIC_ASSET_ERROR",
                        "error": "Symbol cannot be empty."
                    }))
                    continue

                if "/" not in raw_symbol:
                    if raw_symbol.endswith("USDT"):
                        raw_symbol = f"{raw_symbol[:-4]}/USDT"
                    else:
                        raw_symbol = f"{raw_symbol}/USDT"

                log.info(f"[WS] Dynamic asset search request received for: {raw_symbol}")

                async def handle_dynamic_asset_search(sym=raw_symbol):
                    try:
                        # 1. Exchange ticker validation
                        from tools import _bybit_exchange, ccxt_symbol_format, MARKET_CACHE
                        bybit_id = ccxt_symbol_format(sym)
                        exchange = _bybit_exchange()
                        
                        ticker = None
                        if MARKET_CACHE and sym in MARKET_CACHE:
                            ticker = MARKET_CACHE[sym]
                        else:
                            try:
                                ticker = await exchange.fetch_ticker(bybit_id)
                            except Exception as ex_err:
                                log.warning(f"[DynamicAsset] Ticker validation failed for {sym}: {ex_err}")
                        
                        if not ticker:
                            await websocket.send(safe_json({
                                "type": "DYNAMIC_ASSET_ERROR",
                                "symbol": sym,
                                "error": f"Asset '{sym}' not found on exchange."
                            }))
                            return

                        # 2. Fetch recent OHLCV history for dynamic analysis
                        try:
                            bybit_tf = "60"
                            ohlcv_raw = await exchange.fetch_ohlcv(bybit_id, timeframe=bybit_tf, limit=300)
                            if not ohlcv_raw:
                                raise ValueError(f"No OHLCV candles returned for {sym}")

                            df = pd.DataFrame(ohlcv_raw, columns=["time", "open", "high", "low", "close", "volume"])

                            formatted_candles = [
                                {
                                    "time": int(row[0]),
                                    "open": float(row[1]),
                                    "high": float(row[2]),
                                    "low": float(row[3]),
                                    "close": float(row[4]),
                                    "volume": float(row[5])
                                }
                                for row in ohlcv_raw
                            ]

                            if sym not in HISTORY_CACHE:
                                HISTORY_CACHE[sym] = {}
                            HISTORY_CACHE[sym]["1h"] = formatted_candles

                            if sym not in LATEST_OHLCV_CACHE:
                                LATEST_OHLCV_CACHE[sym] = {}
                            LATEST_OHLCV_CACHE[sym]["1h"] = formatted_candles

                        except Exception as fetch_err:
                            log.error(f"[DynamicAsset] OHLCV fetch failed for {sym}: {fetch_err}")
                            await websocket.send(safe_json({
                                "type": "DYNAMIC_ASSET_ERROR",
                                "symbol": sym,
                                "error": f"Failed to fetch market data for '{sym}': {str(fetch_err)}"
                            }))
                            return

                        # 3. Run Temporary Agent Analysis
                        dyn_orderbook = None
                        try:
                            ob_raw = await exchange.fetch_order_book(bybit_id, limit=20)
                            bids = [[float(b[0]), float(b[1])] for b in ob_raw.get('bids', [])]
                            asks = [[float(a[0]), float(a[1])] for a in ob_raw.get('asks', [])]
                            dyn_orderbook = {"bids": bids, "asks": asks}
                        except Exception as ob_err:
                            log.warning(f"[DynamicAsset] Orderbook fetch failed for {sym}: {ob_err}")

                        from master_agent import run_temporary_agent_analysis
                        result = await run_temporary_agent_analysis(sym, recent_ohlcv_df=df, orderbook=dyn_orderbook)

                        # Send result back to requesting client
                        await websocket.send(safe_json({
                            "type": "DYNAMIC_ASSET_RESULT",
                            "data": result
                        }))
                        log.info(f"[DynamicAsset] Successfully evaluated temporary agent for {sym}: Decision={result.get('decision')}")

                    except Exception as dyn_err:
                        log.error(f"[DynamicAsset] Execution error for {sym}: {dyn_err}")
                        await websocket.send(safe_json({
                            "type": "DYNAMIC_ASSET_ERROR",
                            "symbol": sym,
                            "error": f"Dynamic asset analysis failed: {str(dyn_err)}"
                        }))

                asyncio.create_task(handle_dynamic_asset_search())

            elif msg_type == "execute":
                requested_active = bool(msg.get("active", False))
                if DRY_RUN_MODE and requested_active:
                    EXECUTE_MODE = False
                    log.warning(
                        "Execute mode toggle to LIVE blocked: DRY_RUN_MODE is active.")
                    await broadcast({
                        "type": "status",
                        "text": "Execute mode → BLOCKED (DRY RUN ACTIVE)",
                    })
                else:
                    EXECUTE_MODE = requested_active
                    mode_str = "LIVE" if EXECUTE_MODE else "SIMULATION"
                    log.info("Execute mode toggled → %s", mode_str)
                    await broadcast({
                        "type": "status",
                        "text": f"Execute mode → {mode_str}",
                    })

            elif msg_type == "generate_report":
                target_sym = msg.get("symbol", "BTC/USDT")
                log.info(
                    "Manual daily report requested by client for %s.",
                    target_sym)
                await stream_thought(f"[{target_sym}] ────────────────────────────────────")
                await stream_thought(f"[{target_sym}] [Report] Generating manual performance report via Gemini...")
                try:
                    report = await generate_daily_report(symbol=target_sym)
                    for line in report.split("\n"):
                        await stream_thought(f"[{target_sym}] {line}")
                except Exception as r_err:
                    log.error("Report generation failed: %s", r_err)
                    await stream_thought(f"[{target_sym}] [Report] Generation failed: {r_err}")

    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        CLIENTS.discard(websocket)
        log.info(
            "Client disconnected: %s  (total: %d)",
            client_addr,
            len(CLIENTS))


async def dispatch_shap_rationale(symbol, df):
    try:
        from ml_brain import get_brain
        brain = get_brain()
        shap_text = await asyncio.wait_for(
            asyncio.to_thread(brain.get_shap_explanation, symbol, df),
            timeout=5.0
        )
        if symbol in LATEST_DECISIONS:
            LATEST_DECISIONS[symbol]["reasoning"] = shap_text
        await broadcast({"type": "shap_update", "symbol": symbol, "data": {"reasoning": shap_text}})
    except asyncio.TimeoutError:
        log.warning("SHAP calculation timed out for %s (5s max)", symbol)
        if symbol in LATEST_DECISIONS:
            LATEST_DECISIONS[symbol]["reasoning"] = "SHAP rationale generation timed out."
        await broadcast({"type": "shap_update", "symbol": symbol, "data": {"reasoning": "SHAP rationale generation timed out."}})
    except Exception as e:
        log.error(f"SHAP background task failed: {e}")


async def fetch_ab_test_metrics(symbol: str) -> tuple[float, float, float]:
    """Calculates/estimates funding_rate, oi_delta, and btc_correlation locally from cache to stay under Bybit rate limits."""
    funding_rate = 0.0001  # Default linear perp funding rate
    oi_delta = 0.0
    btc_corr = 0.85        # Default high positive correlation with BTC

    # Correlation (rolling correlation with BTC using cache data)
    try:
        btc_history = HISTORY_CACHE.get("BTC/USDT", {}).get("1m", [])
        asset_history = HISTORY_CACHE.get(symbol, {}).get("1m", [])
        if len(btc_history) >= 15 and len(asset_history) >= 15:
            btc_df = pd.DataFrame(btc_history)
            asset_df = pd.DataFrame(asset_history)
            merged = pd.merge(
                btc_df, asset_df, on="time", suffixes=(
                    "_btc", "_asset"))
            if len(merged) >= 15:
                merged["ret_btc"] = merged["close_btc"].pct_change()
                merged["ret_asset"] = merged["close_asset"].pct_change()
                corr = merged["ret_btc"].rolling(
                    window=15).corr(
                    merged["ret_asset"])
                val = float(corr.iloc[-1])
                if not pd.isna(val):
                    btc_corr = val
    except Exception as e:
        log.warning(f"Failed to calculate cache correlation for {symbol}: {e}")

    return funding_rate, oi_delta, btc_corr


# ── Analysis cycle ──────────────────────────────────────────────────────

async def run_analysis_cycle(symbol: str) -> None:
    """Full pipeline for a specific asset: fetch data → sub-agents → master agent → broadcast."""
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")

    async def log_thought(text: str):
        if text.startswith(f"[{symbol}]"):
            await stream_thought(text)
        else:
            await stream_thought(f"[{symbol}] {text}")

    # 1. Fetch current price
    current_price = 0.0
    try:
        ohlcv = await fetch_ohlcv(symbol=symbol, timeframe=TIMEFRAME, limit=5)
        current_price = ohlcv["latest_close"]
    except Exception as exc:
        if symbol in RECENT_OHLCV_DFS and RECENT_OHLCV_DFS[symbol] is not None and not RECENT_OHLCV_DFS[symbol].empty:
            current_price = float(RECENT_OHLCV_DFS[symbol]["close"].iloc[-1])
            log.warning("[Analysis Cycle] OHLCV fetch failed for %s (%s). Using cached price: %.5f", symbol, exc, current_price)
        elif symbol in HISTORY_CACHE and HISTORY_CACHE[symbol]:
            tf = list(HISTORY_CACHE[symbol].keys())[0]
            if HISTORY_CACHE[symbol][tf]:
                current_price = float(HISTORY_CACHE[symbol][tf][-1]["close"])
                log.warning("[Analysis Cycle] OHLCV fetch failed for %s (%s). Using history cache price: %.5f", symbol, exc, current_price)
        
        if current_price <= 0.0:
            log.error("OHLCV fetch failed for %s: %s", symbol, exc)
            return

    # 1. Fetch spread, depth and funding rate metrics FIRST so we can use them
    # in pre-calculated MarketData
    spread_pct = 0.0004  # default 0.04% spread
    bid_vol = 1.0
    ask_vol = 1.0
    funding_rate = 0.0001  # standard default

    ob = LAST_ORDERBOOKS.get(symbol)
    if current_price > 0 and ob and ob.get('bids') and ob.get('asks'):
        spread_pct = (ob['asks'][0][0] - ob['bids'][0][0]) / current_price
        bid_vol = sum(b[1] for b in ob['bids'][:5])
        ask_vol = sum(a[1] for a in ob['asks'][:5])

    # Smart Funding Time Check
    now_utc = datetime.now(timezone.utc)
    current_time_minutes = now_utc.hour * 60 + now_utc.minute
    settlement_minutes = [0, 8 * 60, 16 * 60, 24 * 60]
    is_near_funding = any(abs(current_time_minutes - s_min)
                          <= 60 for s_min in settlement_minutes)

    if is_near_funding:
        from tools import _bybit_exchange
        exchange = _bybit_exchange()
        try:
            fr_data = await exchange.fetch_funding_rate(symbol, params={'category': 'linear'})
            funding_rate = abs(float(fr_data.get('fundingRate', 0.0)))
        except Exception as fr_err:
            log.warning(
                f"Failed to fetch live funding rate for {symbol}: {fr_err}")
        finally:
            await exchange.close()

    total_friction = spread_pct + 0.00055 + funding_rate  # taker fee = 0.055%
    cost_passed = total_friction <= 0.0035

    # 2. Construct MarketData and calculate Regime classification
    from master_agent import RegimeShield, MarketData, calculate_atr, run_master_agent
    import numpy as np
    import pandas as pd

    recent_ohlcv_df = RECENT_OHLCV_DFS.get(symbol)

    # Auto-refresh seed data if missing or empty
    if recent_ohlcv_df is None or recent_ohlcv_df.empty:
        try:
            from tools import _bybit_exchange, ccxt_symbol_format
            log.info(
                "[Analysis Cycle] Seed OHLCV missing for %s. Fetching fresh 1h seed...",
                symbol)
            exchange = _bybit_exchange()
            sym_id = ccxt_symbol_format(symbol, exchange)
            params = {'category': 'linear'}
            raw = []
            since = exchange.milliseconds() - ((5000 + 100) * 60 * 60 * 1000)
            while len(raw) < 5000:
                batch = await exchange.fetch_ohlcv(sym_id, timeframe="1h", limit=1000, since=since, params=params)
                if not batch:
                    break
                raw.extend(batch)
                since = batch[-1][0] + 1
            await exchange.close()
            if raw:
                raw_1h = raw[-5000:]
                df_1h = pd.DataFrame(
                    raw_1h,
                    columns=[
                        "timestamp",
                        "open",
                        "high",
                        "low",
                        "close",
                        "volume"])
                df_1h["timestamp"] = pd.to_datetime(
                    df_1h["timestamp"], unit="ms", utc=True)
                RECENT_OHLCV_DFS[symbol] = df_1h
                recent_ohlcv_df = df_1h
                log.info(
                    "[Analysis Cycle] Successfully refreshed 1h seed for %s (%d candles)",
                    symbol,
                    len(df_1h))
        except Exception as seed_err:
            log.warning(
                "[Analysis Cycle] Failed to refresh 1h seed for %s: %s",
                symbol,
                seed_err)

    data = None
    regime = "CHOPPY"

    if recent_ohlcv_df is not None and not recent_ohlcv_df.empty:
        try:
            atr_data = calculate_atr(recent_ohlcv_df, period=14)
            atr_val = atr_data['raw']
            min_atr = current_price * 0.001
            if atr_val < min_atr:
                atr_val = min_atr
            atr_pct = (atr_val / current_price) if current_price > 0 else 0.02

            close_prices = recent_ohlcv_df["close"].astype(float)
            rolling_vol_series = close_prices.pct_change().rolling(window=20).std()
            vol_val = 0.003
            if not rolling_vol_series.empty and not pd.isna(
                    rolling_vol_series.iloc[-1]):
                vol_val = float(rolling_vol_series.iloc[-1])

            adx_val = 22.0
            dist_val = 0.5
            from ml_brain import engineer_features
            engineered_df = engineer_features(recent_ohlcv_df.copy())
            if not engineered_df.empty:
                if "adx_14" in engineered_df.columns and not pd.isna(
                        engineered_df["adx_14"].iloc[-1]):
                    adx_val = float(engineered_df["adx_14"].iloc[-1])
                if "Distance_From_200_EMA_1D" in engineered_df.columns and not pd.isna(
                        engineered_df["Distance_From_200_EMA_1D"].iloc[-1]):
                    dist_val = float(
                        engineered_df["Distance_From_200_EMA_1D"].iloc[-1])

            features_arr = np.array([adx_val, vol_val, dist_val])

            from master_agent import calculate_rolling_avg_atr
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
                rolling_avg_atr=calculate_rolling_avg_atr(recent_ohlcv_df),
                orderbook=LAST_ORDERBOOKS.get(symbol),
                current_position=ACTIVE_TRADES.get(symbol)
            )

            # Predict regime
            shield = RegimeShield()
            regime = shield.detect_regime(data)
            data.regime = regime

        except Exception as pre_chk_err:
            log.warning(f"Regime calculation failed: {pre_chk_err}")

    # Fallback to guaranteed non-null MarketData instance if data is None
    if data is None:
        fallback_atr = current_price * 0.001 if current_price > 0 else 1.0
        data = MarketData(
            asset=symbol,
            price=current_price,
            atr_raw=fallback_atr,
            atr_pct=0.01,
            spread_pct=spread_pct,
            bid_depth=bid_vol,
            ask_depth=ask_vol,
            funding_rate=funding_rate,
            features=np.array([22.0, 0.003, 0.5]),
            rolling_avg_atr=fallback_atr,
            orderbook=LAST_ORDERBOOKS.get(symbol),
            current_position=ACTIVE_TRADES.get(symbol),
            regime="CHOPPY"
        )

    is_choppy = (regime == "CHOPPY")

    if is_choppy:
        # Bypasses sub-agents, ML inference, and order execution downstream.
        decision = await run_master_agent(
            sub_agent_results=[],
            symbol=symbol,
            current_price=current_price,
            stream_callback=log_thought,
            ml_score=None,
            ml_score_b=None,
            recent_ohlcv_df=recent_ohlcv_df,
            spread_pct=spread_pct,
            bid_vol=bid_vol,
            ask_vol=ask_vol,
            funding_rate=funding_rate,
            total_friction=total_friction,
            cost_passed=cost_passed,
            orderbook=LAST_ORDERBOOKS.get(symbol),
            market_data=data
        )
        await broadcast({"type": "decision", "symbol": symbol, "data": decision})
        return

    # Sub-agents are decorative for UI display — skipped during pipeline
    # execution to preserve speed
    sub_results = []

    # Prepare A/B features and perform dual-model inference
    df_full = RECENT_OHLCV_DFS.get(symbol)
    ml_score_A = None
    ml_score_B = None
    if df_full is not None:
        funding_rate, oi_delta, btc_corr = await fetch_ab_test_metrics(symbol)

        df_full = df_full.copy()
        df_full["funding_rate"] = funding_rate
        df_full["oi_delta"] = oi_delta
        df_full["btc_correlation"] = btc_corr
        RECENT_OHLCV_DFS[symbol] = df_full

        try:
            from ml_brain import get_brain
            brain = get_brain()
            if brain.trained:
                ml_score_A = await asyncio.to_thread(brain.predict, symbol, df_full, "A", regime=regime)
                ml_score_B = await asyncio.to_thread(brain.predict, symbol, df_full, "B", regime=regime)

                dec_A = ml_score_A.get("ml_signal", "NEUTRAL")
                conf_A = ml_score_A.get("confidence", 0.0)
                reg_A = ml_score_A.get("market_regime", "Trending")

                dec_B = ml_score_B.get("ml_signal", "NEUTRAL")
                conf_B = ml_score_B.get("confidence", 0.0)
                reg_B = ml_score_B.get("market_regime", "Trending")

                global AB_TEST_METRICS
                if symbol not in AB_TEST_METRICS:
                    AB_TEST_METRICS[symbol] = {
                        "A_hits": 0, "B_hits": 0, "total": 0}

                AB_TEST_METRICS[symbol]["total"] += 1
                if conf_A >= 0.60 and reg_A != "Choppy":
                    AB_TEST_METRICS[symbol]["A_hits"] += 1
                if conf_B >= 0.60 and reg_B != "Choppy":
                    AB_TEST_METRICS[symbol]["B_hits"] += 1

                total_cycles = AB_TEST_METRICS[symbol]["total"]
                hits_A = AB_TEST_METRICS[symbol]["A_hits"]
                hits_B = AB_TEST_METRICS[symbol]["B_hits"]

                log.debug(
                    f"A/B TEST {symbol} | Model A: {dec_A} ({conf_A:.1%}) | Model B: {dec_B} ({conf_B:.1%})"
                )
        except Exception as inf_err:
            log.error(
                f"Dual-model inference / tracking failed for {symbol}: {inf_err}")

    # 4. Master Agent decision (Model A remains the live executor for this
    # test)
    try:
        decision = await run_master_agent(
            sub_agent_results=sub_results,
            symbol=symbol,
            current_price=current_price,
            stream_callback=log_thought,
            ml_score=ml_score_A,
            ml_score_b=ml_score_B,
            recent_ohlcv_df=RECENT_OHLCV_DFS.get(symbol),
            spread_pct=spread_pct,
            bid_vol=bid_vol,
            ask_vol=ask_vol,
            funding_rate=funding_rate,
            total_friction=total_friction,
            cost_passed=cost_passed,
            orderbook=LAST_ORDERBOOKS.get(symbol),
            market_data=data
        )
    except Exception as exc:
        log.error("Master agent failed for %s: %s", symbol, exc)
        await broadcast({"type": "error", "symbol": symbol, "text": f"Master agent failed: {exc}"})
        return

    # 5. Broadcast final decision
    await broadcast({"type": "decision", "symbol": symbol, "data": decision})

    # Trigger decorative sub-agents asynchronously for UI display only
    # (non-blocking)
    asyncio.create_task(
        update_sub_agent_ui(
            symbol,
            LAST_ORDERBOOKS.get(symbol),
            regime=regime))

    # Extract decision parameters
    d = decision.get("decision", "HOLD")
    # Kelly Survival Cap: Hardcode max leverage multiplier of 5x
    leverage = min(5, decision.get("leverage", 1))
    stop_loss = decision.get("stop_loss")
    take_profit = decision.get("take_profit")
    confidence = decision.get("confidence", 0.0)

    # Handle CLOSE_EARLY / Position Exits or Trailing SL Updates
    reasoning = decision.get("reasoning", "")
    if d == "CLOSE_EARLY" or (
        symbol in ACTIVE_TRADES and any(
            kw in reasoning for kw in [
            "SL_HIT",
            "TP_HIT",
            "SIGNAL_INVALIDATION",
            "TRAILING_STOP_HIT"])):
        if symbol in ACTIVE_TRADES:
            active_trade = ACTIVE_TRADES[symbol]
            entry = active_trade["entry_price"]
            margin = active_trade.get("margin", 2.00)
            lev = active_trade.get("leverage", 1)
            trade_dir = active_trade.get("direction", "LONG")

            pnl_raw = (current_price - entry) / entry if entry > 0 else 0.0
            if trade_dir == "SHORT":
                pnl_raw = -pnl_raw

            pnl_usd = margin * lev * pnl_raw
            pnl_pct = pnl_raw * 100 * lev

            global VIRTUAL_WALLET
            VIRTUAL_WALLET += margin + pnl_usd
            del ACTIVE_TRADES[symbol]
            try:
                import time
                from master_agent import TRADE_COOLDOWNS
                TRADE_COOLDOWNS[symbol] = time.time()
            except Exception:
                pass
            save_wallet_state()

            outcome_str = reasoning or "CLOSED_EARLY"
            _log_close_trade(
                symbol,
                active_trade,
                current_price,
                pnl_usd,
                pnl_pct,
                outcome_str)

            mode_str = "⚡ LIVE" if EXECUTE_MODE else "⏸ SIMULATION"
            await log_thought(
                f"[{mode_str} Wallet] Closed position ({outcome_str})! "
                f"Close Price: {current_price} USDT | PnL: ${pnl_usd:+.2f} ({pnl_pct:+.2f}%) | Wallet: ${VIRTUAL_WALLET:.2f}"
            )
    elif symbol in ACTIVE_TRADES and stop_loss is not None:
        from tools import round_price_prec
        ACTIVE_TRADES[symbol]["stop_loss"] = round_price_prec(
            stop_loss, symbol)
        save_wallet_state()

    # Trigger Virtual Wallet Trade Allocation if LONG/SHORT (all symbols
    # including BTC)
    if d in ["LONG", "SHORT"]:
        if symbol not in ACTIVE_TRADES:
            # Calculate current total account balance (equity)
            margin_in_use = sum(t.get("margin", 2.00)
                                for t in ACTIVE_TRADES.values())
            unrealized_pnl = 0.0
            for active_sym, active_t in ACTIVE_TRADES.items():
                active_curr_price = active_t.get("entry_price")
                if active_sym in HISTORY_CACHE and HISTORY_CACHE[active_sym]:
                    active_tf_keys = list(HISTORY_CACHE[active_sym].keys())
                    if active_tf_keys:
                        active_candles = HISTORY_CACHE[active_sym][active_tf_keys[0]]
                        if active_candles:
                            active_curr_price = active_candles[-1]["close"]
                active_dir = active_t["direction"]
                active_entry = active_t["entry_price"]
                active_lev = active_t["leverage"]
                active_margin = active_t.get("margin", 2.00)
                if active_dir == "LONG":
                    active_pnl_raw = (
                        active_curr_price - active_entry) / active_entry if active_entry > 0 else 0.0
                else:
                    active_pnl_raw = (
                        active_entry - active_curr_price) / active_entry if active_entry > 0 else 0.0
                unrealized_pnl += active_margin * active_lev * active_pnl_raw

            total_balance = VIRTUAL_WALLET + margin_in_use + unrealized_pnl
            dynamic_margin = round(decision.get("margin", 2.00), 2)

            available_cash = VIRTUAL_WALLET
            if dynamic_margin > available_cash:
                dynamic_margin = available_cash

            if available_cash >= 0.10 and dynamic_margin >= 0.10:
                # ---> ISOLATED MARGIN ENFORCEMENT GATE <---
                if not EXECUTE_MODE:
                    await log_thought(f"[SIMULATION] Mocking Isolated Margin API success for {symbol}.")
                else:
                    from tools import _bybit_exchange
                    import ccxt
                    try:
                        ex = _bybit_exchange()

                        # 1. Enforce ISOLATED mode
                        try:
                            await ex.set_margin_mode('isolated', symbol, params={'marginMode': 'isolated'})
                        except ccxt.MarginModeAlreadySet:
                            pass
                        except Exception as e:
                            await log_thought(f"⛔ VETO_CROSS_MARGIN_DETECTED: Failed to set ISOLATED mode for {symbol}. Order rejected. Error: {e}")
                            await ex.close()
                            return

                        # 2. Set strict ML Leverage
                        try:
                            await ex.set_leverage(leverage, symbol)
                        except Exception as e:
                            log.warning(
                                "Failed to set leverage to %sx for %s (it may already be set): %s",
                                leverage,
                                symbol,
                                e)

                        await ex.close()
                    except Exception as ex_err:
                        await log_thought(f"⛔ ISOLATED_GATE_ERROR: Execution blocked for {symbol}. {ex_err}")
                        return
                # ---> END ISOLATED GATE <---

                from tools import round_price_prec
                entry_rounded = round_price_prec(current_price, symbol)
                sl_rounded = round_price_prec(
                    stop_loss, symbol) if stop_loss else None
                tp_rounded = round_price_prec(
                    take_profit, symbol) if take_profit else None

                allocated = await allocate_margin(symbol, dynamic_margin)
                if not allocated:
                    await log_thought(f"[{symbol}] ⛔ BALANCE_OVERALLOCATED: Margin allocation of ${dynamic_margin:.2f} failed. Trade cancelled.")
                    return

                ACTIVE_TRADES[symbol] = {
                    "direction": d,
                    "entry_price": entry_rounded,
                    "leverage": leverage,
                    "stop_loss": sl_rounded,
                    "take_profit": tp_rounded,
                    "margin": dynamic_margin,
                    "kelly_conf": confidence,
                    "is_scalp": decision.get("is_scalp", False),
                    "scale_factor": decision.get("scale_factor", 1.0),
                    "margin_mode": "ISOLATED",
                    "timestamp": datetime.now(timezone.utc).isoformat()
                }
                save_wallet_state()
                _log_open_trade(
                    symbol,
                    d,
                    entry_rounded,
                    confidence,
                    leverage,
                    sl_rounded,
                    tp_rounded,
                    dynamic_margin,
                    confidence)

                mode_str = "⚡ LIVE" if EXECUTE_MODE else "⏸ SIMULATION"
                await log_thought(
                    f"[{mode_str} Wallet] Opened position! Dir: {d} | "
                    f"Entry: {entry_rounded} USDT | Lev: {leverage}x | Dynamic Margin: ${dynamic_margin:.2f} | SL: {sl_rounded} | TP: {tp_rounded}"
                )
            else:
                await log_thought(
                    f"⚠️ [Wallet] Insufficient cash or size to bet. "
                    f"Available: ${available_cash:.2f} USD | Dynamic Margin Target: ${dynamic_margin:.2f} USD"
                )

    # Update LATEST_DECISIONS cache for unified state
    # BUG FIX: ML probabilities are nested under 'ml_brain' key, not top-level
    # 'probabilities'
    probs = decision.get("ml_brain", {}).get("probabilities", {})
    LATEST_DECISIONS[symbol] = {
        "decision": decision.get("decision", "HOLD"),
        "confidence": decision.get("confidence", 0.0),
        "leverage": decision.get("leverage", 1),
        "stop_loss": decision.get("stop_loss"),
        "p_up": probs.get("UPTREND", 0.33),
        "p_down": probs.get("DOWNTREND", 0.33),
        "p_range": probs.get("RANGING", 0.33),
        "market_regime": decision.get("market_regime", "Choppy"),
        "reasoning": decision.get("reasoning", "")
    }

    if d in ["LONG", "SHORT"]:
        asyncio.create_task(
            dispatch_shap_rationale(
                symbol, RECENT_OHLCV_DFS.get(symbol)))

    try:
        await broadcast_unified_state()
    except Exception as state_err:
        log.error("Failed to broadcast unified state: %s", state_err)


async def daily_report_cron() -> None:
    """Generates daily report for all active assets every 24 hours in the background."""
    while True:
        await asyncio.sleep(24 * 3600)  # Wait 24 hours
        for sym in SYMBOLS:
            try:
                log.info("Running scheduled daily report for %s...", sym)
                report = await generate_daily_report(symbol=sym)
                for line in report.split("\n"):
                    await stream_thought(f"[{sym}] {line}")
            except Exception as e:
                log.error("Scheduled daily report failed for %s: %s", sym, e)


# ── Analysis loop ───────────────────────────────────────────────────────

async def analysis_loop_for_symbol(symbol: str) -> None:
    """Runs analysis cycles specifically for the active symbol on the configured interval."""
    log.info(
        "Analysis loop started for %s. Cycle interval: %ds",
        symbol,
        CYCLE_INTERVAL)
    await asyncio.sleep(5.0)
    while True:
        try:
            if symbol != "HOME":
                await run_analysis_cycle(symbol)
        except Exception as exc:
            log.error(
                "Unhandled error in analysis loop for %s: %s",
                symbol,
                exc,
                exc_info=True)
            await broadcast({"type": "error", "text": f"Loop error for {symbol}: {exc}"})
        await asyncio.sleep(CYCLE_INTERVAL)


# ── Indicator Calculation Helper ────────────────────────────────────────

def compute_indicators(df: pd.DataFrame, pane_id: str) -> pd.DataFrame:
    close = df["close"]
    high = df["high"]
    low = df["low"]
    volume = df["volume"]

    if pane_id == "1d":
        span = min(200, len(df))
        df["ema200"] = close.ewm(span=span, adjust=False).mean()
        from ml_brain import _calculate_adx
        df["adx"] = _calculate_adx(high, low, close, 14).fillna(20.0)

    elif pane_id == "4h":
        middle = close.rolling(window=20).mean()
        std = close.rolling(window=20).std()
        df["bb_middle"] = middle
        df["bb_upper"] = middle + 2 * std
        df["bb_lower"] = middle - 2 * std
        df["bb_middle"] = df["bb_middle"].fillna(close)
        df["bb_upper"] = df["bb_upper"].fillna(close)
        df["bb_lower"] = df["bb_lower"].fillna(close)

    elif pane_id == "1h":
        df["resistance"] = high.rolling(window=30, min_periods=1).max()
        df["support"] = low.rolling(window=30, min_periods=1).min()

    elif pane_id == "15m":
        delta = close.diff()
        gain = delta.clip(lower=0).rolling(window=14, min_periods=1).mean()
        loss = (-delta.clip(upper=0)).rolling(window=14, min_periods=1).mean()
        rs = gain / loss.replace(0, 1e-9)
        df["rsi"] = 100 - (100 / (1 + rs))
        df["rsi"] = df["rsi"].fillna(50.0)

        # Calculate local swing highs/lows for price and RSI
        df["price_swing_low"] = low.shift(7) == low.rolling(window=15).min()
        df["price_swing_high"] = high.shift(7) == high.rolling(window=15).max()
        df["rsi_swing_low"] = df["rsi"].shift(
            7) == df["rsi"].rolling(window=15).min()
        df["rsi_swing_high"] = df["rsi"].shift(
            7) == df["rsi"].rolling(window=15).max()

        swing_lows = df[df["price_swing_low"]].copy()
        if len(swing_lows) > 0:
            swing_lows["prev_low_price"] = swing_lows["low"].shift(1)
            swing_lows["prev_low_rsi"] = swing_lows["rsi"].shift(1)
            df = df.join(
                swing_lows[["prev_low_price", "prev_low_rsi"]], rsuffix="_swing")
            df["prev_low_price"] = df["prev_low_price"].ffill()
            df["prev_low_rsi"] = df["prev_low_rsi"].ffill()
        else:
            df["prev_low_price"] = np.nan
            df["prev_low_rsi"] = np.nan

        swing_highs = df[df["price_swing_high"]].copy()
        if len(swing_highs) > 0:
            swing_highs["prev_high_price"] = swing_highs["high"].shift(1)
            swing_highs["prev_high_rsi"] = swing_highs["rsi"].shift(1)
            df = df.join(
                swing_highs[["prev_high_price", "prev_high_rsi"]], rsuffix="_swing")
            df["prev_high_price"] = df["prev_high_price"].ffill()
            df["prev_high_rsi"] = df["prev_high_rsi"].ffill()
        else:
            df["prev_high_price"] = np.nan
            df["prev_high_rsi"] = np.nan

        df["bullish_hidden_div"] = np.where(
            df["price_swing_low"] &
            (df["low"] > df["prev_low_price"]) &
            (df["rsi"] < df["prev_low_rsi"]),
            1.0, 0.0
        )
        df["bearish_regular_div"] = np.where(
            df["price_swing_high"] &
            (df["high"] > df["prev_high_price"]) &
            (df["rsi"] < df["prev_high_rsi"]),
            1.0, 0.0
        )

    elif pane_id == "3m":
        denom = (high - low).replace(0, 1e-9)
        buy_vol = volume * (close - low) / denom
        sell_vol = volume * (high - close) / denom
        df["volume_delta"] = buy_vol - sell_vol
        df["volume_delta"] = df["volume_delta"].fillna(0.0)

    elif pane_id == "macd":
        ema_fast = close.ewm(span=12, adjust=False).mean()
        ema_slow = close.ewm(span=26, adjust=False).mean()
        macd_line = ema_fast - ema_slow
        signal_line = macd_line.ewm(span=9, adjust=False).mean()
        df["macd_line"] = macd_line
        df["signal_line"] = signal_line
        df["macd_histogram"] = macd_line - signal_line
        df["macd_line"] = df["macd_line"].fillna(0.0)
        df["signal_line"] = df["signal_line"].fillna(0.0)
        df["macd_histogram"] = df["macd_histogram"].fillna(0.0)

    return df


# ── Live OHLCV Stream loop ──────────────────────────────────────────────

async def preload_all_history() -> None:
    """Pre-loads historical data for all timeframes and assets into HISTORY_CACHE on server startup."""
    global HISTORY_CACHE, RECENT_OHLCV_DFS
    log.info("Pre-loading historical data for all timeframes & assets...")
    from tools import _bybit_exchange, ccxt_symbol_format
    exchange = _bybit_exchange()
    timeframes = {
        "1d": "1d",
        "4h": "4h",
        "1h": "1h",
        "15m": "15m",
        "3m": "3m",
        "1m": "1m"
    }
    try:
        for sym in SYMBOLS:
            try:
                # Normalize symbol to Bybit raw market ID (e.g. XRP/USDT ->
                # XRPUSDT)
                sym_id = ccxt_symbol_format(sym, exchange)
                log.info(
                    "Preloading data for %s (Bybit ID: %s)...",
                    sym,
                    sym_id)

                for pane_id, tf in timeframes.items():
                    params = {'category': 'linear'}
                    raw = await exchange.fetch_ohlcv(sym_id, timeframe=tf, limit=200, params=params)
                    if raw:
                        df = pd.DataFrame(
                            raw,
                            columns=[
                                "timestamp",
                                "open",
                                "high",
                                "low",
                                "close",
                                "volume"])
                        df["time"] = (df["timestamp"] / 1000).astype(int)
                        df = compute_indicators(df, pane_id)
                        HISTORY_CACHE[sym][pane_id] = df.to_dict(
                            orient="records")
                        LATEST_OHLCV_CACHE[sym][pane_id] = df.to_dict(
                            orient="records")

                # Pre-load 1h data seeds for ML Brain regime classifiers
                params = {'category': 'linear'}
                raw = []
                since = exchange.milliseconds() - ((5000 + 100) * 60 * 60 * 1000)
                while len(raw) < 5000:
                    batch = await exchange.fetch_ohlcv(sym_id, timeframe="1h", limit=1000, since=since, params=params)
                    if not batch:
                        break
                    raw.extend(batch)
                    since = batch[-1][0] + 1
                raw_1h = raw[-5000:]
                df_1h = pd.DataFrame(
                    raw_1h,
                    columns=[
                        "timestamp",
                        "open",
                        "high",
                        "low",
                        "close",
                        "volume"])
                df_1h["timestamp"] = pd.to_datetime(
                    df_1h["timestamp"], unit="ms", utc=True)
                RECENT_OHLCV_DFS[sym] = df_1h
                log.info(
                    "ML Brain OHLCV seed loaded for %s (%d candles)",
                    sym,
                    len(df_1h))

            except Exception as exc:
                log.warning(
                    "[WARNING] Skipping %s: Data fetch failed: %s", sym, exc)
                if '429' in str(exc) or 'DDoS' in str(exc):
                    log.warning(
                        "[RATE LIMIT / DDOS DETECTED] Triggering cool-down. Sleeping for 10 seconds...")
                    await asyncio.sleep(10)

            # Enforce Rate Limiting: 0.8s pause between asset fetches
            await asyncio.sleep(0.8)

    finally:
        await exchange.close()


def update_ohlcv_caches(symbol: str, pane_id: str, df: pd.DataFrame):
    """Updates HISTORY_CACHE and LATEST_OHLCV_CACHE with the new records from df."""
    records = df.to_dict(orient="records")
    for cache in [HISTORY_CACHE, LATEST_OHLCV_CACHE]:
        existing = cache.setdefault(symbol, {}).setdefault(pane_id, [])
        existing_by_time = {c["time"]: c for c in existing}
        for nc in records:
            existing_by_time[nc["time"]] = nc
        # Sort and keep last 200 candles
        cache[symbol][pane_id] = [existing_by_time[t]
                                  for t in sorted(existing_by_time.keys())][-200:]


async def stream_live_ohlcv_for_symbol(symbol: str) -> None:
    """Streams live indicators and order book ticks for a single active symbol."""
    global HISTORY_CACHE, LATEST_OHLCV_CACHE, LAST_ORDERBOOKS, RECENT_OHLCV_DFS
    log.info("Started live stream update loop for %s", symbol)

    from tools import _bybit_exchange, ccxt_symbol_format
    exchange = _bybit_exchange()

    # Normalize to Bybit raw market ID once at stream init
    sym_id = ccxt_symbol_format(symbol, exchange)
    log.info("Live stream for %s using Bybit ID: %s", symbol, sym_id)

    timeframes = {
        "1d": "1d",
        "4h": "4h",
        "1h": "1h",
        "15m": "15m",
        "3m": "3m",
        "1m": "1m"
    }

    counter = 0
    try:
        while True:
            try:
                if symbol != "HOME":
                    # 1. Update 1m and 3m timeframes (every 2 seconds)
                    for pane_id in ["1m", "3m"]:
                        tf = timeframes[pane_id]
                        params = {'category': 'linear'}
                        try:
                            raw = await exchange.fetch_ohlcv(sym_id, timeframe=tf, limit=5, params=params)
                        except Exception as fetch_err:
                            log.warning(
                                "[WARNING] Skipping %s %s OHLCV: %s", symbol, pane_id, fetch_err)
                            await asyncio.sleep(0.5)
                            continue
                        if raw:
                            global LAST_PRICE_UPDATE_TIME
                            LAST_PRICE_UPDATE_TIME = time.time()
                            df = pd.DataFrame(
                                raw,
                                columns=[
                                    "timestamp",
                                    "open",
                                    "high",
                                    "low",
                                    "close",
                                    "volume"])
                            df["time"] = (df["timestamp"] / 1000).astype(int)
                            df = compute_indicators(df, pane_id)
                            update_ohlcv_caches(symbol, pane_id, df)
                            last_record = LATEST_OHLCV_CACHE[symbol][pane_id][-1]

                            await broadcast({
                                "type": "OHLCV",
                                "symbol": symbol,
                                "paneId": pane_id,
                                "data": last_record
                            })

                            # Real-time active trade SL/TP/Liquidation tracking
                            if pane_id == "1m":
                                current_price = last_record["close"]
                                if symbol in ACTIVE_TRADES:
                                    trade = ACTIVE_TRADES[symbol]
                                    direction = trade["direction"]
                                    entry = trade["entry_price"]
                                    leverage = trade["leverage"]
                                    sl = trade.get("stop_loss")
                                    tp = trade.get("take_profit")
                                    margin = trade.get("margin", 2.00)

                                    if direction == "LONG":
                                        pnl_raw = (
                                            current_price - entry) / entry if entry > 0 else 0.0
                                    else:
                                        pnl_raw = (
                                            entry - current_price) / entry if entry > 0 else 0.0

                                    pnl_pct = pnl_raw * 100 * leverage
                                    pnl_usd = margin * leverage * pnl_raw

                                    hit = False
                                    outcome = "ACTIVE"

                                    if sl is not None:
                                        if direction == "LONG" and current_price <= sl:
                                            hit = True
                                            outcome = "SL HIT"
                                        elif direction == "SHORT" and current_price >= sl:
                                            hit = True
                                            outcome = "SL HIT"

                                    if not hit and tp is not None:
                                        if direction == "LONG" and current_price >= tp:
                                            hit = True
                                            outcome = "TP HIT"
                                        elif direction == "SHORT" and current_price <= tp:
                                            hit = True
                                            outcome = "TP HIT"

                                    if not hit and pnl_pct <= -100.0:
                                        hit = True
                                        outcome = "LIQUIDATED"
                                        pnl_usd = -margin
                                        pnl_pct = -100.0

                                    if hit:
                                        global VIRTUAL_WALLET
                                        VIRTUAL_WALLET += pnl_usd
                                        del ACTIVE_TRADES[symbol]
                                        save_wallet_state()
                                        _log_close_trade(
                                            symbol, trade, current_price, pnl_usd, pnl_pct, outcome)

                                        mode_str = "⚡ LIVE" if EXECUTE_MODE else "⏸ SIMULATION"
                                        await stream_thought(
                                            f"[{symbol}] [{mode_str} Wallet] Closed position! Status: {outcome} | "
                                            f"Realized PnL: {pnl_usd:+.2f} USD ({pnl_pct:+.2f}%) | New Balance: ${VIRTUAL_WALLET:.2f}"
                                        )
                                        await broadcast_unified_state()
                            await asyncio.sleep(0.02)

                    # 2. Update MACD Pane (based on 3m timeframe) (every 2
                    # seconds)
                    params = {'category': 'linear'}
                    try:
                        raw_3m = await exchange.fetch_ohlcv(sym_id, timeframe="3m", limit=35, params=params)
                    except Exception as macd_err:
                        log.warning(
                            "[WARNING] Skipping %s MACD: %s", symbol, macd_err)
                        raw_3m = None
                    if raw_3m:
                        df = pd.DataFrame(
                            raw_3m,
                            columns=[
                                "timestamp",
                                "open",
                                "high",
                                "low",
                                "close",
                                "volume"])
                        df["time"] = (df["timestamp"] / 1000).astype(int)
                        df = compute_indicators(df, "macd")
                        last_macd = df.to_dict(orient="records")[-1]
                        await broadcast({
                            "type": "MACD",
                            "symbol": symbol,
                            "paneId": "macd",
                            "data": last_macd
                        })
                        await asyncio.sleep(0.02)

                    # 3. Update Order Book (every 2 seconds)
                    try:
                        params = {'category': 'linear'}
                        ob_raw = await exchange.fetch_order_book(sym_id, limit=20, params=params)
                        bids = [[float(b[0]), float(b[1])]
                                for b in ob_raw.get('bids', [])]
                        asks = [[float(a[0]), float(a[1])]
                                for a in ob_raw.get('asks', [])]
                        orderbook = {
                            "bids": bids,
                            "asks": asks
                        }
                        LAST_ORDERBOOKS[symbol] = orderbook
                        spread = 0.0
                        if bids and asks:
                            spread = asks[0][0] - bids[0][0]
                        await broadcast({
                            "type": "ORDERBOOK",
                            "symbol": symbol,
                            "paneId": "orderbook",
                            "data": {
                                "bids": bids,
                                "asks": asks,
                                "spread": spread
                            }
                        })
                    except Exception as ob_err:
                        log.error(
                            "Failed to fetch real order book for %s: %s", symbol, ob_err)
                    await asyncio.sleep(0.02)

                    # 4. Update 15m and 1h timeframes (every 10 seconds)
                    if counter % 5 == 0:
                        for pane_id in ["15m", "1h"]:
                            tf = timeframes[pane_id]
                            params = {'category': 'linear'}
                            try:
                                raw = await exchange.fetch_ohlcv(sym_id, timeframe=tf, limit=35, params=params)
                            except Exception as tf_err:
                                log.warning(
                                    "[WARNING] Skipping %s %s: %s", symbol, pane_id, tf_err)
                                await asyncio.sleep(0.5)
                                continue
                            if raw:
                                df = pd.DataFrame(
                                    raw,
                                    columns=[
                                        "timestamp",
                                        "open",
                                        "high",
                                        "low",
                                        "close",
                                        "volume"])
                                df["time"] = (
                                    df["timestamp"] / 1000).astype(int)
                                df = compute_indicators(df, pane_id)
                                update_ohlcv_caches(symbol, pane_id, df)
                                last_record = LATEST_OHLCV_CACHE[symbol][pane_id][-1]

                                await broadcast({
                                    "type": "OHLCV",
                                    "symbol": symbol,
                                    "paneId": pane_id,
                                    "data": last_record
                                })
                                await asyncio.sleep(0.02)

                                if pane_id == "1h":
                                    try:
                                        params = {'category': 'linear'}
                                        raw_full = await exchange.fetch_ohlcv(sym_id, timeframe="1h", limit=500, params=params)
                                        df_full = pd.DataFrame(
                                            raw_full,
                                            columns=[
                                                "timestamp",
                                                "open",
                                                "high",
                                                "low",
                                                "close",
                                                "volume"])
                                        df_full["timestamp"] = pd.to_datetime(
                                            df_full["timestamp"], unit="ms", utc=True)
                                        RECENT_OHLCV_DFS[symbol] = df_full
                                    except Exception as full_err:
                                        log.warning(
                                            "[WARNING] Skipping %s 1h full seed refresh: %s", symbol, full_err)

                    # 5. Update 4h and 1d timeframes (every 30 seconds)
                    if counter % 15 == 0:
                        for pane_id in ["4h", "1d"]:
                            tf = timeframes[pane_id]
                            params = {'category': 'linear'}
                            try:
                                raw = await exchange.fetch_ohlcv(sym_id, timeframe=tf, limit=200, params=params)
                            except Exception as slow_tf_err:
                                log.warning(
                                    "[WARNING] Skipping %s %s: %s", symbol, pane_id, slow_tf_err)
                                await asyncio.sleep(0.5)
                                continue
                            if raw:
                                df = pd.DataFrame(
                                    raw,
                                    columns=[
                                        "timestamp",
                                        "open",
                                        "high",
                                        "low",
                                        "close",
                                        "volume"])
                                df["time"] = (
                                    df["timestamp"] / 1000).astype(int)
                                df = compute_indicators(df, pane_id)
                                update_ohlcv_caches(symbol, pane_id, df)
                                last_record = LATEST_OHLCV_CACHE[symbol][pane_id][-1]

                                await broadcast({
                                    "type": "OHLCV",
                                    "symbol": symbol,
                                    "paneId": pane_id,
                                    "data": last_record
                                })
                                await asyncio.sleep(0.02)

                counter = (counter + 1) % 60
            except Exception as exc:
                log.warning("[WARNING] Skipping cycle for %s: %s", symbol, exc)

            await asyncio.sleep(3.0)
    except asyncio.CancelledError:
        log.info("Live stream task cancelled for %s", symbol)
        raise
    finally:
        await exchange.close()
        log.info("Closed exchange for %s", symbol)


async def start_symbol_tasks(symbol: str):
    global CURRENT_SYMBOL
    if symbol == CURRENT_SYMBOL and symbol in ACTIVE_RUNNERS:
        return

    log.info(
        "Switching active streaming/analysis tasks from %s to %s",
        CURRENT_SYMBOL,
        symbol)

    for sym, runner in list(ACTIVE_RUNNERS.items()):
        if sym != symbol:
            await runner.stop()
            ACTIVE_RUNNERS.pop(sym, None)

    if symbol not in ACTIVE_RUNNERS:
        runner = AssetRunner(symbol)
        ACTIVE_RUNNERS[symbol] = runner
        await runner.start()

    CURRENT_SYMBOL = symbol


async def handle_root(request):
    index_path = os.path.join(STATIC_DIR, "index.html")
    if os.path.exists(index_path) and os.path.isfile(index_path):
        try:
            with open(index_path, "rb") as f:
                content = f.read()
            return web.Response(body=content, content_type='text/html', status=200)
        except Exception as exc:
            log.error("Failed serving index.html in handle_root: %s", exc)

    html_content = """<!DOCTYPE html><html><body><h2>OceanHub Engine Active</h2></body></html>"""
    return web.Response(text=html_content, content_type='text/html', status=200)


async def handle_static_assets(request):
    req_path = request.path.lstrip('/').split('?')[0]
    file_path = os.path.join(STATIC_DIR, req_path)
    if req_path and os.path.exists(file_path) and os.path.isfile(file_path):
        mime_type = "application/octet-stream"
        if file_path.endswith('.html'):
            mime_type = "text/html"
        elif file_path.endswith('.js'):
            mime_type = "application/javascript"
        elif file_path.endswith('.css'):
            mime_type = "text/css"
        elif file_path.endswith('.svg'):
            mime_type = "image/svg+xml"
        elif file_path.endswith('.png'):
            mime_type = "image/png"
        try:
            with open(file_path, "rb") as f:
                content = f.read()
            return web.Response(body=content, content_type=mime_type, status=200)
        except Exception:
            pass
    return await handle_root(request)


async def handle_health(request):
    from ml_brain import get_brain
    brain = get_brain()
    if isinstance(brain.trained, dict):
        models_loaded = any(brain.trained.get(sym, False) for sym in SYMBOLS)
    else:
        models_loaded = bool(brain.trained)

    now = time.time()
    diff = now - LAST_PRICE_UPDATE_TIME if LAST_PRICE_UPDATE_TIME > 0 else 0
    last_update_ok = diff <= 300

    status_msg = f"OK - ModelsLoaded: {models_loaded}, PriceUpdate: {last_update_ok}"
    return web.Response(text=status_msg, status=200)


async def start_health_server():
    if WS_PORT == 8000:
        log.info("HTTP Health & Status page served directly on WS_PORT (8000)")
        return
    try:
        app = web.Application()
        app.router.add_get('/health', handle_health)
        app.router.add_get('/{tail:.*}', handle_static_assets)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, '0.0.0.0', 8000)
        await site.start()
        log.info("HTTP Health & Static Web Dashboard server started on port 8000")
    except Exception as exc:
        log.warning("Secondary health server on port 8000 skipped: %s", exc)


async def perform_shutdown(sig=None):
    if sig:
        log.info(
            "Received exit signal %s. Initiating graceful shutdown...",
            sig.name)
    else:
        log.info("Initiating graceful shutdown...")

    # Cancel all active runners
    for sym, runner in list(ACTIVE_RUNNERS.items()):
        await runner.stop()
        del ACTIVE_RUNNERS[sym]

    # Close all active websocket client connections
    if CLIENTS:
        log.info(
            "Closing %d active client websocket connections...",
            len(CLIENTS))
        close_tasks = [c.close() for c in CLIENTS]
        await asyncio.gather(*close_tasks, return_exceptions=True)

    log.info("Graceful shutdown complete.")


def register_signal_handlers():
    loop = asyncio.get_running_loop()

    async def shutdown_task(sig):
        await perform_shutdown(sig)
        loop.stop()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(
                sig, lambda s=sig: asyncio.create_task(
                    shutdown_task(s)))
        except NotImplementedError:
            # Fallback for Windows local development environments
            pass


# ── Entry point ─────────────────────────────────────────────────────────

async def main() -> None:
    global LAST_PRICE_UPDATE_TIME
    LAST_PRICE_UPDATE_TIME = time.time()  # Initialize to current time at startup

    log.info("OceanHub backend starting...")
    log.info("================================================")
    log.info("  PHASE 0 DRY RUN MODE: %s",
             "ACTIVE" if DRY_RUN_MODE else "INACTIVE")
    log.info("  Order execution logic is simulated / read-only.")
    log.info("================================================")
    log.info("WebSocket server → ws://%s:%d", WS_HOST, WS_PORT)
    log.info("Symbols: %s | Timeframe: %s | Cycle: %ds",
             SYMBOLS, TIMEFRAME, CYCLE_INTERVAL)

    # Fetch instrument list exactly once during startup (Initialising neural
    # substrate)
    try:
        from tools import _bybit_exchange
        import tools
        log.info(
            "[System] Initialising neural substrate: Loading instrument metadata...")
        temp_ex = _bybit_exchange()
        # Fetch markets from Bybit Testnet/Mainnet
        tools.MARKET_CACHE = await temp_ex.load_markets()
        await temp_ex.close()
        log.info(
            "Successfully cached %d linear perpetual markets at startup.", len(
                tools.MARKET_CACHE))
    except Exception as cache_err:
        log.error(
            "Failed to populate global MARKET_CACHE at startup: %s",
            cache_err)

    # State Recovery Protocol: On-Startup Exchange Sync
    try:
        from tools import sync_exchange_positions
        synced_positions = await sync_exchange_positions(SYMBOLS)
        if synced_positions:
            ACTIVE_TRADES.update(synced_positions)
            save_wallet_state()
            log.info(
                "State Recovery Protocol: Restored %d active position(s) from Exchange API on startup.",
                len(synced_positions))
    except Exception as sync_err:
        log.warning("State recovery exchange sync warning: %s", sync_err)

    # Register OS signal handlers for graceful shutdown (SIGTERM/SIGINT)
    register_signal_handlers()

    # Start HTTP Health check server on port 8000
    asyncio.create_task(start_health_server())

    # Start global position guardian — monitors ALL active trades for SL/TP/liquidation
    # regardless of which symbol is currently being streamed by the frontend.
    asyncio.create_task(global_position_guardian())

    # Initialise ML Brain (load from disk or train fresh) for all assets
    async def _init_ml():
        try:
            from ml_brain import initialize_brain
            await stream_thought("────────────────────────────────────")
            await stream_thought("[ML Brain] Initialising Random Forest classifiers for all assets...")
            results = await initialize_brain(force_retrain=False, symbols=SYMBOLS)
            for sym, result in results.items():
                status = result.get("status", "?")
                if status == "trained":
                    await stream_thought(f"[ML Brain] {sym} trained: acc={result.get('test_accuracy')}%")
                elif status == "loaded_from_disk" or result.get("trained"):
                    await stream_thought(f"[ML Brain] {sym} loaded from disk.")
                else:
                    await stream_thought(f"[ML Brain] {sym} init result: {result}")
        except Exception as exc:
            log.warning("ML Brain init failed: %s", exc)
            await stream_thought(f"[ML Brain] Init failed: {exc}")

    # Preload historical data for all assets and timeframes
    await preload_all_history()

    # Start the default active symbol tasks (ADA/USDT)
    await start_symbol_tasks("ADA/USDT")

    # Start ML Brain training in background (after 10s so OHLCV data is seeded
    # first)
    async def _delayed_ml_init():
        await asyncio.sleep(10)
        await _init_ml()
    asyncio.create_task(_delayed_ml_init())
    # Start daily report scheduler task
    asyncio.create_task(daily_report_cron())

    async with websockets.serve(
        handle_client,
        WS_HOST,
        WS_PORT,
        origins=None,
        process_request=http_process_request,
    ):
        log.info("WebSocket server listening on ws://%s:%d", WS_HOST, WS_PORT)
        try:
            # Keep serving client sockets indefinitely
            await asyncio.Event().wait()
        except (asyncio.CancelledError, KeyboardInterrupt):
            log.info("Main loop cancelled/interrupted. Initiating shutdown...")
        finally:
            await perform_shutdown()


if __name__ == "__main__":
    asyncio.run(main())
