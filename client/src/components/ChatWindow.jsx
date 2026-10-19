import { useEffect, useRef } from 'react'
import Icon from './Icon'
import MessageBubble from './MessageBubble'

const SUGGESTIONS = [
  { icon: 'book', title: 'Summarise', prompt: 'Give me a short summary of the documents in my knowledge base.' },
  { icon: 'sparkle', title: 'Explain', prompt: 'Explain how retrieval-augmented generation works, in simple terms.' },
  { icon: 'edit', title: 'Draft', prompt: 'Draft a polite email asking to reschedule a meeting to next week.' },
  { icon: 'layers', title: 'Brainstorm', prompt: 'Give me five ideas for a weekend side project I could build in Python.' },
]

export default function ChatWindow({
  messages,
  streamText,
  thinkingText,
  streamSources,
  streamPhase,
  streamTrace,
  streaming,
  onSuggest,
  onAnswer,
  onProposal,
}) {
  const endRef = useRef(null)
  const scrollRef = useRef(null)
  const pinnedRef = useRef(true)

  // Only auto-scroll while the view is already at the bottom, so reading back
  // through history isn't yanked away by incoming tokens.
  function handleScroll() {
    const el = scrollRef.current
    if (!el) return
    pinnedRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 120
  }

  useEffect(() => {
    if (pinnedRef.current) {
      endRef.current?.scrollIntoView({ block: 'end' })
    }
  }, [messages, streamText, thinkingText, streamSources, streamPhase, streamTrace])

  // A newly opened conversation always starts at its latest message.
  useEffect(() => {
    pinnedRef.current = true
  }, [messages.length === 0])

  const empty = messages.length === 0 && !streaming

  return (
    <div className="chat" ref={scrollRef} onScroll={handleScroll}>
      <div className="chat__inner">
        {empty && (
          <div className="hero">
            <span className="hero__mark">
              <Icon name="sparkle" size={28} strokeWidth={1.6} />
            </span>
            <h2 className="hero__title">How can I help?</h2>
            <div className="hero__chips">
              {SUGGESTIONS.map((s) => (
                <button
                  key={s.title}
                  className="suggestion"
                  onClick={() => onSuggest(s.prompt)}
                  title={s.prompt}
                >
                  <Icon name={s.icon} size={15} />
                  {s.title}
                </button>
              ))}
            </div>
          </div>
        )}

        {messages.map((m) => (
          <MessageBubble
            key={m.id}
            id={m.id}
            role={m.role}
            content={m.content}
            sources={m.sources}
            trace={m.trace}
            questions={m.questions}
            proposals={m.proposals}
            busy={streaming}
            onAnswer={(answers) => onAnswer(m, answers)}
            onProposal={(proposalId, action, edits) => onProposal(m.id, proposalId, action, edits)}
          />
        ))}

        {streaming && (
          <MessageBubble
            id="streaming"
            role="assistant"
            content={streamText}
            thinking={thinkingText}
            sources={streamSources}
            phase={streamPhase}
            trace={streamTrace}
            streaming
          />
        )}

        <div ref={endRef} className="chat__end" />
      </div>
    </div>
  )
}
