import { useRef, useState } from 'react'
import './App.css'

const BACKEND_URL =
  (import.meta.env.VITE_BACKEND_URL || 'http://localhost:8000').replace(/\/$/, '')

function newSessionId() {
  return 's-' + Math.random().toString(16).slice(2, 10) + Date.now().toString(16).slice(-4)
}

export default function App() {
  const [sessionId] = useState(newSessionId)
  const [messages, setMessages] = useState([])
  const [input, setInput] = useState('')
  const [systemPrompt, setSystemPrompt] = useState('')
  const [sending, setSending] = useState(false)
  const inputRef = useRef(null)

  async function sendMessage() {
    const text = input.trim()
    if (!text || sending) return
    setInput('')
    setSending(true)
    setMessages((prev) => [...prev, { role: 'user', content: text }])
    try {
      const res = await fetch(`${BACKEND_URL}/chat`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          session_id: sessionId,
          message: text,
          ...(systemPrompt.trim() ? { system_prompt: systemPrompt.trim() } : {}),
        }),
      })
      const data = await res.json().catch(() => ({}))
      if (!res.ok) {
        const detail =
          data.detail ||
          (res.status === 503
            ? 'All providers unavailable — please try again in a moment.'
            : 'Something went wrong — please try again.')
        setMessages((prev) => [...prev, { role: 'error', content: detail }])
        return
      }
      setMessages((prev) => [
        ...prev,
        { role: 'assistant', content: data.reply, metadata: data.metadata },
      ])
    } catch (err) {
      setMessages((prev) => [
        ...prev,
        {
          role: 'error',
          content: 'Could not reach the backend. Is it running at ' + BACKEND_URL + '?',
        },
      ])
    } finally {
      setSending(false)
      inputRef.current?.focus()
    }
  }

  function onKeyDown(e) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      sendMessage()
    }
  }

  return (
    <div className="app">
      <header className="header">
        <h1>Lyftr AI Chatbot</h1>
        <p className="subtitle">
          LLM chat with key failover and response caching · Session{' '}
          <code>{sessionId}</code>
        </p>
        <label className="system-label">
          System prompt (optional)
          <input
            data-testid="system-prompt-input"
            type="text"
            value={systemPrompt}
            onChange={(e) => setSystemPrompt(e.target.value)}
            placeholder="e.g. Answer concisely, in simple English."
          />
        </label>
      </header>

      <main className="chat">
        {messages.length === 0 && (
          <p className="empty">
            Say hello — ask anything. The bot remembers this conversation.
          </p>
        )}
        {messages.map((m, i) =>
          m.role === 'user' ? (
            <div key={i} className="msg user-msg">
              {m.content}
            </div>
          ) : m.role === 'error' ? (
            <div key={i} className="msg error-msg">
              {m.content}
            </div>
          ) : (
            <div key={i} className="msg assistant-msg" data-testid="assistant-message">
              <div className="reply">{m.content}</div>
              {m.metadata && (
                <div className="meta">
                  <span>
                    Source:{' '}
                    <strong data-testid="meta-source">
                      {m.metadata.resolution_source}
                    </strong>
                  </span>
                  <span>
                    Model: <strong data-testid="meta-model">{m.metadata.model}</strong>
                  </span>
                  <span>
                    Latency:{' '}
                    <strong data-testid="meta-latency">
                      {m.metadata.latency_ms} ms
                    </strong>
                  </span>
                  <span>
                    Input tokens:{' '}
                    <strong data-testid="meta-input-tokens">
                      {m.metadata.input_tokens}
                    </strong>
                  </span>
                  <span>
                    Output tokens:{' '}
                    <strong data-testid="meta-output-tokens">
                      {m.metadata.output_tokens}
                    </strong>
                  </span>
                  {m.metadata.key_id && (
                    <span>
                      Key: <strong>{m.metadata.key_id}</strong>
                    </span>
                  )}
                  {m.metadata.fallback_attempts > 0 && (
                    <span>
                      Fallbacks: <strong>{m.metadata.fallback_attempts}</strong>
                    </span>
                  )}
                </div>
              )}
            </div>
          ),
        )}
        {sending && <p className="typing">Thinking…</p>}
      </main>

      <footer className="composer">
        <textarea
          ref={inputRef}
          data-testid="chat-input"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={onKeyDown}
          placeholder="Type your message…"
          rows={2}
        />
        <button data-testid="send-button" onClick={sendMessage} disabled={sending}>
          Send
        </button>
      </footer>
    </div>
  )
}
