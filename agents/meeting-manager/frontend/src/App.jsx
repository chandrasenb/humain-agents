import { useEffect, useRef, useState } from 'react'
import './App.css'

function formatTimeRange(start, end) {
  const s = new Date(start)
  const e = new Date(end)
  const dateOpts = { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' }
  const startStr = s.toLocaleString(undefined, dateOpts)
  const sameDay = s.toDateString() === e.toDateString()
  const endStr = sameDay
    ? e.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' })
    : e.toLocaleString(undefined, dateOpts)
  return `${startStr} – ${endStr}`
}

function EventCard({ event }) {
  return (
    <div className="event-card">
      <div className="event-card-title">{event.title}</div>
      <div className="event-card-time">{formatTimeRange(event.start, event.end)}</div>
      {event.attendees?.length > 0 && (
        <div className="event-card-attendees">{event.attendees.join(', ')}</div>
      )}
      {event.meeting_link && (
        <a
          className="event-card-link"
          href={event.meeting_link}
          target="_blank"
          rel="noreferrer"
        >
          Join meeting
        </a>
      )}
    </div>
  )
}

function EventCarousel({ events }) {
  const trackRef = useRef(null)
  const [canScrollLeft, setCanScrollLeft] = useState(false)
  const [canScrollRight, setCanScrollRight] = useState(false)
  const [activeDot, setActiveDot] = useState(0)

  useEffect(() => {
    const el = trackRef.current
    if (!el) return

    function update() {
      const maxScroll = el.scrollWidth - el.clientWidth
      const overflow = maxScroll > 4
      setCanScrollLeft(overflow && el.scrollLeft > 4)
      setCanScrollRight(overflow && el.scrollLeft < maxScroll - 4)
      if (overflow && el.clientWidth > 0) {
        const approx = Math.round(el.scrollLeft / Math.max(el.clientWidth * 0.7, 1))
        setActiveDot(Math.min(Math.max(approx, 0), events.length - 1))
      } else {
        setActiveDot(0)
      }
    }

    update()
    el.addEventListener('scroll', update, { passive: true })
    window.addEventListener('resize', update)
    return () => {
      el.removeEventListener('scroll', update)
      window.removeEventListener('resize', update)
    }
  }, [events])

  if (!events || events.length === 0) return null

  const showDots = events.length > 1

  return (
    <div className={`event-carousel-wrap${canScrollLeft ? ' fade-left' : ''}${canScrollRight ? ' fade-right' : ''}`}>
      <div className="event-carousel" ref={trackRef}>
        {events.map((event, i) => (
          <EventCard key={`${event.title}-${event.start}-${i}`} event={event} />
        ))}
      </div>
      {showDots && (
        <div className="event-carousel-dots" aria-hidden="true">
          {events.map((_, i) => (
            <span key={i} className={`event-carousel-dot${i === activeDot ? ' active' : ''}`} />
          ))}
        </div>
      )}
    </div>
  )
}

function TypingIndicator() {
  return (
    <div className="chat-bubble agent typing" aria-label="Assistant is thinking">
      <span className="typing-dots">
        <span className="typing-dot" />
        <span className="typing-dot" />
        <span className="typing-dot" />
      </span>
    </div>
  )
}

function ChatBubble({ message }) {
  return (
    <div className={`chat-bubble ${message.role}${message.error ? ' has-error' : ''}`}>
      {message.text ? <div className="chat-bubble-text">{message.text}</div> : null}
      {message.error && (
        <div className="chat-bubble-error">{message.error}</div>
      )}
      <EventCarousel events={message.events} />
    </div>
  )
}

/** Parse an SSE byte stream from fetch() — EventSource can't POST a body. */
async function consumeSse(response, onEvent) {
  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''

  while (true) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })

    // SSE events are separated by a blank line.
    const parts = buffer.split('\n\n')
    buffer = parts.pop() ?? ''

    for (const part of parts) {
      const dataLines = part
        .split('\n')
        .filter((line) => line.startsWith('data:'))
        .map((line) => line.slice(5).trimStart())
      if (dataLines.length === 0) continue
      const data = dataLines.join('\n')
      if (data === '[DONE]') {
        onEvent({ type: 'done_sentinel' })
        return
      }
      try {
        onEvent(JSON.parse(data))
      } catch {
        // Ignore malformed chunks rather than aborting the whole turn.
      }
    }
  }
}

export default function App() {
  const [messages, setMessages] = useState([])
  const [input, setInput] = useState('')
  const [sending, setSending] = useState(false)
  const [awaitingFirstToken, setAwaitingFirstToken] = useState(false)
  const [error, setError] = useState(null)
  const conversationState = useRef(null)
  const historyRef = useRef(null)
  // Index of the in-flight assistant bubble (so token appends don't race).
  const streamingIndex = useRef(-1)

  useEffect(() => {
    const el = historyRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [messages, awaitingFirstToken])

  async function handleSend() {
    const text = input.trim()
    if (!text || sending) return

    setInput('')
    setError(null)
    setSending(true)
    setAwaitingFirstToken(true)

    setMessages((prev) => {
      const next = [...prev, { role: 'user', text }, { role: 'agent', text: '', events: null }]
      streamingIndex.current = next.length - 1
      return next
    })

    try {
      const res = await fetch('/chat/stream', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
        body: JSON.stringify({ message: text, state: conversationState.current }),
      })

      if (!res.ok) {
        const body = await res.json().catch(() => ({}))
        throw new Error(body.detail || `Request failed (${res.status})`)
      }

      let sawDone = false

      await consumeSse(res, (event) => {
        if (event.type === 'token' && typeof event.content === 'string') {
          setAwaitingFirstToken(false)
          setMessages((prev) => {
            const next = [...prev]
            const idx = streamingIndex.current
            if (idx < 0 || !next[idx]) return prev
            next[idx] = { ...next[idx], text: (next[idx].text || '') + event.content }
            return next
          })
        } else if (event.type === 'done') {
          sawDone = true
          setAwaitingFirstToken(false)
          if (event.state) conversationState.current = event.state
          setMessages((prev) => {
            const next = [...prev]
            const idx = streamingIndex.current
            if (idx < 0 || !next[idx]) return prev
            next[idx] = {
              ...next[idx],
              text: event.reply ?? next[idx].text,
              events: event.events ?? null,
            }
            return next
          })
        } else if (event.type === 'error') {
          setAwaitingFirstToken(false)
          const detail = event.detail || 'Stream error'
          setMessages((prev) => {
            const next = [...prev]
            const idx = streamingIndex.current
            if (idx < 0 || !next[idx]) return prev
            // Keep any partial text that already arrived.
            next[idx] = {
              ...next[idx],
              error: detail,
            }
            return next
          })
          setError(detail)
        } else if (event.type === 'done_sentinel') {
          sawDone = true
        }
      })

      if (!sawDone) {
        // Connection closed without a final event — keep partial text.
        setAwaitingFirstToken(false)
        setMessages((prev) => {
          const next = [...prev]
          const idx = streamingIndex.current
          if (idx < 0 || !next[idx]) return prev
          if (!next[idx].text && !next[idx].error) {
            next[idx] = { ...next[idx], error: 'Connection interrupted' }
          } else if (next[idx].text && !next[idx].error) {
            next[idx] = { ...next[idx], error: 'Connection interrupted' }
          }
          return next
        })
        setError('Connection interrupted')
      }
    } catch (err) {
      setAwaitingFirstToken(false)
      const detail = err.message || 'Something went wrong'
      setMessages((prev) => {
        const next = [...prev]
        const idx = streamingIndex.current
        if (idx >= 0 && next[idx]) {
          // Preserve partial streamed text if any arrived before the failure.
          if (next[idx].text) {
            next[idx] = { ...next[idx], error: detail }
            return next
          }
          // Empty assistant bubble with no text — drop it and surface banner error.
          return next.slice(0, idx)
        }
        return prev
      })
      setError(detail)
    } finally {
      setSending(false)
      setAwaitingFirstToken(false)
      streamingIndex.current = -1
    }
  }

  function handleKeyDown(e) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      handleSend()
    }
  }

  // Show the bounce indicator only before the first token; once text starts
  // streaming the in-progress agent bubble already carries the content.
  const showTyping =
    awaitingFirstToken &&
    (messages.length === 0 || messages[messages.length - 1]?.text === '')

  return (
    <div className="app">
      <header className="app-header">Meeting Manager</header>

      <div className="chat-history" ref={historyRef}>
        {messages.length === 0 && (
          <div className="chat-empty">Ask about your calendar or schedule a meeting.</div>
        )}
        {messages.map((message, i) => {
          // While waiting for the first token, hide the empty agent shell and
          // show the typing indicator instead (avoids a blank grey bubble).
          if (
            showTyping &&
            i === messages.length - 1 &&
            message.role === 'agent' &&
            !message.text &&
            !message.error
          ) {
            return <TypingIndicator key={i} />
          }
          if (message.role === 'agent' && !message.text && !message.error && !message.events) {
            return null
          }
          return <ChatBubble key={i} message={message} />
        })}
      </div>

      {error && <div className="chat-error">{error}</div>}

      <div className="chat-input-row">
        <textarea
          className="chat-input"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={handleKeyDown}
          placeholder="Message Meeting Manager…"
          rows={1}
        />
        <button className="chat-send" onClick={handleSend} disabled={sending || !input.trim()}>
          Send
        </button>
      </div>
    </div>
  )
}
