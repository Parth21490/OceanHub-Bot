"""
OceanHub Phase 0 — Dry Run Configuration
==========================================

Global parameters for Phase 0 Dry Run Mode
"""

# ============================================================================
# PHASE 0 DRY RUN MODE
# ============================================================================
DRY_RUN_MODE = True  # Set to True to prevent any order submission to Bybit API
DEBUG_LOGGING = True  # Verbose logging for all calculations and rationale


# ============================================================================
# TRIPLE BARRIER LABELING PARAMETERS (ml_brain.py)
# ============================================================================
TIME_BARRIER_BARS = 24                    # 24 1-hour bars (1 full trading day)
ATR_STOP_MULTIPLIER = 1.2                 # Stop Loss = 1.2 * ATR
DYNAMIC_TP_MULTIPLIER = 1.8               # Take Profit = 1.8 * ATR (1.5:1 R:R)

# Tie-breaker logic when multiple barriers hit in one bar:
# - LONG signals: Prioritize Upper barrier (TP) over Lower (SL)
# - SHORT signals: Prioritize Lower barrier (TP) over Upper (SL)
TIEBREAKER_LONG = "upper_tp"              # For LONG positions
# BUG-03 FIX: Was "lower_sl" — which recorded the SL bar as the hit, mislabeling winning SHORTs as RANGING
# Changed to "lower_tp" to prioritize the TP win on tie (same logic as LONG)
TIEBREAKER_SHORT = "lower_tp"            # For SHORT positions — prioritize TP hit on ties
CONSERVATIVE_LABELING = True              # Use conservative labeling to reduce noise


# ============================================================================
# K-MEANS MARKET REGIME DEFINITION (server.py)
# ============================================================================
ADX_CHOPPY_THRESHOLD = 20                 # ADX < 20 = Choppy market
ROLLING_VOL_THRESHOLD = 0.005             # Rolling Volatility < 0.5% = Choppy
ROLLING_VOL_WINDOW = 20                   # Calculate volatility over 20 bars

MARKET_REGIMES = {
    "CHOPPY": {"adx_max": 20, "vol_max": 0.005},
    "TRENDING": {"adx_min": 20, "vol_min": 0.005},
}


# ============================================================================
# CALIBRATION PARAMETERS (server.py)
# ============================================================================
USE_CALIBRATED_CLASSIFIERS = True         # Use CalibratedClassifierCV per-asset
CALIBRATION_METHOD = "sigmoid"            # or "isotonic"
CALIBRATION_CV_FOLDS = 5                  # Cross-validation folds for calibration

# Per-asset calibration instances (separate for each symbol)
CALIBRATED_SYMBOLS = {
    "BTC/USDT": {"use_calibration": True, "cv": CALIBRATION_CV_FOLDS},
    "ETH/USDT": {"use_calibration": True, "cv": CALIBRATION_CV_FOLDS},
    "SOL/USDT": {"use_calibration": True, "cv": CALIBRATION_CV_FOLDS},
    "BNB/USDT": {"use_calibration": True, "cv": CALIBRATION_CV_FOLDS},
    "HYPE/USDT": {"use_calibration": True, "cv": CALIBRATION_CV_FOLDS},
    "XRP/USDT": {"use_calibration": True, "cv": CALIBRATION_CV_FOLDS},
    "ADA/USDT": {"use_calibration": True, "cv": CALIBRATION_CV_FOLDS},
    "DOGE/USDT": {"use_calibration": True, "cv": CALIBRATION_CV_FOLDS},
}


# ============================================================================
# EXECUTION PARAMETERS
# ============================================================================
SIMULATION_MODE = True                    # Simulation-only execution
LEVERAGE_CAP = 3.0                        # Max leverage: 3x
POSITION_SIZE_RATIO = 0.5                 # Risk 0.5% of account per trade

# Stop-loss calculation
STOP_LOSS_TYPE = "atr_based"              # or "percentage"
PERCENTAGE_STOP_LOSS = 0.02               # 2% hard stop if not using ATR

# Take-profit calculation
TAKE_PROFIT_TYPE = "atr_based"            # or "percentage"
PERCENTAGE_TAKE_PROFIT = 0.06             # 6% TP if not using ATR (3:1 R:R)


# ============================================================================
# LOGGING & DEBUG
# ============================================================================
LOG_RATIONALE = True                      # Log decision rationale
LOG_BARRIER_CALCS = True                  # Log triple barrier calculations
LOG_REGIME_SWITCHES = True                # Log market regime changes
LOG_CALIBRATION_SCORES = True             # Log calibrated probability scores


# ============================================================================
# UI PARAMETERS (App.jsx)
# ============================================================================
SHOW_DRY_RUN_WATERMARK = True             # Display "DRY RUN ACTIVE" on dashboard
CHOP_REGIME_OVERRIDE_MATRIX = True        # CHOP state overrides trading matrix
SHOW_REGIME_BADGE = True                  # Show market regime badge
