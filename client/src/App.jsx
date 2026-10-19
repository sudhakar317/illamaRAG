import { useCallback, useEffect, useRef, useState } from 'react'
import ChatWindow from './components/ChatWindow'
import Composer from './components/Composer'
import Icon from './components/Icon'
import KnowledgeView from './components/KnowledgeView'
import SessionSidebar from './components/SessionSidebar'
import { StatusDot } from './components/HealthBadge'
import * as api from './api'

/**
 * Redraw the agent's side of a session after a reload. Each agent message
 * names its run in meta; the run supplies the steps and claims behind it.
 * Open questions go on that run's latest question message, and undecided
 * proposals on its answer.
 */
function withAgentState(messages, state) {
  if (!state) return messages
  const runs = state.runs || {}
  const pending = state.pending
  const proposalsByRun = {}
  for (const p of state.proposals || []) (proposalsByRun[p.run_id] ||= []).push(p)

  const lastQuestion = pending
    ? messages.findLastIndex(
        (m) => m.meta?.run_id === pending.run_id && m.meta?.kind === 'questions',
      )
    : -1

  return messages.map((m, i) => {
    const runId = m.meta?.run_id
    if (!runId || !runs[runId]) return m
    const run = runs[runId]
    const next = { ...m, trace: { claims: run.claims, steps: run.steps } }
    if (m.meta.kind === 'questions') {
      next.questions = {
        runId,
        items: i === lastQuestion ? pending.questions : [],
        answered: i !== lastQuestion,
      }
    }
    if (m.meta.kind === 'answer' && proposalsByRun[runId]) next.proposals = proposalsByRun[runId]
    return next
  })
}

/** Replace a step by id, or add it. Steps always arrive whole. */
function upsertStep(steps, step) {
  const i = steps.findIndex((s) => s.id === step.id)
  if (i === -1) return [...steps, step]
  const next = [...steps]
  next[i] = step
  return next
}

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

  // Agent mode routes a message through the claim tree instead of plain chat.
  // Remembered per browser; it is a preference, not data.
  const [agentMode, setAgentMode] = useState(() => {
    try {
      return localStorage.getItem('agentMode') !== 'off'
    } catch {
      return true
    }
  })
  // The in-flight run's tree and step log, drawn while it reasons.
  const [streamTrace, setStreamTrace] = useState(null)

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
    Promise.all([
      api.getMessages(activeId),
      // Agent state is a nicety on top of the transcript; never let it block it.
      api.getAgentState(activeId).catch(() => null),
    ])
      .then(([m, state]) => !cancelled && setMessages(withAgentState(m, state)))
      .catch((e) => !cancelled && setError(e.message))
    return () => {
      cancelled = true
    }
  }, [activeId])

  useEffect(() => {
    try {
      localStorage.setItem('agentMode', agentMode ? 'on' : 'off')
    } catch {
      // Storage unavailable (private window); the toggle still works this visit.
    }
  }, [agentMode])

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
    if (agentMode) {
      setInput('')
      return runAgent({ message: text, shown: text })
    }

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

  /**
   * Run the agent for a new question, or resume a run with answers.
   * `shown` is what appears as the user's own message; `base` is the trace of
   * the question turn being answered, so a resumed run keeps its history.
   */
  async function runAgent({ message, runId, answers, shown, base }) {
    setError(null)
    setStreamText('')
    setThinkingText('')
    setStreamSources([])
    setStreamPhase('agent')
    setStreaming(true)
    setSidebarOpen(false)

    // Optimistic: the user's message is on screen before the server replies.
    setMessages((prev) => [...prev, { id: -Date.now(), role: 'user', content: shown }])

    const controller = new AbortController()
    abortRef.current = controller

    // Plain variables alongside state, for the same reason as handleSend:
    // event handlers below would otherwise read values captured at creation.
    const trace = {
      claims: base?.claims || [],
      steps: base?.steps || [],
      progress: null,
      // Client time each step began, so its clock ticks between events.
      stepStarts: {},
    }
    let accumulated = ''
    let sessionForThisTurn = activeId
    let currentRun = runId
    // The message this turn ends on, which takes the final trace at the end:
    // steps keep closing (writing, drafting a note) after it first appears.
    let lastMessageId = null
    const publish = () => setStreamTrace({ ...trace })
    const snapshot = () => ({ claims: trace.claims, steps: trace.steps })
    publish()

    try {
      await api.streamAgent({
        message,
        sessionId: activeId,
        runId,
        answers,
        signal: controller.signal,
        onEvent: (event) => {
          switch (event.type) {
            case 'session':
              sessionForThisTurn = event.session_id
              if (event.session_id !== activeId) setActiveId(event.session_id)
              break
            case 'run':
              currentRun = event.run_id
              break
            case 'step':
              if (event.closed) trace.steps = upsertStep(trace.steps, event.closed)
              if (event.step) {
                if (!trace.stepStarts[event.step.id]) {
                  trace.stepStarts = { ...trace.stepStarts, [event.step.id]: Date.now() }
                }
                trace.steps = upsertStep(trace.steps, event.step)
              }
              publish()
              break
            case 'progress':
              trace.progress = event
              publish()
              break
            case 'tree':
              trace.claims = event.claims
              publish()
              break
            case 'questions':
              // The run pauses here; the questions become their own message.
              setMessages((prev) => [
                ...prev,
                {
                  id: event.message_id,
                  role: 'assistant',
                  // Matches what the server stored, so the transcript reads
                  // the same once the card is answered or the page reloads.
                  content: [
                    'I need a little more to answer this properly:',
                    ...event.questions.map((q, i) => `${i + 1}. ${q.text}`),
                  ].join('\n'),
                  meta: { run_id: event.run_id, kind: 'questions' },
                  trace: snapshot(),
                  questions: { runId: event.run_id, items: event.questions, answered: false },
                },
              ])
              lastMessageId = event.message_id
              break
            case 'thinking':
              setThinkingText((t) => t + event.text)
              break
            case 'token':
              if (!accumulated) setStreamPhase('writing')
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
                  meta: { run_id: currentRun, kind: 'answer' },
                  trace: snapshot(),
                },
              ])
              lastMessageId = event.message_id
              accumulated = ''
              setStreamText('')
              setThinkingText('')
              // Proposals may still follow, so keep showing progress.
              setStreamPhase('agent')
              break
            case 'proposals':
              setMessages((prev) =>
                prev.map((m) =>
                  m.id === event.message_id ? { ...m, proposals: event.items } : m,
                ),
              )
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
        if (accumulated.trim()) {
          setMessages((prev) => [
            ...prev,
            { id: -Date.now(), role: 'assistant', content: accumulated.trim(), trace: snapshot() },
          ])
        }
      } else {
        setError(e.message)
        // A refused resume (already answered, expired) leaves the screen
        // out of step with the server; reload the transcript to match it.
        if (sessionForThisTurn) reloadTranscript(sessionForThisTurn)
      }
    } finally {
      if (lastMessageId != null) {
        const final = snapshot()
        setMessages((prev) =>
          prev.map((m) => (m.id === lastMessageId ? { ...m, trace: final } : m)),
        )
      }
      setStreaming(false)
      setStreamText('')
      setThinkingText('')
      setStreamPhase(null)
      setStreamTrace(null)
      abortRef.current = null
      bumpHealth()
      refreshSessions()
    }
  }

  function reloadTranscript(sessionId) {
    Promise.all([api.getMessages(sessionId), api.getAgentState(sessionId).catch(() => null)])
      .then(([m, state]) => setMessages(withAgentState(m, state)))
      .catch(() => {})
  }

  function handleAnswer(message, answers) {
    if (streaming) return
    // Lock the card at once, so a double click cannot resume the run twice.
    setMessages((prev) =>
      prev.map((m) =>
        m.id === message.id ? { ...m, questions: { ...m.questions, answered: true } } : m,
      ),
    )
    const shown = message.questions.items
      .map((q) => `${q.text}\n${answers[q.claim_id] || "I don't know"}`)
      .join('\n\n')
    runAgent({ runId: message.questions.runId, answers, shown, base: message.trace })
  }

  /**
   * Save or discard a proposal. Optimistic: the card shows the outcome at
   * once and is put back if the server refuses.
   */
  async function handleProposal(messageId, proposalId, action, edits) {
    const setStatus = (update) =>
      setMessages((prev) =>
        prev.map((m) =>
          m.id === messageId
            ? { ...m, proposals: m.proposals.map((p) => (p.id === proposalId ? update(p) : p)) }
            : m,
        ),
      )
    let before
    setStatus((p) => {
      before = p
      return { ...p, ...edits, status: action === 'save' ? 'saving' : 'discarded' }
    })
    try {
      const updated =
        action === 'save'
          ? await api.saveProposal(proposalId, edits)
          : await api.discardProposal(proposalId)
      setStatus(() => updated)
      // A saved note is a new document; the count in the sidebar should move.
      if (action === 'save') bumpHealth()
    } catch (e) {
      if (before) setStatus(() => before)
      setError(e.message)
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
              streamTrace={streamTrace}
              streaming={streaming}
              onSuggest={handleSuggest}
              onAnswer={handleAnswer}
              onProposal={handleProposal}
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
              agentMode={agentMode}
              onToggleAgent={() => setAgentMode((v) => !v)}
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
