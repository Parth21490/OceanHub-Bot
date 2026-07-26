import { useState, useEffect } from 'react'
import { useWebSocket } from './hooks/useWebSocket'
import { ChartPane } from './components/ChartPane'
import { MasterAICore } from './components/MasterAICore'
import HomeDashboard from './components/HomeDashboard'

// Determine WebSocket URL based on environment
const getWebSocketUrl = () => {
  if (import.meta.env.PROD) {
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
    return `${protocol}//${window.location.host}`
  }
  return 'ws://127.0.0.1:8080'
}

const CHART_PANES = [
  { id: '1d', title: '1D Timeframe', subtitle: 'Macro Trend (200 EMA / ADX)', type: 'candlestick', accent: '#f59e0b' },
  { id: '4h', title: '4H Timeframe', subtitle: 'Swing Setup (BB)', type: 'candlestick', accent: '#a78bfa' },
  { id: '1h', title: '1H Timeframe', subtitle: 'Order Blocks (S/R)', type: 'candlestick', accent: '#38bdf8' },
  { id: '15m', title: '15M Timeframe', subtitle: 'Momentum (RSI)', type: 'candlestick', accent: '#f87171' },
  { id: '3m', title: '3M Timeframe', subtitle: 'Sniper Trigger (Vol Delta)', type: 'candlestick', accent: '#34d399' },
  { id: '1m', title: '1M Timeframe', subtitle: 'Micro Execution View', type: 'candlestick', accent: '#10b981' },
  { id: 'orderbook', title: 'Live Order Book', subtitle: 'Bid/Ask Spread', type: 'orderbook', accent: '#6366f1' },
  { id: 'macd', title: 'MACD Indicator', subtitle: '3M Histogram', type: 'macd', accent: '#f43f5e' }
]

const STATUS_STYLES = {
  connected:    { dot: '#10b981', label: 'LIVE',    pulse: true },
  connecting:   { dot: '#f59e0b', label: 'SYNC',    pulse: true },
  disconnected: { dot: '#6b7280', label: 'OFFLINE', pulse: false },
  error:        { dot: '#f43f5e', label: 'ERROR',   pulse: false }
}

function Clock() {
  const [time, setTime] = useState(new Date())
  useEffect(() => {
    const t = setInterval(() => setTime(new Date()), 1000)
    return () => clearInterval(t)
  }, [])
  return (
    <div className="flex flex-col items-center">
      <span className="clock-text text-sm font-mono font-bold text-white/70 tabular-nums tracking-widest">
        {time.toLocaleTimeString('en-US', { hour12: false })}
      </span>
      <span className="text-[9px] text-white/25 tracking-[0.15em] uppercase font-mono">
        {time.toLocaleDateString('en-US', { weekday: 'short', month: 'short', day: 'numeric' })}
      </span>
    </div>
  )
}

const SYMBOLS = ['HOME', 'BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'BNB/USDT', 'HYPE/USDT', 'XRP/USDT', 'ADA/USDT', 'DOGE/USDT']
const STORAGE_KEY = 'oceanhub_active_asset'

/**
 * Resolves initial active asset/view state prior to first render.
 * Order of precedence:
 * 1. URL Query Parameter (?symbol=..., ?asset=..., or ?tab=...)
 * 2. LocalStorage saved state (oceanhub_active_asset)
 * 3. Default fallback ('HOME')
 */
function getInitialAsset(validSymbols) {
  try {
    // 1. Check URL parameters
    const params = new URLSearchParams(window.location.search)
    const rawParam = params.get('symbol') || params.get('asset') || params.get('tab') || params.get('ticker')
    if (rawParam) {
      let normalized = rawParam.replace('-', '/').toUpperCase()
      if (!normalized.includes('/')) {
        normalized = normalized + '/USDT'
      }
      if (validSymbols.includes(normalized)) {
        return normalized
      }
    }

    // 2. Fallback to LocalStorage
    const saved = localStorage.getItem(STORAGE_KEY)
    if (saved && validSymbols.includes(saved)) {
      return saved
    }
  } catch (e) {
    console.warn('Failed to parse persistent view state:', e)
  }

  // 3. Default fallback
  return 'HOME'
}

export default function App() {
  // Synchronously initialize state prior to first render to prevent flickering
  const [activeAsset, setActiveAsset] = useState(() => getInitialAsset(SYMBOLS))
  const ws = useWebSocket(getWebSocketUrl())
  const { dot, label, pulse } = STATUS_STYLES[ws.status] || STATUS_STYLES.disconnected

  // Dynamic asset search state
  const [searchQuery, setSearchQuery] = useState('')
  const [dynamicAsset, setDynamicAsset] = useState(null)
  const [searchError, setSearchError] = useState(null)
  const [isSearching, setIsSearching] = useState(false)

  // Listen for dynamic asset WS search results and errors
  useEffect(() => {
    if (!ws.lastMessage) return
    try {
      const msg = JSON.parse(ws.lastMessage.data)
      if (msg.type === 'DYNAMIC_ASSET_RESULT') {
        setIsSearching(false)
        setSearchError(null)
        if (msg.data && msg.data.valid) {
          setDynamicAsset(msg.data)
          setActiveAsset(msg.data.symbol)
        } else if (msg.data && msg.data.error) {
          setSearchError(msg.data.error)
          setTimeout(() => setSearchError(null), 5000)
        }
      } else if (msg.type === 'DYNAMIC_ASSET_ERROR') {
        setIsSearching(false)
        setSearchError(msg.error || 'Asset not found on exchange.')
        setTimeout(() => setSearchError(null), 5000)
      }
    } catch (e) {
      console.error('Failed to parse dynamic asset message:', e)
    }
  }, [ws.lastMessage])

  // Handle dynamic asset search submit
  const handleSearchSubmit = (e) => {
    e.preventDefault()
    if (!searchQuery.trim()) return

    let formatted = searchQuery.trim().toUpperCase()
    if (!formatted.includes('/')) {
      if (formatted.endsWith('USDT')) {
        formatted = formatted.slice(0, -4) + '/USDT'
      } else {
        formatted = formatted + '/USDT'
      }
    }

    setIsSearching(true)
    setSearchError(null)

    if (ws.status === 'connected') {
      ws.sendMessage({
        type: 'DYNAMIC_ASSET_SEARCH',
        symbol: formatted
      })
    } else {
      setIsSearching(false)
      setSearchError('WebSocket offline. Cannot search asset.')
      setTimeout(() => setSearchError(null), 5000)
    }

    setSearchQuery('')
  }

  // Update active asset state and persist to URL and LocalStorage
  const handleSelectAsset = (symbol) => {
    setActiveAsset(symbol)

    // Save to LocalStorage
    try {
      localStorage.setItem(STORAGE_KEY, symbol)
    } catch (e) {
      console.warn('LocalStorage save error:', e)
    }

    // Save to URL Query Parameters without full page reload
    try {
      const url = new URL(window.location.href)
      if (symbol === 'HOME') {
        url.searchParams.delete('symbol')
        url.searchParams.delete('asset')
        url.searchParams.delete('tab')
      } else {
        url.searchParams.set('symbol', symbol.replace('/', '-'))
      }
      window.history.replaceState(null, '', url.pathname + url.search + url.hash)
    } catch (e) {
      console.warn('URL update error:', e)
    }
  }

  // Handle browser back/forward navigation
  useEffect(() => {
    const handlePopState = () => {
      const initial = getInitialAsset(SYMBOLS)
      setActiveAsset(initial)
    }
    window.addEventListener('popstate', handlePopState)
    return () => window.removeEventListener('popstate', handlePopState)
  }, [])

  // Dynamically update document title based on active view
  useEffect(() => {
    if (activeAsset === 'HOME') {
      document.title = '[🏠 Home] Trading Bot Engine'
    } else {
      document.title = `[$${activeAsset.split('/')[0]}] Live Telemetry & Signals`
    }
  }, [activeAsset])

  useEffect(() => {
    if (activeAsset !== 'HOME' && ws.status === 'connected') {
      ws.sendMessage({
        type: 'SWITCH_SYMBOL',
        symbol: activeAsset
      })
    }
  }, [activeAsset, ws.status, ws.sendMessage])

  return (
    <div
      className="h-screen w-screen overflow-hidden flex flex-col select-none"
      style={{ background: '#0a0d14' }}
    >
      {/* ── HEADER BAR ── */}
      <header
        className="shrink-0 flex items-center justify-between px-4 py-1.5 border-b border-white/[0.06]"
        style={{
          background: 'linear-gradient(90deg, rgba(6,182,212,0.07) 0%, rgba(10,13,20,0) 40%, rgba(139,92,246,0.06) 100%)',
          minHeight: '40px'
        }}
      >
        {/* Logo */}
        <div className="flex items-center gap-2.5">
          <div
            className="w-6 h-6 rounded-lg flex items-center justify-center text-white text-xs font-black shrink-0"
            style={{ background: 'linear-gradient(135deg, #0891b2, #7c3aed)' }}
          >
            ◈
          </div>
          <div className="flex items-baseline gap-0.5">
            <span className="text-[13px] font-bold tracking-wider text-white">Ocean</span>
            <span
              className="text-[13px] font-bold tracking-wider"
              style={{
                background: 'linear-gradient(90deg, #22d3ee, #a78bfa)',
                WebkitBackgroundClip: 'text',
                WebkitTextFillColor: 'transparent'
              }}
            >
              Hub
            </span>
          </div>
          <span className="text-[9px] text-white/20 tracking-[0.2em] uppercase font-mono">
            AI Trading Terminal
          </span>
          <span className="px-2 py-0.5 rounded bg-amber-500/15 border border-amber-500/35 text-amber-400 text-[8px] font-bold font-mono tracking-widest uppercase animate-pulse ml-2.5">
            DRY RUN — SIMULATION ONLY
          </span>
        </div>

        {/* Quick Search Bar */}
        <form onSubmit={handleSearchSubmit} className="flex items-center gap-1.5 bg-white/[0.03] border border-white/10 rounded-lg px-2.5 py-1 focus-within:border-cyan-500/50 transition-all">
          <span className="text-[11px] text-cyan-400">🔍</span>
          <input
            type="text"
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            placeholder="Search Asset (e.g. AVAX/USDT)"
            className="bg-transparent text-[10px] font-mono text-white placeholder-white/30 outline-none w-48 tracking-wider uppercase"
          />
          <button
            type="submit"
            disabled={isSearching}
            className="px-2 py-0.5 rounded text-[8px] font-bold font-mono uppercase tracking-wider bg-cyan-500/20 text-cyan-300 border border-cyan-500/40 hover:bg-cyan-500/30 transition-all disabled:opacity-50"
          >
            {isSearching ? 'ANALYZING...' : 'SEARCH'}
          </button>
        </form>

        {/* Clock */}
        <Clock />

        {/* WS Status */}
        <div className="flex items-center gap-2">
          <div
            className="w-1.5 h-1.5 rounded-full"
            style={{
              backgroundColor: dot,
              boxShadow: `0 0 6px ${dot}`,
              animation: pulse ? 'subtle-pulse 1.5s ease-in-out infinite' : 'none'
            }}
          />
          <span className="text-[9px] font-mono text-white/35 tracking-widest">
            ws://localhost:8080
          </span>
          <span
            className="text-[9px] font-mono font-bold tracking-widest px-1.5 py-0.5 rounded border"
            style={{
              color: dot,
              borderColor: dot + '40',
              background: dot + '12'
            }}
          >
            {label}
          </span>
        </div>
      </header>

      {/* Error Toast Notification */}
      {searchError && (
        <div className="shrink-0 bg-rose-500/20 border-b border-rose-500/40 text-rose-300 text-[10px] font-mono font-bold px-4 py-1.5 flex items-center justify-between animate-pulse">
          <div className="flex items-center gap-2">
            <span>⚠️</span>
            <span>{searchError}</span>
          </div>
          <button onClick={() => setSearchError(null)} className="text-white/60 hover:text-white text-[11px]">✕</button>
        </div>
      )}

      {/* ── ASSET TABS ── */}
      <div className="shrink-0 flex items-center justify-start gap-1 px-4 py-1 border-b border-white/[0.04] bg-[#070a12]/20">
        {SYMBOLS.map((symbol) => (
          <button
            key={symbol}
            onClick={() => handleSelectAsset(symbol)}
            className={`px-3 py-1 rounded-lg text-[9px] font-bold font-mono tracking-widest transition-all uppercase cursor-pointer ${
              activeAsset === symbol
                ? 'bg-cyan-500/15 border border-cyan-500/35 text-cyan-300 shadow-[0_0_8px_rgba(34,211,238,0.15)]'
                : 'bg-transparent border border-transparent text-white/40 hover:text-white/70 hover:bg-white/[0.02]'
            }`}
          >
            {symbol}
          </button>
        ))}

        {/* Dynamic Temporary Asset Tab */}
        {dynamicAsset && dynamicAsset.symbol && !SYMBOLS.includes(dynamicAsset.symbol) && (
          <button
            key={dynamicAsset.symbol}
            onClick={() => handleSelectAsset(dynamicAsset.symbol)}
            className={`px-3 py-1 rounded-lg text-[9px] font-bold font-mono tracking-widest transition-all uppercase cursor-pointer flex items-center gap-1.5 ${
              activeAsset === dynamicAsset.symbol
                ? 'bg-purple-500/20 border border-purple-500/40 text-purple-300 shadow-[0_0_10px_rgba(168,85,247,0.2)]'
                : 'bg-white/[0.03] border border-white/10 text-white/60 hover:text-white'
            }`}
          >
            <span className="text-[10px]">⚡</span>
            <span>{dynamicAsset.symbol}</span>
            <span className="text-[7px] bg-purple-500/30 px-1 py-0.2 rounded text-purple-200">TEMP</span>
          </button>
        )}
      </div>

      {/* ── MAIN CONTENT ── */}
      {activeAsset === 'HOME' ? (
        <HomeDashboard ws={ws} dynamicAsset={dynamicAsset} onSelectAsset={handleSelectAsset} />
      ) : (
        <main
          className="flex-1 min-h-0 p-2"
          style={{
            display: 'grid',
            gridTemplateColumns: 'repeat(4, 1fr) 320px',
            gridTemplateRows: 'repeat(2, 1fr)',
            gap: '6px'
          }}
        >
          {CHART_PANES.map((pane) => (
            <ChartPane
              key={pane.id}
              paneId={pane.id}
              title={pane.title}
              subtitle={pane.subtitle}
              type={pane.type}
              accentColor={pane.accent}
              activeAsset={activeAsset}
              ws={ws}
            />
          ))}

          <div style={{ gridColumn: '5', gridRow: '1 / span 2', height: '100%', minHeight: 0 }}>
            <MasterAICore ws={ws} activeSymbol={activeAsset} />
          </div>
        </main>
      )}
    </div>
  )
}
