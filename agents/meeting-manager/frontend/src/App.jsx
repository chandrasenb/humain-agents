import { useRef, useState } from 'react'
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
      {event.attendees.length > 0 && (
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
  if (!events || events.length === 0) return null
  return (
    <div className="event-carousel">
      {events.map((event, i) => (
        <EventCard key={`${event.title}-${event.start}-${i}`} event={event} />
      ))}
    </div>
  )
}

function ChatBubble({ message }) {
  return (
    <div className={`chat-bubble ${message.role}`}>
      <div className="chat-bubble-text">{message.text}</div>
      <EventCarousel events={message.events} />
    </div>
  )
}

export default function App() {
  const [messages, setMessages] = useState([])
  const [input, setInput] = useState('')
  const [sending, setSending] = useState(false)
  const [error, setError] = useState(null)
  // Conversation state the backend hands back each turn and expects
  // threaded straight back in on the next call — not component state,
  // since re-rendering on it would be pointless (nothing reads it directly).
  const conversationState = useRef(null)

  async function handleSend() {
    const text = input.trim()
    if (!text || sending) return

    setInput('')
    setError(null)
    setMessages((prev) => [...prev, { role: 'user', text }])
    setSending(true)

    try {
      const res = await fetch('/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message: text, state: conversationState.current }),
      })

      if (!res.ok) {
        const body = await res.json().catch(() => ({}))
        throw new Error(body.detail || `Request failed (${res.status})`)
      }

      const data = await res.json()
      conversationState.current = data.state
      setMessages((prev) => [...prev, { role: 'agent', text: data.reply, events: data.events }])
    } catch (err) {
      setError(err.message || 'Something went wrong')
    } finally {
      setSending(false)
    }
  }

  function handleKeyDown(e) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      handleSend()
    }
  }

  return (
    <div className="app">
      <header className="app-header">Meeting Manager</header>

      <div className="chat-history">
        {messages.length === 0 && (
          <div className="chat-empty">Ask about your calendar or schedule a meeting.</div>
        )}
        {messages.map((message, i) => (
          <ChatBubble key={i} message={message} />
        ))}
        {sending && <div className="chat-bubble agent typing">…</div>}
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
