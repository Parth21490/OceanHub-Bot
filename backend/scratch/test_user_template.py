import sys
import os
import asyncio
import pandas as pd
import numpy as np

sys.path.insert(0, r'c:\Users\parth\OneDrive\Desktop\OceanHub\backend')
sys.stdout.reconfigure(encoding='utf-8')

from tools import calculate_position_exits, get_tick_decimals
from master_agent import RiskEngine, render_cycle_log

def test_template_architecture():
    print("═══════════════════════════════════════════════════════")
    print("Testing Exact User Template Architecture & Fixes")
    print("═══════════════════════════════════════════════════════")

    # BUG 1 FIX VERIFICATION: calculate_position_exits
    sl_eth, tp_eth = calculate_position_exits(1960.997166, "LONG", 29.4149, "ETH/USDT")
    print(f"\n1. BUG 1 FIX — calculate_position_exits:")
    print(f"   ETH/USDT -> Entry: 1960.997166 | SL: {sl_eth} ({type(sl_eth).__name__}) | TP: {tp_eth} ({type(tp_eth).__name__})")
    assert sl_eth == 1916.87, f"Expected 1916.87, got {sl_eth}"
    assert tp_eth == 2049.24, f"Expected 2049.24, got {tp_eth}"
    assert isinstance(sl_eth, float), "SL must be a float object"
    assert isinstance(tp_eth, float), "TP must be a float object"

    sl_xrp, tp_xrp = calculate_position_exits(0.54321, "SHORT", 0.01234, "XRP/USDT")
    print(f"   XRP/USDT -> Entry: 0.54321 | SL: {sl_xrp} ({type(sl_xrp).__name__}) | TP: {tp_xrp} ({type(tp_xrp).__name__})")
    assert sl_xrp == 0.5617, f"Expected 0.5617, got {sl_xrp}"
    assert tp_xrp == 0.5062, f"Expected 0.5062, got {tp_xrp}"

    # DOGE/USDT (5 decimals)
    sl_doge, tp_doge = calculate_position_exits(0.1234567, "LONG", 0.0054321, "DOGE/USDT")
    print(f"   DOGE/USDT -> Entry: 0.1234567 | SL: {sl_doge} ({type(sl_doge).__name__}) | TP: {tp_doge} ({type(tp_doge).__name__})")
    assert sl_doge == 0.11531, f"Expected 0.11531, got {sl_doge}"
    assert tp_doge == 0.13975, f"Expected 0.13975, got {tp_doge}"

    # BNB/USDT (2 decimals)
    sl_bnb, tp_bnb = calculate_position_exits(585.1234, "LONG", 5.4321, "BNB/USDT")
    print(f"   BNB/USDT -> Entry: 585.1234 | SL: {sl_bnb} ({type(sl_bnb).__name__}) | TP: {tp_bnb} ({type(tp_bnb).__name__})")
    assert sl_bnb == 576.98, f"Expected 576.98, got {sl_bnb}"
    assert tp_bnb == 601.42, f"Expected 601.42, got {tp_bnb}"

    # HYPE/USDT (2 decimals)
    sl_hype, tp_hype = calculate_position_exits(25.6789, "SHORT", 0.7891, "HYPE/USDT")
    print(f"   HYPE/USDT -> Entry: 25.6789 | SL: {sl_hype} ({type(sl_hype).__name__}) | TP: {tp_hype} ({type(tp_hype).__name__})")
    assert sl_hype == 26.86, f"Expected 26.86, got {sl_hype}"
    assert tp_hype == 23.31, f"Expected 23.31, got {tp_hype}"
    print("   BUG 1 FIX & NEW ASSET TICK DECIMALS PASSED SUCCESSFULLY!")

    # BUG 2 FIX VERIFICATION: render_cycle_log
    sample_state = {
        "symbol": "ETH/USDT",
        "timestamp": "18:03:59 UTC",
        "price": 1960.997166,
        "atr": 29.4149,
        "regime": "TRENDING_UP",
        "spread": 0.01,
        "bid_depth": 50.0,
        "ask_depth": 50.0,
        "funding_rate": 0.01,
        "friction": 0.065,
        "slippage": 0.005,
        "cost_gate": "PASS",
        "position_active": False,
        "rf_raw_up": 85.0,
        "rf_raw_down": 5.0,
        "rf_raw_range": 10.0,
        "rf_cal_up": 80.0,
        "rf_cal_down": 10.0,
        "rf_cal_range": 10.0,
        "leverage": 5.0,
        "margin": 2.50,
        "sl": sl_eth,
        "tp": tp_eth,
        "decision": "LONG",
        "confidence": 80
    }
    
    print("\n2. BUG 2 FIX — render_cycle_log template execution:")
    lines = render_cycle_log(sample_state)
    for l in lines:
        print(l)

    # Assert single rendering
    decision_headers = [l for l in lines if "▶ DECISION:" in l]
    assert len(decision_headers) == 1, f"Expected 1 decision line, found {len(decision_headers)}"
    print("\n   BUG 2 FIX PASSED SUCCESSFULLY!")

    print("\n═══════════════════════════════════════════════════════")
    print("ALL TESTS PASSED SUCCESSFULLY!")
    print("═══════════════════════════════════════════════════════")

if __name__ == "__main__":
    test_template_architecture()
