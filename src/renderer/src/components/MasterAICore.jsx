import { useEffect, useRef, useState } from 'react'

const STATUS_COLORS = {
  connected:    '#10b981',
  connecting:   '#f59e0b',
  disconnected: '#6b7280',
  error:        '#f43f5e'
}

const STATUS_LABELS = {
  connected:    'LIVE',
  connecting:   'SYNC',
  disconnected: 'OFFLINE',
  error:        'ERR'
}

const BOOT_SEQUENCE = [
  'OceanHub Master AI Core v2.1.0',
  '────────────────────────────────────',
  'Initialising neural substrate......',
  'Loading market sentiment models.....',
  'Connecting to signal aggregators....',
  'Calibrating risk parameters.........',
  'Warming up execution engine.........',
  '────────────────────────────────────',
  'READY. Connected to OceanHub Master AI Engine',
  ''
]

export function MasterAICore({ ws, activeSymbol }) {
  const [lastDecision, setLastDecision] = useState({
    'BTC/USDT': null,
    'ETH/USDT': null,
    'SOL/USDT': null,
    'BNB/USDT': null,
    'HYPE/USDT': null,
    'XRP/USDT': null,
    'ADA/USDT': null,
    'DOGE/USDT': null,
    'ALL': null
  })
  const [isExecuteOn, setIsExecuteOn] = useState(false)
  const [rawLogs, setRawLogs] = useState([])
  const [signalHistory, setSignalHistory] = useState({
    'BTC/USDT': [],
    'ETH/USDT': [],
    'SOL/USDT': [],
    'BNB/USDT': [],
    'HYPE/USDT': [],
    'XRP/USDT': [],
    'ADA/USDT': [],
    'DOGE/USDT': [],
    'ALL': []
  })
  const [isBooting, setIsBooting] = useState(true)
  const logRef = useRef(null)
  const bootIdx = useRef(0)

  // Boot sequence - seed for all assets
  useEffect(() => {
    if (!isBooting) return
    const interval = setInterval(() => {
      if (bootIdx.current < BOOT_SEQUENCE.length) {
        const line = BOOT_SEQUENCE[bootIdx.current]
        setRawLogs((prev) => [...prev, line])
        bootIdx.current += 1
      } else {
        setIsBooting(false)
        clearInterval(interval)
      }
    }, 110)
    return () => clearInterval(interval)
  }, [isBooting])

  // Incoming WebSocket messages
  useEffect(() => {
    if (!ws.lastMessage) return
    try {
      const data = JSON.parse(ws.lastMessage.data)
      
      if (data.type === 'INIT_LOG_HISTORY' && Array.isArray(data.data)) {
        setIsBooting(false)
        setRawLogs(data.data)
      } else if (data.type === 'ai_thought' && data.text) {
        setRawLogs((prev) => [...prev, data.text])
      } else if (data.type === 'decision' && data.data) {
        const sym = data.symbol || 'BTC/USDT'
        const d = data.data
        
        setLastDecision((prev) => ({
          ...prev,
          [sym]: d
        }))
        
        const decisionText = `═══ DECISION: ${d.decision} (${((d.confidence||0)*100).toFixed(0)}%) ═══`
        const reasoningText = d.reasoning || ''

        setRawLogs((prev) => [
          ...prev,
          `[${sym}] ${decisionText}`,
          `[${sym}] ${reasoningText}`
        ])
        
        // Push to signal history (keep last 5)
        const newSig = {
          decision: d.decision,
          confidence: d.confidence,
          timestamp: new Date().toLocaleTimeString('en-US', { hour12: false }),
          stopLoss: d.stop_loss
        }
        setSignalHistory((prev) => ({
          ...prev,
          [sym]: [newSig, ...(prev[sym] || [])].slice(0, 5)
        }))
      } else if (data.type === 'status' && data.text) {
        setRawLogs((prev) => [...prev, `[STATUS] ${data.text}`])
      } else if (data.type === 'error' && data.text) {
        const sym = data.symbol || activeSymbol
        setRawLogs((prev) => [...prev, `[${sym}] [ERROR] ${data.text}`])
      }
    } catch {
      setRawLogs((prev) => [...prev, ws.lastMessage.data])
    }
  }, [ws.lastMessage, activeSymbol])

  // Auto-scroll
  useEffect(() => {
    if (logRef.current) {
      const timer = setTimeout(() => {
        if (logRef.current) {
          logRef.current.scrollTop = logRef.current.scrollHeight
        }
      }, 50)
      return () => clearTimeout(timer)
    }
  }, [rawLogs, activeSymbol])

  const handleToggle = () => {
    const next = !isExecuteOn
    setIsExecuteOn(next)
    ws.sendMessage({ type: 'execute', active: next, timestamp: Date.now() })
    const ts = new Date().toLocaleTimeString('en-US', { hour12: false })
    setRawLogs((prev) => [...prev, `[${ts}] ► Execute mode ${next ? 'ACTIVATED ⚡' : 'DEACTIVATED ⏸'}`])
  }

  const statusColor = STATUS_COLORS[ws.status] || STATUS_COLORS.disconnected
  const statusLabel = STATUS_LABELS[ws.status] || STATUS_LABELS.disconnected

  // Context-aware log parser and filter
  const parsedLogs = rawLogs.map((line) => {
    const match = line.match(/^\[([A-Z0-9]+\/USDT)\]\s*(.*)$/)
    if (match) {
      return { text: match[2], symbol: match[1], raw: line }
    }
    return { text: line, symbol: 'SYSTEM', raw: line }
  })

  const filteredLogs = parsedLogs.filter((logItem) => {
    if (activeSymbol === 'HOME') return true
    return logItem.symbol === activeSymbol
  })

  const activeDecision = lastDecision[activeSymbol]

  if (activeSymbol === 'HOME') {
    return (
      <div className="h-full relative flex flex-col overflow-hidden rounded-xl border border-cyan-500/20 bg-[#070a12] shadow-2xl">
        {/* Scanline overlay */}
        <div className="pointer-events-none absolute inset-0 scanlines z-10 opacity-[0.12]" />

        {/* Header */}
        <div
          className="relative z-20 shrink-0 px-4 py-2 border-b border-cyan-500/15"
          style={{
            background: 'linear-gradient(135deg, rgba(6,182,212,0.14) 0%, rgba(139,92,246,0.08) 100%)'
          }}
        >
          <div className="flex items-center justify-between">
            <div>
              <h2 className="text-[11px] font-bold tracking-[0.2em] uppercase"
                style={{ color: '#22d3ee' }}
              >
                ◈ Master AI Core
              </h2>
              <p className="text-[9px] text-white/35 mt-0.5 tracking-[0.15em] font-mono uppercase">
                Neural Process Logs — ALL ASSETS
              </p>
              {/* Visual Indicator */}
              <div className="flex items-center gap-1.5 mt-1 text-[8px] font-mono font-bold uppercase tracking-wider">
                <span className="text-white/40">Showing:</span>
                <span className="px-1.5 py-0.2 rounded bg-cyan-500/10 border border-cyan-500/30 text-cyan-300">
                  [All Assets]
                </span>
              </div>
            </div>

            <div className="flex items-center gap-2">
              {/* WS Status badge */}
              <div
                className="flex items-center gap-1.5 px-2 py-0.5 rounded-lg border text-[8px] tracking-wider"
                style={{ borderColor: statusColor + '40', background: statusColor + '12' }}
              >
                <span
                  className="w-1 h-1 rounded-full"
                  style={{
                    backgroundColor: statusColor,
                    boxShadow: `0 0 6px ${statusColor}`,
                  }}
                />
                <span className="font-mono text-white/60">{statusLabel}</span>
              </div>
            </div>
          </div>
        </div>

        {/* Logs Console */}
        <div
          ref={logRef}
          className="flex-1 overflow-y-auto p-4 font-mono text-[9px] leading-relaxed space-y-2 select-text custom-scrollbar bg-[#02050b]/80"
        >
          {filteredLogs.map((logItem, idx) => (
            <div key={idx} className="whitespace-pre-wrap break-all text-cyan-400/80">
              {logItem.raw}
            </div>
          ))}
          {/* Blinking cursor */}
          <span className="inline-block w-[6px] h-[10px] align-bottom ml-0.5 cursor-blink"
            style={{ background: '#22d3ee', boxShadow: '0 0 6px #22d3ee' }}
          />
        </div>

        {/* Execute Toggle */}
        <div className="h-px shrink-0 bg-cyan-500/15" />
        <div
          className="relative z-20 shrink-0 px-4 py-2 flex items-center justify-between"
          style={{
            background: isExecuteOn
              ? 'linear-gradient(180deg, transparent 0%, rgba(16,185,129,0.06) 100%)'
              : 'linear-gradient(180deg, transparent 0%, rgba(6,182,212,0.03) 100%)'
          }}
        >
          <div>
            <div className="text-[9px] font-semibold tracking-[0.15em] text-white/55 uppercase">
              Execute Mode
            </div>
            <div className="text-[8px] font-mono mt-0.5"
              style={{ color: isExecuteOn ? '#10b981' : 'rgba(255,255,255,0.2)' }}
            >
              {isExecuteOn ? '⚡ Orders fire on signal' : '⏸ Simulation only'}
            </div>
          </div>

          <button
            onClick={handleToggle}
            role="switch"
            aria-checked={isExecuteOn}
            aria-label="Execute mode toggle"
            className="relative cursor-pointer focus:outline-none"
          >
            <div
              className="w-10 h-5 rounded-full transition-all duration-300 border relative"
              style={{
                background: isExecuteOn
                  ? 'linear-gradient(90deg, #0891b2, #10b981)'
                  : 'rgba(255,255,255,0.06)',
                borderColor: isExecuteOn ? '#10b98150' : 'rgba(255,255,255,0.1)',
                boxShadow: isExecuteOn ? '0 0 12px rgba(16,185,129,0.3)' : 'none'
              }}
            >
              <div
                className="absolute top-0.5 w-3.5 h-3.5 rounded-full transition-all duration-300 shadow-lg"
                style={{
                  left: isExecuteOn ? '22px' : '2px',
                  background: isExecuteOn ? '#fff' : 'rgba(255,255,255,0.25)',
                }}
              />
            </div>
          </button>
        </div>
      </div>
    )
  }

  return (
    <div className="flex flex-col h-full gap-3">
      {/* ── TOP HALF: MASTER AI CORE ── */}
      <div className="flex-1 min-h-0 relative flex flex-col overflow-hidden rounded-xl border border-cyan-500/20 bg-[#070a12] shadow-2xl">
        {/* Scanline overlay */}
        <div className="pointer-events-none absolute inset-0 scanlines z-10 opacity-[0.12]" />

        {/* Header */}
        <div
          className="relative z-20 shrink-0 px-4 py-2 border-b border-cyan-500/15"
          style={{
            background: 'linear-gradient(135deg, rgba(6,182,212,0.14) 0%, rgba(139,92,246,0.08) 100%)'
          }}
        >
          <div className="flex items-center justify-between">
            <div>
              <h2 className="text-[11px] font-bold tracking-[0.2em] uppercase"
                style={{ color: '#22d3ee' }}
              >
                ◈ Master AI Core
              </h2>
              <p className="text-[9px] text-white/35 mt-0.5 tracking-[0.15em] font-mono uppercase">
                Neural Process Logs — {activeSymbol}
              </p>
              {/* Visual Indicator */}
              <div className="flex items-center gap-1.5 mt-1 text-[8px] font-mono font-bold uppercase tracking-wider">
                <span className="text-white/40">Showing:</span>
                <span className="px-1.5 py-0.2 rounded bg-cyan-500/10 border border-cyan-500/30 text-cyan-300">
                  [{activeSymbol}]
                </span>
              </div>
            </div>

            <div className="flex items-center gap-2">
              {/* Daily Report Button */}
              <button
                onClick={() => ws.sendMessage({ type: 'generate_report', symbol: activeSymbol })}
                className="px-2 py-0.5 rounded bg-cyan-500/10 border border-cyan-500/25 text-cyan-300 text-[8px] font-mono hover:bg-cyan-500/20 active:scale-95 transition-all uppercase tracking-wider cursor-pointer"
              >
                Daily Report
              </button>

              {/* WS Status badge */}
              <div
                className="flex items-center gap-1.5 px-2 py-0.5 rounded-lg border text-[8px] tracking-wider"
                style={{ borderColor: statusColor + '40', background: statusColor + '12' }}
              >
                <span
                  className="w-1 h-1 rounded-full"
                  style={{
                    backgroundColor: statusColor,
                    boxShadow: `0 0 6px ${statusColor}`,
                  }}
                />
                <span className="font-mono text-white/60">{statusLabel}</span>
              </div>
            </div>
          </div>
        </div>

        {/* Logs Console */}
        <div
          ref={logRef}
          className="flex-1 overflow-y-auto p-4 font-mono text-[9px] leading-relaxed space-y-2 select-text custom-scrollbar bg-[#02050b]/80"
        >
          {filteredLogs.map((logItem, idx) => (
            <div key={idx} className="whitespace-pre-wrap break-all text-cyan-400/80">
              {logItem.text}
            </div>
          ))}
          {/* Blinking cursor */}
          <span className="inline-block w-[6px] h-[10px] align-bottom ml-0.5 cursor-blink"
            style={{ background: '#22d3ee', boxShadow: '0 0 6px #22d3ee' }}
          />
        </div>

        {/* Execute Toggle */}
        <div className="h-px shrink-0 bg-cyan-500/15" />
        <div
          className="relative z-20 shrink-0 px-4 py-2 flex items-center justify-between"
          style={{
            background: isExecuteOn
              ? 'linear-gradient(180deg, transparent 0%, rgba(16,185,129,0.06) 100%)'
              : 'linear-gradient(180deg, transparent 0%, rgba(6,182,212,0.03) 100%)'
          }}
        >
          <div>
            <div className="text-[9px] font-semibold tracking-[0.15em] text-white/55 uppercase">
              Execute Mode
            </div>
            <div className="text-[8px] font-mono mt-0.5"
              style={{ color: isExecuteOn ? '#10b981' : 'rgba(255,255,255,0.2)' }}
            >
              {isExecuteOn ? '⚡ Orders fire on signal' : '⏸ Simulation only'}
            </div>
          </div>

          <button
            onClick={handleToggle}
            role="switch"
            aria-checked={isExecuteOn}
            aria-label="Execute mode toggle"
            className="relative cursor-pointer focus:outline-none"
          >
            <div
              className="w-10 h-5 rounded-full transition-all duration-300 border relative"
              style={{
                background: isExecuteOn
                  ? 'linear-gradient(90deg, #0891b2, #10b981)'
                  : 'rgba(255,255,255,0.06)',
                borderColor: isExecuteOn ? '#10b98150' : 'rgba(255,255,255,0.1)',
                boxShadow: isExecuteOn ? '0 0 12px rgba(16,185,129,0.3)' : 'none'
              }}
            >
              <div
                className="absolute top-0.5 w-3.5 h-3.5 rounded-full transition-all duration-300 shadow-lg"
                style={{
                  left: isExecuteOn ? '22px' : '2px',
                  background: isExecuteOn ? '#fff' : 'rgba(255,255,255,0.25)',
                }}
              />
            </div>
          </button>
        </div>
      </div>

      {/* ── BOTTOM HALF: LIVE SIGNAL MATRIX ── */}
      <div className="flex-1 min-h-0 relative flex flex-col overflow-hidden rounded-xl border border-purple-500/25 bg-[#070a12] shadow-2xl">
        <div className="pointer-events-none absolute inset-0 scanlines z-10 opacity-[0.1]" />

        {/* Header */}
        <div
          className="relative z-20 shrink-0 px-4 py-2.5 border-b border-purple-500/20"
          style={{
            background: 'linear-gradient(135deg, rgba(168,85,247,0.12) 0%, rgba(236,72,153,0.06) 100%)'
          }}
        >
          <div className="flex items-center justify-between">
            <div>
              <h2 className="text-[11px] font-bold tracking-[0.2em] uppercase text-purple-300">
                ◈ Live Signal Matrix
              </h2>
              <p className="text-[9px] text-white/35 mt-0.5 tracking-[0.15em] font-mono uppercase">
                Neural Decision Output
              </p>
            </div>
            {activeSymbol !== 'HOME' && (
              <div className="flex items-center gap-1.5">
                <span className="text-[8px] font-mono font-bold tracking-widest text-white/35 uppercase">Regime:</span>
                <span
                  className="px-2 py-0.5 rounded text-[8px] font-bold font-mono tracking-widest uppercase border transition-all duration-300"
                  style={{
                    color: activeDecision?.market_regime === 'Trending' ? '#10b981' : '#94a3b8',
                    borderColor: activeDecision?.market_regime === 'Trending' ? 'rgba(16, 185, 129, 0.3)' : 'rgba(255, 255, 255, 0.1)',
                    backgroundColor: activeDecision?.market_regime === 'Trending' ? 'rgba(16, 185, 129, 0.12)' : 'rgba(255, 255, 255, 0.05)',
                    boxShadow: activeDecision?.market_regime === 'Trending' ? '0 0 8px rgba(16, 185, 129, 0.15)' : 'none'
                  }}
                >
                  {activeDecision?.market_regime || 'Choppy'}
                </span>
              </div>
            )}
          </div>
        </div>

        {/* Live Metrics Display */}
        {activeDecision?.market_regime === 'Choppy' ? (
          <div className="flex-1 flex flex-col justify-between p-3 bg-[#02050b]/60">
            {/* Calming Chop Banner override */}
            <div className="flex-1 flex flex-col items-center justify-center border border-amber-500/20 bg-amber-500/5 rounded-xl p-4 my-2 text-center animate-pulse">
              <span className="text-xl mb-2">🛡️</span>
              <h3 className="text-xs font-bold text-amber-400 tracking-wider uppercase font-mono">
                CHOP REGIME DETECTED
              </h3>
              <p className="text-[9px] text-white/60 font-mono tracking-wide mt-1 uppercase">
                NO SIGNALS EXPECTED
              </p>
              <div className="h-px w-12 bg-amber-500/25 my-2"></div>
              <p className="text-[8px] text-amber-300/80 font-mono uppercase tracking-widest font-bold">
                CAPITAL PRESERVATION ACTIVE
              </p>
            </div>
            
            {/* Signal History Widget */}
            <div className="flex flex-col min-h-0 pt-2 border-t border-white/5">
              <span className="text-[8px] font-mono text-white/30 uppercase tracking-widest block mb-2">Signal History</span>
              <div className="flex-1 overflow-y-auto space-y-1.5 pr-1 custom-scrollbar text-[9px] max-h-[110px]">
                {!(signalHistory[activeSymbol]) || signalHistory[activeSymbol].length === 0 ? (
                  <div className="text-white/20 italic text-center py-2">No historical signals.</div>
                ) : (
                  signalHistory[activeSymbol].map((sig, i) => (
                    <div key={i} className="flex justify-between items-center py-1.5 px-2 rounded bg-white/[0.02] border border-white/5 hover:bg-white/[0.04] transition-all">
                      <div className="flex items-center gap-1.5">
                        <span
                          className="px-1.5 py-0.5 rounded text-[7px] font-bold tracking-widest font-mono"
                          style={{
                            color: sig.decision === 'LONG' ? '#10b981' : sig.decision === 'SHORT' ? '#f43f5e' : '#94a3b8',
                            background: sig.decision === 'LONG' ? 'rgba(16, 185, 129, 0.12)' :
                                        sig.decision === 'SHORT' ? 'rgba(244, 63, 94, 0.12)' : 'rgba(255, 255, 255, 0.05)'
                          }}
                        >
                          {sig.decision}
                        </span>
                        <span className="text-white/40 font-mono text-[8px]">{sig.timestamp}</span>
                      </div>
                      <div className="flex items-center gap-2">
                        <span className="text-white/60 font-mono">Conf: {((sig.confidence||0)*100).toFixed(0)}%</span>
                        {sig.stopLoss && (
                          <span className="text-rose-400 font-mono text-[8px]">SL: {sig.stopLoss.toFixed(0)}</span>
                        )}
                      </div>
                    </div>
                  ))
                )}
              </div>
            </div>
          </div>
        ) : (
          <div className="flex-1 overflow-y-auto p-3 flex flex-col justify-between gap-3 custom-scrollbar bg-[#02050b]/60">
            
            {/* Signal Row */}
            <div className="grid grid-cols-2 gap-2 shrink-0">
              {/* Decision Badge */}
              <div className="flex flex-col justify-center p-2 rounded-lg border bg-white/[0.02] min-h-[52px]"
                style={{
                  borderColor: activeDecision?.decision === 'LONG' ? 'rgba(16, 185, 129, 0.3)' :
                               activeDecision?.decision === 'SHORT' ? 'rgba(244, 63, 94, 0.3)' : 'rgba(255, 255, 255, 0.08)'
                }}
              >
                <span className="text-[8px] font-mono text-white/30 uppercase tracking-widest mb-1">Decision</span>
                <div className="flex items-center gap-1.5">
                  <span
                    className="px-2 py-0.5 rounded text-[9px] font-bold font-mono tracking-widest text-center min-w-[56px]"
                    style={{
                      color: activeDecision?.decision === 'LONG' ? '#10b981' :
                             activeDecision?.decision === 'SHORT' ? '#f43f5e' : '#94a3b8',
                      background: activeDecision?.decision === 'LONG' ? 'rgba(16, 185, 129, 0.15)' :
                                  activeDecision?.decision === 'SHORT' ? 'rgba(244, 63, 94, 0.15)' : 'rgba(255, 255, 255, 0.08)'
                    }}
                  >
                    {activeDecision?.decision || 'HOLD'}
                  </span>
                  <span className="text-[10px]" style={{
                    color: activeDecision?.decision === 'LONG' ? '#10b981' :
                           activeDecision?.decision === 'SHORT' ? '#f43f5e' : '#94a3b8'
                  }}>
                    {activeDecision?.decision === 'LONG' ? '▲' : activeDecision?.decision === 'SHORT' ? '▼' : '◆'}
                  </span>
                </div>
              </div>

              {/* Confidence Display */}
              <div className="flex flex-col justify-center p-2 rounded-lg border border-white/8 bg-white/[0.02] min-h-[52px]">
                <span className="text-[8px] font-mono text-white/30 uppercase tracking-widest mb-1">Confidence</span>
                <div className="flex items-center gap-2">
                  <span className="text-[10px] font-bold font-mono text-white">
                    {((activeDecision?.confidence || 0) * 100).toFixed(0)}%
                  </span>
                  <div className="flex-1 h-1.5 bg-white/10 rounded-full overflow-hidden">
                    <div
                      className="h-full rounded-full transition-all duration-500"
                      style={{
                        width: `${(activeDecision?.confidence || 0) * 100}%`,
                        background: activeDecision?.decision === 'LONG' ? '#10b981' :
                                    activeDecision?.decision === 'SHORT' ? '#f43f5e' : '#94a3b8',
                        boxShadow: activeDecision?.decision === 'LONG' ? '0 0 6px #10b981' :
                                   activeDecision?.decision === 'SHORT' ? '0 0 6px #f43f5e' : 'none'
                      }}
                    />
                  </div>
                </div>
              </div>
            </div>

            {/* S/L, T/P, Leverage and Est. Duration Row */}
            <div className="grid grid-cols-2 gap-2 shrink-0 text-[10px]">
              {/* Max Leverage */}
              <div className="flex flex-col justify-center p-2 rounded-lg border border-white/8 bg-white/[0.02] min-h-[52px]">
                <span className="text-[8px] font-mono text-white/30 uppercase tracking-widest mb-1">Max Leverage</span>
                <div className="flex items-center gap-1.5">
                  <span className="px-1.5 py-0.5 rounded text-[8px] font-bold font-mono bg-purple-500/20 text-purple-300 border border-purple-500/30 tracking-wider">
                    {activeDecision?.leverage ? `${activeDecision.leverage}X` : '5X'}
                  </span>
                  <span className="text-white/40 text-[8px] font-mono">Isolated</span>
                </div>
              </div>

              {/* Take Profit Time / Est. Duration */}
              <div className="flex flex-col justify-center p-2 rounded-lg border border-cyan-500/20 bg-cyan-500/5 min-h-[52px]">
                <span className="text-[8px] font-mono text-cyan-300/70 uppercase tracking-widest mb-1">Take Profit Time</span>
                <span className="font-mono text-cyan-300 font-bold text-[9px]">
                  {activeDecision?.expected_duration_mins ? `~${activeDecision.expected_bars || 0} bars (${activeDecision.expected_duration_mins}m)` : '~0.0 bars (0m)'}
                </span>
              </div>

              {/* Stop loss */}
              <div className="flex flex-col justify-center p-2 rounded-lg border border-white/8 bg-white/[0.02] min-h-[52px]">
                <span className="text-[8px] font-mono text-white/30 uppercase tracking-widest mb-1">Stop Loss</span>
                <span className="font-mono text-rose-400 font-bold">
                  {activeDecision?.stop_loss ? `$${activeDecision.stop_loss.toLocaleString(undefined, { minimumFractionDigits: 1 })}` : 'N/A'}
                </span>
              </div>

              {/* Take Profit */}
              <div className="flex flex-col justify-center p-2 rounded-lg border border-white/8 bg-white/[0.02] min-h-[52px]">
                <span className="text-[8px] font-mono text-white/30 uppercase tracking-widest mb-1">Take Profit</span>
                <span className="font-mono text-emerald-400 font-bold">
                  {activeDecision?.take_profit ? `$${activeDecision.take_profit.toLocaleString(undefined, { minimumFractionDigits: 1 })}` : 'N/A'}
                </span>
              </div>
            </div>

            {/* Strategic Rationale */}
            <div className="p-2 rounded-lg border border-white/8 bg-white/[0.01] shrink-0">
              <span className="text-[8px] font-mono text-white/30 uppercase tracking-widest block mb-1">Strategic Rationale</span>
              <p className="text-[9px] text-white/80 italic leading-normal font-sans">
                "{activeDecision?.reasoning || 'No active trading signal generated.'}"
              </p>
            </div>

            {/* Signal History Widget */}
            <div className="flex-1 flex flex-col min-h-0 pt-2 border-t border-white/5">
              <span className="text-[8px] font-mono text-white/30 uppercase tracking-widest block mb-2">Signal History</span>
              <div className="flex-1 overflow-y-auto space-y-1.5 pr-1 custom-scrollbar text-[9px] max-h-[110px]">
                {!(signalHistory[activeSymbol]) || signalHistory[activeSymbol].length === 0 ? (
                  <div className="text-white/20 italic text-center py-2">No historical signals.</div>
                ) : (
                  signalHistory[activeSymbol].map((sig, i) => (
                    <div key={i} className="flex justify-between items-center py-1.5 px-2 rounded bg-white/[0.02] border border-white/5 hover:bg-white/[0.04] transition-all">
                      <div className="flex items-center gap-1.5">
                        <span
                          className="px-1.5 py-0.5 rounded text-[7px] font-bold tracking-widest font-mono"
                          style={{
                            color: sig.decision === 'LONG' ? '#10b981' : sig.decision === 'SHORT' ? '#f43f5e' : '#94a3b8',
                            background: sig.decision === 'LONG' ? 'rgba(16, 185, 129, 0.12)' :
                                        sig.decision === 'SHORT' ? 'rgba(244, 63, 94, 0.12)' : 'rgba(255, 255, 255, 0.05)'
                          }}
                        >
                          {sig.decision}
                        </span>
                        <span className="text-white/40 font-mono text-[8px]">{sig.timestamp}</span>
                      </div>
                      <div className="flex items-center gap-2">
                        <span className="text-white/60 font-mono">Conf: {((sig.confidence||0)*100).toFixed(0)}%</span>
                        {sig.stopLoss && (
                          <span className="text-rose-400 font-mono text-[8px]">SL: {sig.stopLoss.toFixed(0)}</span>
                        )}
                      </div>
                    </div>
                  ))
                )}
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
