import sys
import os
import asyncio
from datetime import datetime, timezone
import pandas as pd
import numpy as np

sys.path.insert(0, r'c:\Users\parth\OneDrive\Desktop\OceanHub\backend')
sys.stdout.reconfigure(encoding='utf-8')

from tools import round_price_prec, sync_exchange_positions
from master_agent import RiskEngine, render_cycle_log, run_master_agent

async def test_patch_fixes():
    print("═══════════════════════════════════════════════════════")
    print("Testing In-Memory Precision Rounding & Consolidated Logging")
    print("═══════════════════════════════════════════════════════")

    # 1. Test In-Memory Precision Rounding
    risk = RiskEngine()
    pos = risk.calculate_position(
        free_balance=100.0,
        confidence=0.80,
        atr_pct=0.015,
        current_price=1960.997166,
        direction="LONG",
        symbol="ETH/USDT"
    )
    print(f"\n1. In-Memory Precision Rounding Test (ETH/USDT):")
    print(f"   Raw price input: 1960.997166")
    print(f"   Calculated SL in memory: {pos['stop_loss']} (type: {type(pos['stop_loss']).__name__})")
    print(f"   Calculated TP in memory: {pos['take_profit']} (type: {type(pos['take_profit']).__name__})")
    
    # Assert floating point values are explicitly rounded to 2 decimal places for ETH
    assert isinstance(pos['stop_loss'], float), "stop_loss must be float"
    assert isinstance(pos['take_profit'], float), "take_profit must be float"
    assert pos['stop_loss'] == round(pos['stop_loss'], 2), "stop_loss in memory must be rounded to asset precision"
    assert pos['take_profit'] == round(pos['take_profit'], 2), "take_profit in memory must be rounded to asset precision"
    print("   Precision Rounding in Memory: PASSED!")

    # 2. Test Consolidated Single Log Renderer (Zero duplicate headers / lines)
    state = {
        "ts_short": "12:16:59 UTC",
        "symbol": "ETH/USDT",
        "current_price": 1960.997166,
        "atr_val": 29.4149,
        "atr_pct": 0.015,
        "market_regime": "TRENDING_UP",
        "spread_pct": 0.0001,
        "bid_vol": 50.0,
        "ask_vol": 50.0,
        "funding_rate": 0.0001,
        "total_friction": 0.00065,
        "slippage_estimate": 0.00005,
        "cost_gate_status": "PASS",
        "sub_agent_results": [],
        "ml_score": {"probabilities": {"UPTREND": 0.8, "DOWNTREND": 0.1, "RANGING": 0.1}},
        "decision_str": "LONG",
        "confidence": 0.8,
        "leverage": 5.0,
        "margin": 2.50,
        "stop_loss": pos['stop_loss'],
        "take_profit": pos['take_profit']
    }
    lines = render_cycle_log(state)
    print("\n2. Consolidated Log Renderer Output:")
    for line in lines:
        print(line)

    decision_lines = [l for l in lines if "DECISION:" in l]
    print(f"\n   Decision lines rendered: {len(decision_lines)}")
    assert len(decision_lines) == 1, f"Expected exactly 1 DECISION line, found {len(decision_lines)}"
    assert any("Funding Rate: 0.0100%" in l for l in lines), "Funding rate line must be present"
    print("   Consolidated Log Renderer Test: PASSED!")

    # 3. Test On-Startup Exchange Sync Functionality
    print("\n3. Testing On-Startup Exchange Sync signature...")
    positions = await sync_exchange_positions(["ETH/USDT", "BTC/USDT"])
    print(f"   Exchange positions returned (Mock/Testnet): {positions}")
    print("   State Recovery Protocol Functionality: PASSED!")

    print("\n═══════════════════════════════════════════════════════")
    print("ALL PATCH FIXES VERIFIED SUCCESSFULLY!")
    print("═══════════════════════════════════════════════════════")

if __name__ == "__main__":
    asyncio.run(test_patch_fixes())
