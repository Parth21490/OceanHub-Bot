import React, { useState, useEffect } from 'react'
import { MasterAICore } from './MasterAICore'

const DEFAULT_ASSETS = {
  'BTC/USDT': { decision: 'HOLD', confidence: 0.0, leverage: 1, stop_loss: null, p_up: 0.33, p_down: 0.33, p_range: 0.34, price: 0.0 },
  'ETH/USDT': { decision: 'HOLD', confidence: 0.0, leverage: 1, stop_loss: null, p_up: 0.33, p_down: 0.33, p_range: 0.34, price: 0.0 },
  'SOL/USDT': { decision: 'HOLD', confidence: 0.0, leverage: 1, stop_loss: null, p_up: 0.33, p_down: 0.33, p_range: 0.34, price: 0.0 },
  'BNB/USDT': { decision: 'HOLD', confidence: 0.0, leverage: 1, stop_loss: null, p_up: 0.33, p_down: 0.33, p_range: 0.34, price: 0.0 },
  'HYPE/USDT': { decision: 'HOLD', confidence: 0.0, leverage: 1, stop_loss: null, p_up: 0.33, p_down: 0.33, p_range: 0.34, price: 0.0 },
  'XRP/USDT': { decision: 'HOLD', confidence: 0.0, leverage: 1, stop_loss: null, p_up: 0.33, p_down: 0.33, p_range: 0.34, price: 0.0 },
  'ADA/USDT': { decision: 'HOLD', confidence: 0.0, leverage: 1, stop_loss: null, p_up: 0.33, p_down: 0.33, p_range: 0.34, price: 0.0 },
  'DOGE/USDT': { decision: 'HOLD', confidence: 0.0, leverage: 1, stop_loss: null, p_up: 0.33, p_down: 0.33, p_range: 0.34, price: 0.0 }
}

export default function HomeDashboard({ ws, dynamicAsset, onSelectAsset }) {
  const [assets, setAssets] = useState(DEFAULT_ASSETS)
  const [tradeLedger, setTradeLedger] = useState([])
  const [wallet, setWallet] = useState({
    balance: 10.00,
    cash: 10.00,
    margin_in_use: 0.00,
    unrealized_pnl: 0.00
  })

  // Parse incoming multi-asset unified state updates
  useEffect(() => {
    if (!ws.lastMessage) return
    try {
      const msg = JSON.parse(ws.lastMessage.data)
      if (msg.type === 'unified_state' && msg.data) {
        setAssets((prev) => {
          // Merge incoming asset states safely with fallbacks
          const next = { ...prev }
          Object.entries(msg.data.assets || {}).forEach(([sym, val]) => {
            next[sym] = {
              ...prev[sym],
              ...val
            }
          })
          return next
        })
        setTradeLedger(msg.data.trade_ledger || [])
        if (msg.data.wallet) {
          setWallet(msg.data.wallet)
        }
      }
    } catch (e) {
      console.error('Failed to parse unified state:', e)
    }
  }, [ws.lastMessage])

  const handleLaunchTabs = () => {
    const tickers = Object.keys(DEFAULT_ASSETS).map(s => s.split('/')[0])
    tickers.forEach(t => window.open(`/?ticker=${t}`, '_blank'))
  }

  useEffect(() => {
    const hasLaunched = sessionStorage.getItem('has_launched_workspace')
    if (!hasLaunched) {
      sessionStorage.setItem('has_launched_workspace', 'true')
      handleLaunchTabs()
    }
  }, [])

  return (
    <div className="flex-1 flex flex-col min-h-0 gap-3 p-3 overflow-y-auto custom-scrollbar bg-[#080b11]">
      
      {/* ── WALLET OVERVIEW WIDGET ── */}
      <div className="grid grid-cols-4 gap-3 shrink-0">
        {/* Total Balance Card */}
        <div className="flex flex-col p-3 rounded-xl border border-white/5 bg-white/[0.02] backdrop-blur-md shadow-lg">
          <span className="text-[8px] font-bold font-mono tracking-wider text-purple-300 uppercase">Total Account Balance</span>
          <span className="text-lg font-bold font-mono text-white mt-1">
            ${wallet.balance.toFixed(2)}
          </span>
          <span className="text-[7px] font-mono text-white/40 mt-1">Cash + Active Unrealized PnL</span>
        </div>

        {/* Cash Balance Card */}
        <div className="flex flex-col p-3 rounded-xl border border-white/5 bg-white/[0.02] backdrop-blur-md shadow-lg">
          <span className="text-[8px] font-bold font-mono tracking-wider text-cyan-300 uppercase">Available Cash</span>
          <span className="text-lg font-bold font-mono text-white mt-1">
            ${wallet.cash.toFixed(2)}
          </span>
          <span className="text-[7px] font-mono text-white/40 mt-1">Free Capital (Initial $40.00)</span>
        </div>

        {/* Margin in Use Card */}
        <div className="flex flex-col p-3 rounded-xl border border-white/5 bg-white/[0.02] backdrop-blur-md shadow-lg">
          <span className="text-[8px] font-bold font-mono tracking-wider text-yellow-300 uppercase">Margin in Use</span>
          <span className="text-lg font-bold font-mono text-white mt-1">
            ${wallet.margin_in_use.toFixed(2)}
          </span>
          <span className="text-[7px] font-mono text-white/40 mt-1">Dynamic Confidence-Weighted Margin</span>
        </div>

        {/* Real-time Active PnL Card */}
        <div className="flex flex-col p-3 rounded-xl border border-white/5 bg-white/[0.02] backdrop-blur-md shadow-lg">
          <span className="text-[8px] font-bold font-mono tracking-wider text-rose-300 uppercase">Active Unrealized PnL</span>
          <span className={`text-lg font-bold font-mono mt-1 ${wallet.unrealized_pnl >= 0 ? 'text-emerald-400' : 'text-rose-400'}`}>
            {wallet.unrealized_pnl >= 0 ? '+' : ''}${wallet.unrealized_pnl.toFixed(2)}
          </span>
          <span className="text-[7px] font-mono text-white/40 mt-1">Live Valuation Return</span>
        </div>
      </div>

      {/* ── DYNAMIC TEMPORARY AGENT SEARCH RESULT CARD ── */}
      {dynamicAsset && dynamicAsset.symbol && (
        <div className="p-3 rounded-xl border border-purple-500/30 bg-purple-950/20 backdrop-blur-md flex flex-col gap-2 shadow-[0_0_15px_rgba(168,85,247,0.1)]">
          <div className="flex items-center justify-between border-b border-purple-500/20 pb-2">
            <div className="flex items-center gap-2">
              <span className="px-2 py-0.5 rounded bg-purple-500/20 text-purple-300 text-[9px] font-bold font-mono tracking-widest uppercase border border-purple-500/40">
                ⚡ DYNAMIC TEMPORARY AGENT
              </span>
              <span className="text-sm font-bold font-mono text-white">{dynamicAsset.symbol}</span>
              <span className="text-[9px] font-mono text-purple-300/60">
                Regime: {dynamicAsset.regime}
              </span>
            </div>
            <div className="flex items-center gap-3">
              <span className="text-xs font-bold font-mono text-cyan-300">
                ${dynamicAsset.price ? dynamicAsset.price.toLocaleString() : 'N/A'}
              </span>
              <button
                onClick={() => onSelectAsset && onSelectAsset(dynamicAsset.symbol)}
                className="px-2.5 py-1 rounded text-[8px] font-bold font-mono uppercase tracking-wider bg-purple-500/30 text-purple-200 border border-purple-500/50 hover:bg-purple-500/50 transition-all"
              >
                OPEN TEMPORARY TAB ➔
              </button>
            </div>
          </div>

          <div className="grid grid-cols-6 gap-2 text-[9px] font-mono pt-1">
            <div className="flex flex-col bg-white/[0.02] p-2 rounded border border-white/5">
              <span className="text-white/40 text-[7px] uppercase">Decision</span>
              <span className={`font-bold mt-0.5 ${dynamicAsset.decision === 'LONG' ? 'text-emerald-400' : dynamicAsset.decision === 'SHORT' ? 'text-rose-400' : 'text-slate-400'}`}>
                {dynamicAsset.decision}
              </span>
            </div>

            <div className="flex flex-col bg-white/[0.02] p-2 rounded border border-white/5">
              <span className="text-white/40 text-[7px] uppercase">Confidence</span>
              <span className="font-bold text-white mt-0.5">
                {((dynamicAsset.confidence || 0) * 100).toFixed(1)}%
              </span>
            </div>

            <div className="flex flex-col bg-white/[0.02] p-2 rounded border border-white/5">
              <span className="text-white/40 text-[7px] uppercase">Leverage</span>
              <span className="font-bold text-purple-300 mt-0.5">
                {dynamicAsset.leverage}X
              </span>
            </div>

            <div className="flex flex-col bg-white/[0.02] p-2 rounded border border-white/5">
              <span className="text-white/40 text-[7px] uppercase">Stop Loss</span>
              <span className="font-bold text-rose-300 mt-0.5 truncate">
                {dynamicAsset.stop_loss ? `$${dynamicAsset.stop_loss}` : 'NONE'}
              </span>
            </div>

            <div className="flex flex-col bg-white/[0.02] p-2 rounded border border-white/5">
              <span className="text-white/40 text-[7px] uppercase">Take Profit</span>
              <span className="font-bold text-emerald-300 mt-0.5 truncate">
                {dynamicAsset.take_profit ? `$${dynamicAsset.take_profit}` : 'NONE'}
              </span>
            </div>

            <div className="flex flex-col bg-white/[0.02] p-2 rounded border border-white/5">
              <span className="text-white/40 text-[7px] uppercase">Est. Duration</span>
              <span className="font-bold text-cyan-300 mt-0.5">
                ~{dynamicAsset.expected_bars} bars ({dynamicAsset.expected_duration_mins}m)
              </span>
            </div>
          </div>
        </div>
      )}

      {/* ── TOP ROW: MINI SIGNAL MATRIX GRID ── */}
      <div className="grid grid-cols-5 gap-3 shrink-0">
        {Object.entries(assets).map(([sym, data]) => {
          const isLong = data.decision === 'LONG'
          const isShort = data.decision === 'SHORT'
          const badgeBg = isLong ? 'rgba(16, 185, 129, 0.15)' : isShort ? 'rgba(244, 63, 94, 0.15)' : 'rgba(255, 255, 255, 0.06)'
          const badgeText = isLong ? '#10b981' : isShort ? '#f43f5e' : '#94a3b8'
          const borderHighlight = isLong ? 'border-emerald-500/20 shadow-[0_0_12px_rgba(16,185,129,0.06)]' : isShort ? 'border-rose-500/20 shadow-[0_0_12px_rgba(244,63,94,0.06)]' : 'border-white/5'

          return (
            <div
              key={sym}
              onClick={() => onSelectAsset && onSelectAsset(sym)}
              className={`flex flex-col p-3 rounded-xl border bg-white/[0.02] backdrop-blur-md transition-all duration-300 cursor-pointer hover:border-cyan-500/40 hover:bg-white/[0.04] ${borderHighlight}`}
              title={`Click to view ${sym} chart and AI core`}
            >
              {/* Asset Name and Current Price */}
              <div className="flex items-center justify-between border-b border-white/[0.04] pb-2 mb-2">
                <div className="flex items-center gap-1.5">
                  <span className="text-[10px] font-bold font-mono tracking-wider text-white/55">{sym}</span>
                  <span
                    className="px-1 py-0.2 rounded text-[6px] font-bold font-mono uppercase tracking-wider border"
                    style={{
                      color: data.market_regime === 'Trending' ? '#10b981' : '#94a3b8',
                      borderColor: data.market_regime === 'Trending' ? 'rgba(16, 185, 129, 0.25)' : 'rgba(255, 255, 255, 0.08)',
                      backgroundColor: data.market_regime === 'Trending' ? 'rgba(16, 185, 129, 0.1)' : 'rgba(255, 255, 255, 0.04)'
                    }}
                  >
                    {data.market_regime || 'Choppy'}
                  </span>
                </div>
                <span className="text-[11px] font-bold font-mono text-cyan-300">
                  {data.price > 0 ? `$${data.price.toLocaleString(undefined, { minimumFractionDigits: sym.includes('XRP') ? 4 : 2 })}` : 'Loading...'}
                </span>
              </div>

              {/* Decision Badge & Confidence or Chop Override */}
              {data.market_regime === 'Choppy' ? (
                <div className="flex-1 flex flex-col items-center justify-center bg-amber-500/[0.04] border border-amber-500/10 rounded-lg p-2.5 my-1.5 text-center min-h-[110px] animate-pulse">
                  <span className="text-base mb-1">🛡️</span>
                  <span className="text-[8px] font-bold text-amber-400 tracking-wider uppercase font-mono">
                    CHOP REGIME ACTIVE
                  </span>
                  <span className="text-[6px] text-white/40 font-mono mt-0.5 uppercase tracking-wide">
                    CAPITAL PRESERVATION
                  </span>
                </div>
              ) : (
                <>
                  <div className="flex items-center justify-between mb-2.5">
                    <span
                      className="px-2 py-0.5 rounded text-[8px] font-bold font-mono tracking-widest uppercase"
                      style={{ backgroundColor: badgeBg, color: badgeText }}
                    >
                      {data.decision}
                    </span>
                    <div className="flex items-center gap-1.5 text-[9px] font-mono text-white/60">
                      <span>Conf:</span>
                      <span className="font-bold">{((data.confidence || 0) * 100).toFixed(0)}%</span>
                    </div>
                  </div>

                  {/* Confidence progress bar */}
                  <div className="w-full h-1 bg-white/5 rounded-full overflow-hidden mb-3">
                    <div
                      className="h-full rounded-full transition-all duration-500"
                      style={{
                        width: `${(data.confidence || 0) * 100}%`,
                        backgroundColor: isLong ? '#10b981' : isShort ? '#f43f5e' : '#94a3b8'
                      }}
                    />
                  </div>

                  {/* Leverage, Stop Loss, Take Profit, and Est. Time tags */}
                  <div className="grid grid-cols-2 gap-1.5 text-[7.5px] font-mono text-white/40 mb-2">
                    <div className="px-1 py-0.5 rounded bg-purple-500/10 border border-purple-500/20 text-purple-300 text-center">
                      LEV: {data.leverage}X
                    </div>
                    <div className="px-1 py-0.5 rounded bg-cyan-500/10 border border-cyan-500/20 text-cyan-300 text-center truncate">
                      TIME: {data.expected_duration_mins ? `${data.expected_duration_mins}m` : '0m'}
                    </div>
                    <div className="px-1 py-0.5 rounded bg-rose-500/10 border border-rose-500/20 text-rose-300 text-center truncate">
                      SL: {data.stop_loss ? `$${data.stop_loss}` : 'NONE'}
                    </div>
                    <div className="px-1 py-0.5 rounded bg-emerald-500/10 border border-emerald-500/20 text-emerald-300 text-center truncate">
                      TP: {data.take_profit ? `$${data.take_profit}` : 'NONE'}
                    </div>
                  </div>
                </>
              )}

              {/* Regime Probabilities Distribution */}
              <div className="space-y-1.5 pt-2 border-t border-white/[0.04]">
                <div className="flex items-center justify-between text-[7px] font-mono text-white/30 uppercase tracking-widest">
                  <span>UP</span>
                  <span>RNG</span>
                  <span>DN</span>
                </div>
                <div className="flex items-center justify-between gap-1 text-[8px] font-mono text-white/70">
                  <span className="text-emerald-400">{((data.p_up || 0) * 100).toFixed(0)}%</span>
                  <span className="text-gray-400">{((data.p_range || 0) * 100).toFixed(0)}%</span>
                  <span className="text-rose-400">{((data.p_down || 0) * 100).toFixed(0)}%</span>
                </div>
              </div>
            </div>
          )
        })}
      </div>

      {/* ── BOTTOM ROW: LEDGER (60%) & MASTER CORE TERMINAL (40%) ── */}
      <div className="flex gap-3 shrink-0 min-h-[800px]">
        
        {/* GLOBAL TRADE LEDGER (60% Width) */}
        <div className="w-[60%] flex flex-col min-h-0 rounded-xl border border-purple-500/20 bg-[#070a12] shadow-2xl overflow-hidden relative h-full">
          <div className="pointer-events-none absolute inset-0 scanlines z-10 opacity-[0.06]" />
          
          {/* Ledger Header */}
          <div
            className="relative z-20 shrink-0 px-4 py-2 border-b border-purple-500/15"
            style={{
              background: 'linear-gradient(135deg, rgba(168,85,247,0.12) 0%, rgba(236,72,153,0.06) 100%)'
            }}
          >
            <h2 className="text-[11px] font-bold tracking-[0.2em] uppercase text-purple-300">
              ◈ Global Trade Ledger
            </h2>
            <p className="text-[9px] text-white/35 mt-0.5 tracking-[0.15em] font-mono uppercase">
              Consolidated Algorithmic Action Logs
            </p>
          </div>

          {/* Ledger Table Container */}
          <div className="flex-1 overflow-auto bg-[#02050b]/60 custom-scrollbar relative z-20">
            {tradeLedger.length === 0 ? (
              <div className="h-full flex items-center justify-center text-white/20 italic font-mono text-[10px]">
                No trade ledger logs generated yet. Waiting for triggers...
              </div>
            ) : (
              <table className="w-full text-left font-mono text-[11px] border-collapse">
                <thead>
                  <tr className="sticky top-0 bg-[#04070d] border-b border-white/5 text-white/40 uppercase tracking-widest text-[9.5px]">
                    <th className="py-3 px-4">Time</th>
                    <th className="py-3 px-4">Pair</th>
                    <th className="py-3 px-4">Direction</th>
                    <th className="py-3 px-4 text-right">Size (Kelly)</th>
                    <th className="py-3 px-4 text-right">Entry Price</th>
                    <th className="py-3 px-4 text-right">Current Price</th>
                    <th className="py-3 px-4 text-right">TP Time</th>
                    <th className="py-3 px-4 text-right">Situation</th>
                    <th className="py-3 px-4 text-right">PNL</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-white/[0.03]">
                  {tradeLedger.map((trade, i) => {
                    const isLong = trade.direction === 'LONG'
                    const isNegative = trade.pnl.includes('-')
                    const isZero = trade.pnl.includes('0.00')
                    const pnlColor = isZero ? 'text-white/40' : (isNegative ? 'text-rose-400' : 'text-emerald-400')
                    const directionColor = isLong ? 'text-emerald-400' : 'text-rose-400'
                    const tpDurationMins = trade.expected_duration_mins || assets[trade.pair]?.expected_duration_mins || 0

                    return (
                      <tr key={i} className="hover:bg-white/[0.02] transition-colors">
                        <td className="py-3 px-4 text-white/50">{trade.time}</td>
                        <td className="py-3 px-4 font-bold text-white/80">{trade.pair}</td>
                        <td className={`py-3 px-4 font-bold ${directionColor}`}>{trade.direction}</td>
                        <td className="py-3 px-4 text-right text-purple-300 font-mono">
                          ${trade.margin ? trade.margin.toFixed(2) : '2.00'} ({((trade.kelly_conf || 0.0) * 100).toFixed(1)}%)
                        </td>
                        <td className="py-3 px-4 text-right text-white/60">
                          {trade.entry_price.toLocaleString(undefined, { minimumFractionDigits: 2 })}
                        </td>
                        <td className="py-3 px-4 text-right text-cyan-300/80">
                          {trade.current_price.toLocaleString(undefined, { minimumFractionDigits: 2 })}
                        </td>
                        <td className="py-3 px-4 text-right text-cyan-300/80 font-mono text-[10px]">
                          {tpDurationMins > 0 ? `~${tpDurationMins}m` : '-'}
                        </td>
                        <td className="py-3 px-4 text-right font-mono font-bold">
                          {trade.status === 'ACTIVE' 
                            ? <span className="text-emerald-400 tracking-widest text-[9px] uppercase border border-emerald-500/30 px-1.5 py-0.5 rounded bg-emerald-500/10">ACTIVE</span> 
                            : <span className="text-white/40 tracking-widest text-[9px] uppercase border border-white/10 px-1.5 py-0.5 rounded bg-white/5">{trade.status}</span>}
                        </td>
                        <td className={`py-3 px-4 text-right font-bold ${pnlColor}`}>{trade.pnl}</td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            )}
          </div>
        </div>

        {/* MASTER AI CORE LOG TERMINAL (40% Width) */}
        <div className="w-[40%] flex flex-col min-h-0 h-full">
          <MasterAICore ws={ws} activeSymbol="HOME" />
        </div>
      </div>
    </div>
  )
}
