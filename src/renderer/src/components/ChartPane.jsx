import { useRef, useEffect, useState, useCallback } from 'react'
import {
  createChart,
  CandlestickSeries,
  LineSeries,
  HistogramSeries
} from 'lightweight-charts'

const DARK_CHART_OPTIONS = {
  layout: {
    background: { color: 'transparent' },
    textColor: '#94a3b8',
    fontSize: 10
  },
  grid: {
    vertLines: { color: 'rgba(255,255,255,0.03)' },
    horzLines: { color: 'rgba(255,255,255,0.03)' }
  },
  crosshair: {
    vertLine: { color: 'rgba(100,200,255,0.2)' },
    horzLine: { color: 'rgba(100,200,255,0.2)' }
  },
  rightPriceScale: {
    borderColor: 'rgba(255,255,255,0.06)',
    scaleMargins: { top: 0.1, bottom: 0.1 }
  },
  leftPriceScale: {
    borderColor: 'rgba(255,255,255,0.06)',
    visible: true,
    scaleMargins: { top: 0.35, bottom: 0.35 }
  },
  timeScale: {
    borderColor: 'rgba(255,255,255,0.06)',
    timeVisible: true
  }
}

// ── Order Book ladder view ─────────────────────────────────────────────────────
function OrderBookView({ data }) {
  const bids = data?.bids?.slice(0, 5) || []
  const asks = data?.asks?.slice(0, 5) || []
  const spread = data?.spread || 0.0

  return (
    <div className="flex-1 min-h-0 p-3 flex flex-col justify-between font-mono text-[10px] select-none text-white/70 bg-[#0a0d14]/40 h-full">
      <div className="flex flex-col gap-0.5">
        <div className="flex justify-between text-white/30 text-[8px] border-b border-white/5 pb-1 mb-1.5 uppercase tracking-wider">
          <span>Price (USDT)</span>
          <span>Size</span>
        </div>
        {[...asks].reverse().map(([price, size], i) => (
          <div key={i} className="flex justify-between relative py-0.5">
            <span className="text-rose-500 font-bold z-10">{price.toLocaleString(undefined, { minimumFractionDigits: 1 })}</span>
            <span className="text-white/60 z-10">{size.toFixed(4)}</span>
            <div className="absolute right-0 top-0 bottom-0 bg-rose-500/10 transition-all duration-300" style={{ width: `${Math.min(100, size * 20)}%` }} />
          </div>
        ))}
      </div>

      <div className="py-1.5 my-1.5 border-y border-white/5 flex justify-between items-center text-[9px]">
        <span className="text-white/30 uppercase tracking-widest text-[8px]">Spread</span>
        <span className="text-cyan-400 font-bold font-mono">{spread.toFixed(1)} USDT</span>
      </div>

      <div className="flex flex-col gap-0.5">
        {bids.map(([price, size], i) => (
          <div key={i} className="flex justify-between relative py-0.5">
            <span className="text-emerald-500 font-bold z-10">{price.toLocaleString(undefined, { minimumFractionDigits: 1 })}</span>
            <span className="text-white/60 z-10">{size.toFixed(4)}</span>
            <div className="absolute right-0 top-0 bottom-0 bg-emerald-500/10 transition-all duration-300" style={{ width: `${Math.min(100, size * 20)}%` }} />
          </div>
        ))}
      </div>
    </div>
  )
}

// ── Helper: sanitise a value — replace null/NaN with undefined so LWC skips it ─
function san(v) {
  if (v === null || v === undefined) return undefined
  if (typeof v === 'number' && !isFinite(v)) return undefined
  return v
}

// ── ChartPane ─────────────────────────────────────────────────────────────────
export function ChartPane({ paneId, title, subtitle, type, accentColor = '#22d3ee', activeAsset, ws }) {
  const containerRef = useRef(null)
  const chartRef     = useRef(null)
  const seriesRef    = useRef({})           // keyed series objects
  const activeRef    = useRef(activeAsset)  // always reflects latest activeAsset (no stale closure)

  const [orderBook, setOrderBook] = useState({ bids: [], asks: [], spread: 0.0 })
  const [adxValue,  setAdxValue]  = useState(20.0)

  // Keep activeRef in sync so message handler always has the right symbol
  useEffect(() => { activeRef.current = activeAsset }, [activeAsset])

  // ── 1. Build chart + series (once per type/paneId) ─────────────────────────
  useEffect(() => {
    if (type === 'orderbook' || !containerRef.current) return

    const chart = createChart(containerRef.current, {
      ...DARK_CHART_OPTIONS,
      width:  containerRef.current.clientWidth,
      height: containerRef.current.clientHeight
    })
    chartRef.current = chart
    const sl = {}

    if (type === 'candlestick') {
      sl.main = chart.addSeries(CandlestickSeries, {
        upColor: '#10b981', downColor: '#f43f5e',
        borderUpColor: '#10b981', borderDownColor: '#f43f5e',
        wickUpColor: '#10b981', wickDownColor: '#f43f5e'
      })
      if (paneId === '1d')  sl.ema200     = chart.addSeries(LineSeries, { color: '#ef4444', lineWidth: 1.5, title: 'EMA 200' })
      if (paneId === '4h') {
        sl.bb_upper  = chart.addSeries(LineSeries, { color: '#a78bfa', lineWidth: 1, lineStyle: 2, title: 'BB Upper' })
        sl.bb_middle = chart.addSeries(LineSeries, { color: '#8b5cf6', lineWidth: 1, lineStyle: 2, title: 'BB Middle' })
        sl.bb_lower  = chart.addSeries(LineSeries, { color: '#a78bfa', lineWidth: 1, lineStyle: 2, title: 'BB Lower' })
      }
      if (paneId === '1h') {
        sl.resistance = chart.addSeries(LineSeries, { color: '#f43f5e', lineWidth: 1.5, title: 'Resistance' })
        sl.support    = chart.addSeries(LineSeries, { color: '#10b981', lineWidth: 1.5, title: 'Support' })
      }
      if (paneId === '15m') sl.rsi = chart.addSeries(LineSeries, { color: '#fb7185', lineWidth: 1.5, priceScaleId: 'left', title: 'RSI' })
    } else if (type === 'macd') {
      sl.macd_histogram = chart.addSeries(HistogramSeries, { color: '#34d399', title: 'MACD Hist' })
      sl.macd_line      = chart.addSeries(LineSeries, { color: '#38bdf8', lineWidth: 1.5, title: 'MACD' })
      sl.signal_line    = chart.addSeries(LineSeries, { color: '#fbbf24', lineWidth: 1.5, title: 'Signal' })
    }

    seriesRef.current = sl

    const observer = new ResizeObserver(() => {
      if (containerRef.current && chartRef.current) {
        chartRef.current.applyOptions({
          width:  containerRef.current.clientWidth,
          height: containerRef.current.clientHeight
        })
      }
    })
    observer.observe(containerRef.current)

    return () => {
      observer.disconnect()
      chart.remove()
      chartRef.current = null
      seriesRef.current = {}
    }
  }, [type, paneId]) // ← only rebuild chart if type/pane changes, NOT on symbol switch

  // ── 2. Seed history when activeAsset changes ───────────────────────────────
  const lastFetchedSymbolRef = useRef(null)
  const sendMessage = ws?.sendMessage
  useEffect(() => {
    if (type === 'orderbook' || !sendMessage) return
    if (lastFetchedSymbolRef.current === activeAsset) return
    lastFetchedSymbolRef.current = activeAsset
    // Ask backend for historical data for this symbol
    sendMessage({ type: 'get_history', symbol: activeAsset })
  }, [activeAsset, type, sendMessage])

  // ── 3. Message handler — wired to the shared ws via subscribe() ────────────
  const applyHistory = useCallback((history) => {
    const sl = seriesRef.current
    const chart = chartRef.current
    if (!sl.main || !chart) return

    // Clear stale data before seeding new symbol
    Object.values(sl).forEach(s => { try { s.setData([]) } catch (_) {} })

    if (type === 'candlestick') {
      sl.main.setData(history
        .filter(d => san(d.open) !== undefined)
        .map(d => ({ time: d.time, open: d.open, high: d.high, low: d.low, close: d.close }))
      )
      if (paneId === '1d' && sl.ema200) {
        sl.ema200.setData(history.filter(d => san(d.ema200) !== undefined).map(d => ({ time: d.time, value: d.ema200 })))
        const last = history[history.length - 1]
        if (last?.adx != null) setAdxValue(last.adx)
      }
      if (paneId === '4h' && sl.bb_upper) {
        sl.bb_upper.setData( history.filter(d => san(d.bb_upper)  !== undefined).map(d => ({ time: d.time, value: d.bb_upper })))
        sl.bb_middle.setData(history.filter(d => san(d.bb_middle) !== undefined).map(d => ({ time: d.time, value: d.bb_middle })))
        sl.bb_lower.setData( history.filter(d => san(d.bb_lower)  !== undefined).map(d => ({ time: d.time, value: d.bb_lower })))
      }
      if (paneId === '1h' && sl.resistance) {
        sl.resistance.setData(history.filter(d => san(d.resistance) !== undefined).map(d => ({ time: d.time, value: d.resistance })))
        sl.support.setData(   history.filter(d => san(d.support)    !== undefined).map(d => ({ time: d.time, value: d.support })))
      }
      if (paneId === '15m' && sl.rsi) {
        sl.rsi.setData(history.filter(d => san(d.rsi) !== undefined).map(d => ({ time: d.time, value: d.rsi })))
      }
    } else if (type === 'macd' && sl.macd_histogram) {
      sl.macd_histogram.setData(history
        .filter(d => san(d.macd_histogram) !== undefined)
        .map(d => ({
          time: d.time, value: d.macd_histogram,
          color: d.macd_histogram >= 0 ? 'rgba(16,185,129,0.7)' : 'rgba(244,63,94,0.7)'
        }))
      )
      sl.macd_line.setData(  history.filter(d => san(d.macd_line)   !== undefined).map(d => ({ time: d.time, value: d.macd_line })))
      sl.signal_line.setData(history.filter(d => san(d.signal_line) !== undefined).map(d => ({ time: d.time, value: d.signal_line })))
    }
    chart.timeScale().fitContent()
  }, [type, paneId])

  const subscribe = ws?.subscribe
  useEffect(() => {
    if (!subscribe || type === 'orderbook') return

    const unsub = subscribe((msg) => {
      const sym = activeRef.current

      // ── Historical seed ────────────────────────────────────────────────────
      if (msg.type === 'INIT_CHART_HISTORY' && msg.symbol === sym && msg.data?.[paneId]) {
        console.log(`[ChartPane ${paneId}] INIT_CHART_HISTORY for ${sym} — ${msg.data[paneId].length} candles`)
        applyHistory(msg.data[paneId])
        return
      }

      // ── Live OHLCV tick ────────────────────────────────────────────────────
      if (msg.type === 'OHLCV' && msg.symbol === sym && msg.paneId === paneId) {
        const sl = seriesRef.current
        const d  = msg.data
        if (!sl.main || !d) return
        console.log(`[ChartPane ${paneId}] OHLCV tick for ${sym}`)
        if (san(d.open) !== undefined) {
          sl.main.update({ time: d.time, open: d.open, high: d.high, low: d.low, close: d.close })
        }
        if (paneId === '1d' && sl.ema200 && san(d.ema200) !== undefined) {
          sl.ema200.update({ time: d.time, value: d.ema200 })
          if (d.adx != null) setAdxValue(d.adx)
        }
        if (paneId === '4h' && sl.bb_upper && san(d.bb_upper) !== undefined) {
          sl.bb_upper.update({ time: d.time, value: d.bb_upper })
          sl.bb_middle.update({ time: d.time, value: d.bb_middle })
          sl.bb_lower.update({ time: d.time, value: d.bb_lower })
        }
        if (paneId === '1h' && sl.resistance && san(d.resistance) !== undefined) {
          sl.resistance.update({ time: d.time, value: d.resistance })
          sl.support.update({ time: d.time, value: d.support })
        }
        if (paneId === '15m' && sl.rsi && san(d.rsi) !== undefined) {
          sl.rsi.update({ time: d.time, value: d.rsi })
        }
        return
      }

      // ── Live MACD tick ─────────────────────────────────────────────────────
      if (msg.type === 'MACD' && msg.symbol === sym && paneId === 'macd') {
        const sl = seriesRef.current
        const d  = msg.data
        if (!sl.macd_histogram || !d || san(d.macd_histogram) === undefined) return
        sl.macd_histogram.update({
          time: d.time, value: d.macd_histogram,
          color: d.macd_histogram >= 0 ? 'rgba(16,185,129,0.7)' : 'rgba(244,63,94,0.7)'
        })
        if (san(d.macd_line)   !== undefined) sl.macd_line.update({ time: d.time, value: d.macd_line })
        if (san(d.signal_line) !== undefined) sl.signal_line.update({ time: d.time, value: d.signal_line })
      }
    })

    return unsub
  }, [subscribe, paneId, type, applyHistory])

  // ── 4. Order book via shared ws ────────────────────────────────────────────
  useEffect(() => {
    if (!subscribe || type !== 'orderbook') return

    const unsub = subscribe((msg) => {
      if (msg.type === 'ORDERBOOK' && msg.symbol === activeRef.current) {
        setOrderBook(msg.data)
      }
    })
    return unsub
  }, [subscribe, type])

  // ── Render ─────────────────────────────────────────────────────────────────
  return (
    <div className="chart-pane group relative flex flex-col overflow-hidden rounded-xl border border-white/8 bg-white/[0.04] backdrop-blur-sm transition-all duration-300 hover:border-white/20 hover:bg-white/[0.06] h-full min-h-0">
      {/* Header */}
      <div className="flex items-center justify-between px-3 py-1.5 shrink-0">
        <div className="flex items-center gap-1.5">
          <span className="text-[10px] font-bold tracking-widest text-white/90 uppercase">{title}</span>
          {subtitle && (
            <span className="text-[9px] text-white/30 font-mono">{subtitle}</span>
          )}
        </div>

        {paneId === '1d' && (
          <div className="flex items-center gap-1.5 px-1.5 py-0.5 bg-cyan-500/10 border border-cyan-500/20 rounded font-mono text-[8px]">
            <span className="text-cyan-300 font-bold">ADX:</span>
            <span className="text-white font-bold">{adxValue.toFixed(1)}</span>
            <div className="w-8 h-1 bg-white/10 rounded-full overflow-hidden">
              <div
                className="h-full bg-cyan-400 transition-all duration-500"
                style={{ width: `${Math.min(100, (adxValue / 50) * 100)}%` }}
              />
            </div>
            <span className="text-white/40 uppercase tracking-wide">
              {adxValue > 25 ? 'Trending' : 'Weak'}
            </span>
          </div>
        )}

        <span
          className="text-[9px] font-mono px-1.5 py-0.5 rounded border tracking-widest text-[8px]"
          style={{
            color: accentColor,
            borderColor: accentColor + '40',
            background: accentColor + '12'
          }}
        >
          {paneId.toUpperCase()}
        </span>
      </div>

      {/* Accent line */}
      <div
        className="h-px shrink-0"
        style={{ background: `linear-gradient(90deg, ${accentColor}80, transparent)` }}
      />

      {/* Chart container */}
      <div className="flex-1 min-h-0 relative">
        {type === 'orderbook' ? (
          <OrderBookView data={orderBook} />
        ) : (
          <div ref={containerRef} className="absolute inset-0 w-full h-full" />
        )}
      </div>
    </div>
  )
}
