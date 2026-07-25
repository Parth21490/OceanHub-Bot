import sys
import os
import asyncio
from datetime import datetime, timezone
import pandas as pd
import numpy as np

sys.path.insert(0, r'c:\Users\parth\OneDrive\Desktop\OceanHub\backend')
sys.stdout.reconfigure(encoding='utf-8')

from master_agent import (
    MarketData, ExecutionPipeline, CostAnalyzer, TradeSignal,
    ensemble_probabilities, render_cycle_log, run_master_agent
)

async def test_telemetry_fixes():
    print("═══════════════════════════════════════════════════════")
    print("Testing Telemetry & Pipeline Fixes")
    print("═══════════════════════════════════════════════════════")

    # 1. Test Unified Logging Handler & Sub-Agent Vote Suppressing
    state_empty_sub = {
        "symbol": "BTC/USDT",
        "current_price": 60000.0,
        "atr_val": 300.0,
        "atr_pct": 0.005,
        "market_regime": "TRENDING_UP",
        "spread_pct": 0.0001,
        "bid_vol": 500000.0,
        "ask_vol": 500000.0,
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
        "stop_loss": 59000.0,
        "take_profit": 62000.0
    }
    lines = render_cycle_log(state_empty_sub)
    print("\n--- Rendered Lines (Empty Sub-agents) ---")
    for l in lines:
        print(l)
    
    # Assert sub-agent header is suppressed
    assert not any("Sub-agent votes" in l for l in lines), "Sub-agent header should be suppressed when list is empty!"
    assert any("Market Regime: TRENDING_UP" in l for l in lines), "Market Regime line must be present!"
    assert any("Cost Analyzer Gate: PASS" in l for l in lines), "Cost Analyzer Gate line must be present!"

    state_with_sub = dict(state_empty_sub)
    state_with_sub["sub_agent_results"] = [{"agent": "MacroAgent", "vote": "LONG", "confidence": 0.85}]
    lines_sub = render_cycle_log(state_with_sub)
    assert any("Sub-agent votes" in l for l in lines_sub), "Sub-agent header should be present when sub-agents exist!"
    assert any("MacroAgent" in l for l in lines_sub), "MacroAgent vote must be rendered!"
    print("1. Unified Logging & Sub-agent rendering tests PASSED!")

    # 2. Test Slippage Calculation against Notional Size (Margin * Leverage)
    cost = CostAnalyzer()
    mid = 2000.0 # ETH price
    depth_ask = 50000.0 # $50k depth
    orderbook = {
        'bids': [[mid * 0.999, depth_ask / mid]],
        'asks': [[mid * 1.001, depth_ask / mid]],
        'funding_rate': 0.0
    }
    # Margin = $2.50, Leverage = 5x -> Notional Order Size = $12.50
    notional_size = 2.50 * 5.0
    f_res = cost.calculate_friction(orderbook, direction='LONG', order_size_usd=notional_size)
    print(f"\n2. Notional Order Size: ${notional_size:.2f} | Slippage: {f_res['impact']:.6f} ({f_res['impact']:.4%})")
    assert f_res['impact'] > 0, "Slippage must be > 0 for non-zero order size"

    # 3. Test A/B Ensemble Blending
    prob_a = {"UPTREND": 0.90, "DOWNTREND": 0.05, "RANGING": 0.05}
    prob_b = {"UPTREND": 0.10, "DOWNTREND": 0.80, "RANGING": 0.10}
    blended = ensemble_probabilities(prob_a, prob_b, weight_a=0.5, weight_b=0.5)
    print(f"\n3. Ensemble Blending -> Model A: {prob_a['UPTREND']:.0%} | Model B: {prob_b['UPTREND']:.0%} | Blended P(Up): {blended['UPTREND']:.1%}")
    assert abs(blended['UPTREND'] - 0.50) < 1e-6, f"Expected 50.0% P(Up), got {blended['UPTREND']:.1%}"
    assert abs(blended['DOWNTREND'] - 0.425) < 1e-6, f"Expected 42.5% P(Down), got {blended['DOWNTREND']:.1%}"
    print("A/B Ensemble Blending test PASSED!")

    # 4. Test Active Position SL/TP Preservation
    data_active = MarketData(
        asset="ETH/USDT",
        price=2000.0,
        atr_raw=40.0,
        atr_pct=0.02,
        spread_pct=0.0002,
        bid_depth=100.0,
        ask_depth=100.0,
        funding_rate=0.0001,
        features=np.array([25.0, 0.01, 0.5]),
        rolling_avg_atr=40.0,
        regime="TRENDING_UP",
        current_position={
            "direction": "LONG",
            "entry_price": 1980.0,
            "stop_loss": 1940.0,
            "take_profit": 2080.0,
            "margin": 2.50,
            "leverage": 5.0,
            "size": 0.00625
        }
    )
    pipeline = ExecutionPipeline(ml_brain=None, regime_shield=None)
    sig = await pipeline.run_cycle(data_active)
    print(f"\n4. Active Position Signal -> Rejection: {sig.rejection_reason} | SL: {sig.stop_loss} | TP: {sig.take_profit}")
    assert sig.stop_loss == 1940.0, f"Expected active SL 1940.0, got {sig.stop_loss}"
    assert sig.take_profit == 2080.0, f"Expected active TP 2080.0, got {sig.take_profit}"
    assert sig.margin == 2.50, f"Expected active margin 2.50, got {sig.margin}"
    assert sig.leverage == 5.0, f"Expected active leverage 5.0, got {sig.leverage}"
    print("Active Position SL/TP Preservation test PASSED!")

    print("\n═══════════════════════════════════════════════════════")
    print("ALL TELEMETRY FIXES VERIFIED SUCCESSFULLY!")
    print("═══════════════════════════════════════════════════════")

if __name__ == "__main__":
    asyncio.run(test_telemetry_fixes())
