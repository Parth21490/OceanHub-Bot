import sys
import os
import asyncio
from datetime import datetime, timezone
import pandas as pd
import numpy as np

sys.path.insert(0, r'c:\Users\parth\OneDrive\Desktop\OceanHub\backend')

from master_agent import MarketData, ExecutionPipeline, CostAnalyzer, TradeSignal

async def test_friction():
    print("Testing Total Friction Calculation...")
    
    # 1. Test CostAnalyzer calculate_friction directly with 0.0087% spread
    cost = CostAnalyzer()
    mid = 1.00
    spread_pct = 0.00087 # 0.087% spread (or 0.000087)
    best_bid = mid * (1.0 - spread_pct / 2.0)
    best_ask = mid * (1.0 + spread_pct / 2.0)
    
    orderbook = {
        'bids': [[best_bid, 10000.0], [best_bid * 0.999, 10000.0]],
        'asks': [[best_ask, 10000.0], [best_ask * 1.001, 10000.0]],
        'funding_rate': 0.0001
    }
    
    res = cost.calculate_friction(orderbook, direction='LONG', order_size_usd=2.50)
    print("CostAnalyzer result:", res)
    assert isinstance(res['total'], float), f"Expected float total, got {type(res['total'])}"
    assert res['total'] > 0, f"Expected total > 0, got {res['total']}"
    assert not isinstance(res['total'], int), "Total friction should not be an integer"
    print(f"Direct CostAnalyzer total friction: {res['total']:.6f} ({res['total']:.4%})")

    # 2. Test ExecutionPipeline run_cycle with CHOPPY regime & active position
    pipeline = ExecutionPipeline(ml_brain=None, regime_shield=None)
    data = MarketData(
        asset="XRP/USDT",
        price=1.00,
        atr_raw=0.02,
        atr_pct=0.02,
        spread_pct=0.00087, # 0.087% live spread
        bid_depth=10000.0,
        ask_depth=10000.0,
        funding_rate=0.0001,
        features=np.array([25.0, 0.01, 0.5]),
        rolling_avg_atr=0.02,
        regime="CHOPPY" # CHOPPY regime!
    )
    
    sig_choppy = await pipeline.run_cycle(data)
    print(f"CHOPPY regime signal total_friction: {sig_choppy.total_friction:.6f} ({sig_choppy.total_friction:.4%})")
    assert isinstance(sig_choppy.total_friction, float), "total_friction must be float"
    assert sig_choppy.total_friction > 0, f"Expected > 0 friction, got {sig_choppy.total_friction}"
    
    # 3. Test active position state check
    data_active = MarketData(
        asset="XRP/USDT",
        price=1.00,
        atr_raw=0.02,
        atr_pct=0.02,
        spread_pct=0.00087,
        bid_depth=10000.0,
        ask_depth=10000.0,
        funding_rate=0.0001,
        features=np.array([25.0, 0.01, 0.5]),
        rolling_avg_atr=0.02,
        regime="TRENDING_UP",
        current_position={"direction": "LONG", "stop_loss": 0.90, "take_profit": 1.10}
    )
    sig_active = await pipeline.run_cycle(data_active)
    print(f"ACTIVE POSITION signal total_friction: {sig_active.total_friction:.6f} ({sig_active.total_friction:.4%})")
    assert isinstance(sig_active.total_friction, float), "total_friction must be float"
    assert sig_active.total_friction > 0, f"Expected > 0 friction, got {sig_active.total_friction}"

    print("ALL FRICTION TESTS PASSED!")

if __name__ == "__main__":
    asyncio.run(test_friction())
