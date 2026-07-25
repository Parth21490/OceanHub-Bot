import { useEffect, useRef, useState, useCallback } from 'react'

const MAX_RETRIES = 5
const BASE_DELAY_MS = 1000

export function useWebSocket(url) {
  const wsRef = useRef(null)
  const retryCount = useRef(0)
  const retryTimer = useRef(null)
  const isMounted = useRef(true)
  // Registry of subscriber callbacks: Set<(parsedMsg) => void>
  const subscribers = useRef(new Set())

  const [lastMessage, setLastMessage] = useState(null)
  const [status, setStatus] = useState('connecting')

  const connect = useCallback(() => {
    if (!isMounted.current) return

    try {
      const ws = new WebSocket(url)
      wsRef.current = ws

      ws.onopen = () => {
        if (!isMounted.current) return
        setStatus('connected')
        retryCount.current = 0
      }

      ws.onmessage = (event) => {
        if (!isMounted.current) return
        setLastMessage(event)
        // Fan-out to all registered subscribers with the parsed message
        try {
          const parsed = JSON.parse(event.data)
          subscribers.current.forEach(cb => {
            try { cb(parsed) } catch (_) {}
          })
        } catch (_) {}
      }

      ws.onclose = () => {
        if (!isMounted.current) return
        setStatus('disconnected')
        wsRef.current = null

        if (retryCount.current < MAX_RETRIES) {
          const delay = BASE_DELAY_MS * Math.pow(2, retryCount.current)
          retryCount.current += 1
          setStatus('connecting')
          retryTimer.current = setTimeout(connect, delay)
        }
      }

      ws.onerror = () => {
        if (!isMounted.current) return
        setStatus('error')
      }
    } catch {
      setStatus('error')
    }
  }, [url])

  useEffect(() => {
    isMounted.current = true
    connect()
    return () => {
      isMounted.current = false
      if (retryTimer.current) clearTimeout(retryTimer.current)
      if (wsRef.current) {
        wsRef.current.onclose = null
        wsRef.current.close()
      }
    }
  }, [connect])

  const sendMessage = useCallback((data) => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      const payload = typeof data === 'string' ? data : JSON.stringify(data)
      wsRef.current.send(payload)
    }
  }, [])

  /**
   * Register a callback that fires on every parsed incoming message.
   * Returns an unsubscribe function — call it in the useEffect cleanup.
   */
  const subscribe = useCallback((handler) => {
    subscribers.current.add(handler)
    return () => subscribers.current.delete(handler)
  }, [])

  return { lastMessage, status, sendMessage, subscribe }
}
