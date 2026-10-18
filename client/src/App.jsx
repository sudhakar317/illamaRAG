import { useCallback, useEffect, useRef, useState } from 'react'
import ChatWindow from './components/ChatWindow'
import Composer from './components/Composer'
import HealthBadge from './components/HealthBadge'
import SessionSidebar from './components/SessionSidebar'
import * as api from './api'

export default function App() {
  const [sessions, setSessions] = useState([])
  const [activeId, setActiveId] = useState(null)
  const [messages, setMessages] = useState([])
  const [input, setInput] = useState('')

  const [streaming, setStreaming] = useState(false)
  const [streamText, setStreamText] = useState('')
  const [thinkingText, setThinkingText] = useState('')
  const [error, setError] = useState(null)
  const [healthKey, setHealthKey] = useState(0)

  // Lets Stop, and unmounting, actually cancel an in-flight generation rather
  // than leaving the model running with the server-side lock held.
  const abortRef = useRef(null)

  const refreshSessions = useCallback(async () => {
    try {
      setSessions(await api.listSessions())
    } catch (e) {
      setError(e.message)
    }
  }, [])

  useEffect(() => {
    refreshSessions()
  }, [refreshSessions])

  // Load the transcript whenever the selected session changes. This is what
  // makes history survive a page reload.
  useEffect(() => {
    if (!activeId) {
      setMessages([])
      return
    }
    let cancelled = false
    api
      .getMessages(activeId)
      .then((m) => !cancelled && setMessages(m))
      .catch((e) => !cancelled && setError(e.message))
    return () => {
      cancelled = true
    }
  }, [activeId])

  useEffect(() => () => abortRef.current?.abort(), [])

  async function handleNew() {
    try {
      const session = await api.createSession()
      setActiveId(session.id)
      setMessages([])
      await refreshSessions()
    } catch (e) {
      setError(e.message)
    }
  }

  async function handleRename(id, title) {
    try {
      await api.renameSession(id, title)
      await refreshSessions()
    } catch (e) {
      setError(e.message)
    }
  }

  async function handleDelete(id) {
    try {
      await api.deleteSession(id)
      if (id === activeId) {
        setActiveId(null)
        setMessages([])
      }
      await refreshSessions()
    } catch (e) {
      setError(e.message)
    }
  }

  function handleStop() {
    abortRef.current?.abort()
  }

  async function handleSend() {
    const text = input.trim()
    if (!text || streaming) return

    setError(null)
    setInput('')
    setStreamText('')
    setThinkingText('')
    setStreaming(true)

    // Show the user's message immediately; the server has already stored it.
    // A negative id cannot collide with a real AUTOINCREMENT row id.
    setMessages((prev) => [
      ...prev,
      { id: -Date.now(), role: 'user', content: text },
    ])

    const controller = new AbortController()
    abortRef.current = controller

    let sessionForThisTurn = activeId
    // Tracked in a plain variable as well as in state: on Stop we need the text
    // accumulated so far, and reading it from state there would give a stale
    // value captured when this handler was created.
    let accumulated = ''

    try {
      await api.streamChat({
        message: text,
        sessionId: activeId,
        signal: controller.signal,
        onEvent: (event) => {
          switch (event.type) {
            case 'session':
              // The server may have created the session for us.
              sessionForThisTurn = event.session_id
              if (event.session_id !== activeId) setActiveId(event.session_id)
              break
            case 'thinking':
              setThinkingText((t) => t + event.text)
              break
            case 'token':
              accumulated += event.text
              setStreamText(accumulated)
              break
            case 'done':
              setMessages((prev) => [
                ...prev,
                { id: event.message_id, role: 'assistant', content: event.content },
              ])
              // Cleared so the abort handler below does not add it a second time.
              accumulated = ''
              setStreamText('')
              setThinkingText('')
              break
            case 'error':
              setError(event.message)
              break
            default:
              break
          }
        },
      })
    } catch (e) {
      if (e.name === 'AbortError') {
        // Stopped on purpose. Keep the text already on screen rather than
        // refetching straight away: the server needs a moment to notice the
        // disconnect and write the partial reply, so an immediate reload would
        // race that write and show nothing.
        if (accumulated.trim()) {
          const partial = accumulated.trim()
          setMessages((prev) => [
            ...prev,
            { id: -Date.now(), role: 'assistant', content: partial },
          ])
        }
        // Reconcile with what the server actually stored, once it has settled.
        if (sessionForThisTurn) {
          setTimeout(async () => {
            try {
              const saved = await api.getMessages(sessionForThisTurn)
              setMessages((prev) =>
                // Only adopt the server copy if we are still on this session
                // and it is at least as complete as what is on screen.
                saved.length >= prev.length ? saved : prev,
              )
              refreshSessions()
            } catch {
              // Non-fatal: the next session switch will refresh it.
            }
          }, 1500)
        }
      } else {
        setError(e.message)
      }
    } finally {
      setStreaming(false)
      setStreamText('')
      setThinkingText('')
      abortRef.current = null
      setHealthKey((k) => k + 1)
      refreshSessions()
    }
  }

  return (
    <div className="app">
      <SessionSidebar
        sessions={sessions}
        activeId={activeId}
        onSelect={setActiveId}
        onNew={handleNew}
        onRename={handleRename}
        onDelete={handleDelete}
        disabled={streaming}
      />

      <main className="main">
        <header className="topbar">
          <div className="topbar__title">
            {sessions.find((s) => s.id === activeId)?.title || 'New chat'}
          </div>
          <HealthBadge refreshKey={healthKey} />
        </header>

        {error && (
          <div className="banner banner--error">
            {error}
            <button onClick={() => setError(null)}>dismiss</button>
          </div>
        )}

        <ChatWindow
          messages={messages}
          streamText={streamText}
          thinkingText={thinkingText}
          streaming={streaming}
        />

        <Composer
          value={input}
          onChange={setInput}
          onSend={handleSend}
          onStop={handleStop}
          streaming={streaming}
        />
      </main>
    </div>
  )
}
