import { useCallback, useEffect, useRef, useState } from 'react'
import ChatWindow from './components/ChatWindow'
import Composer from './components/Composer'
import Icon from './components/Icon'
import KnowledgeView from './components/KnowledgeView'
import SessionSidebar from './components/SessionSidebar'
import { StatusDot } from './components/HealthBadge'
import * as api from './api'

export default function App() {
  const [sessions, setSessions] = useState([])
  const [activeId, setActiveId] = useState(null)
  const [messages, setMessages] = useState([])
  const [input, setInput] = useState('')

  // 'chat' or 'knowledge'. The knowledge base is its own section rather than
  // an overlay on every conversation.
  const [view, setView] = useState('chat')

  const [streaming, setStreaming] = useState(false)
  const [streamText, setStreamText] = useState('')
  const [thinkingText, setThinkingText] = useState('')
  // What the in-flight reply is doing right now: 'searching' the documents,
  // 'thinking' before the first token, or 'writing' the answer.
  const [streamPhase, setStreamPhase] = useState(null)
  // Citations for the reply currently streaming; they arrive before the
  // first token, so they can be shown while the answer is still being written.
  const [streamSources, setStreamSources] = useState([])
  const [useRag, setUseRag] = useState(true)

  const [error, setError] = useState(null)
  const [health, setHealth] = useState(null)
  const [healthError, setHealthError] = useState(null)
  const [healthKey, setHealthKey] = useState(0)
  // Drawer state only matters on narrow screens; on desktop the sidebar is
  // always visible and this value is ignored by the stylesheet.
  const [sidebarOpen, setSidebarOpen] = useState(false)

  // Lets Stop, and unmounting, actually cancel an in-flight generation rather
  // than leaving the model running with the server-side lock held.
  const abortRef = useRef(null)
  const composerRef = useRef(null)

  // Stable identity, so passing it to a child cannot retrigger that child's
  // effects on every render.
  const bumpHealth = useCallback(() => setHealthKey((k) => k + 1), [])

  // One health fetch shared by the sidebar, the header and the knowledge view.
  useEffect(() => {
    let cancelled = false
    api
      .getHealth()
      .then((h) => !cancelled && (setHealth(h), setHealthError(null)))
      .catch((e) => !cancelled && setHealthError(e.message))
    return () => {
      cancelled = true
    }
  }, [healthKey])

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

  function openChat(id) {
    setActiveId(id)
    setView('chat')
    setSidebarOpen(false)
  }

  async function handleNew() {
    try {
      const session = await api.createSession()
      setActiveId(session.id)
      setMessages([])
      setView('chat')
      setSidebarOpen(false)
      await refreshSessions()
      composerRef.current?.focus()
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

  function handleSuggest(text) {
    setInput(text)
    composerRef.current?.focus()
  }

  async function handleSend() {
    const text = input.trim()
    if (!text || streaming) return

    setError(null)
    setInput('')
    setStreamText('')
    setThinkingText('')
    setStreamSources([])
    // Retrieval only runs when it is on and there is something to search.
    const searching = useRag && (health?.knowledge?.documents ?? 0) > 0
    setStreamPhase(searching ? 'searching' : 'thinking')
    setStreaming(true)
    setSidebarOpen(false)

    // Show the user's message immediately; the server has already stored it.
    // A negative id cannot collide with a real AUTOINCREMENT row id.
    setMessages((prev) => [
      ...prev,
      { id: -Date.now(), role: 'user', content: text },
    ])

    const controller = new AbortController()
    abortRef.current = controller

    let sessionForThisTurn = activeId
    // Tracked in plain variables as well as in state: on Stop we need the text
    // and sources accumulated so far, and reading them from state there would
    // give stale values captured when this handler was created.
    let accumulated = ''
    let sources = []
    let gotToken = false

    try {
      await api.streamChat({
        message: text,
        sessionId: activeId,
        useRag,
        signal: controller.signal,
        onEvent: (event) => {
          switch (event.type) {
            case 'session':
              // The server may have created the session for us.
              sessionForThisTurn = event.session_id
              if (event.session_id !== activeId) setActiveId(event.session_id)
              break
            case 'sources':
              sources = event.sources || []
              setStreamSources(sources)
              setStreamPhase((p) => (p === 'searching' ? 'thinking' : p))
              break
            case 'thinking':
              setThinkingText((t) => t + event.text)
              break
            case 'token':
              if (!gotToken) {
                gotToken = true
                setStreamPhase('writing')
              }
              accumulated += event.text
              setStreamText(accumulated)
              break
            case 'done':
              setMessages((prev) => [
                ...prev,
                {
                  id: event.message_id,
                  role: 'assistant',
                  content: event.content,
                  sources: event.sources?.length ? event.sources : null,
                },
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
            {
              id: -Date.now(),
              role: 'assistant',
              content: partial,
              sources: sources.length ? sources : null,
            },
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
      setStreamSources([])
      setStreamPhase(null)
      abortRef.current = null
      bumpHealth()
      refreshSessions()
    }
  }

  const title =
    view === 'knowledge'
      ? 'Knowledge'
      : sessions.find((s) => s.id === activeId)?.title || 'New chat'
  const documentCount = health?.knowledge?.documents ?? 0

  return (
    <div className={`app ${sidebarOpen ? 'app--drawer-open' : ''}`}>
      {/* Decorative backdrop the glass panels blur. */}
      <div className="backdrop" aria-hidden="true">
        <span className="blob blob--1" />
        <span className="blob blob--2" />
        <span className="blob blob--3" />
      </div>

      {/* Tap-anywhere-to-close layer; only rendered as a drawer on phones. */}
      <div
        className="scrim"
        onClick={() => setSidebarOpen(false)}
        aria-hidden="true"
      />

      <SessionSidebar
        view={view}
        onViewChange={(v) => {
          setView(v)
          setSidebarOpen(false)
        }}
        sessions={sessions}
        activeId={activeId}
        onSelect={openChat}
        onNew={handleNew}
        onRename={handleRename}
        onDelete={handleDelete}
        disabled={streaming}
        isOpen={sidebarOpen}
        onClose={() => setSidebarOpen(false)}
        health={health}
        healthError={healthError}
        documentCount={documentCount}
      />

      <main className="main glass">
        <header className="topbar">
          <button
            className="iconbtn topbar__menu"
            onClick={() => setSidebarOpen(true)}
            aria-label="Open menu"
          >
            <Icon name="menu" />
          </button>
          <div className="topbar__title">
            <span className="topbar__name">{title}</span>
          </div>
          <StatusDot health={health} error={healthError} />
        </header>

        {error && (
          <div className="banner banner--error" role="alert">
            <Icon name="alert" size={16} />
            <span className="banner__text">{error}</span>
            <button onClick={() => setError(null)} aria-label="Dismiss">
              <Icon name="x" size={14} />
            </button>
          </div>
        )}

        {view === 'chat' ? (
          <>
            <ChatWindow
              messages={messages}
              streamText={streamText}
              thinkingText={thinkingText}
              streamSources={streamSources}
              streamPhase={streamPhase}
              streaming={streaming}
              onSuggest={handleSuggest}
            />

            <Composer
              ref={composerRef}
              value={input}
              onChange={setInput}
              onSend={handleSend}
              onStop={handleStop}
              streaming={streaming}
              useRag={useRag}
              onToggleRag={() => setUseRag((v) => !v)}
              documentCount={documentCount}
            />
          </>
        ) : (
          <KnowledgeView health={health} onChanged={bumpHealth} />
        )}
      </main>
    </div>
  )
}
