import { useEffect, useRef } from 'react'
import MessageBubble from './MessageBubble'

export default function ChatWindow({ messages, streamText, thinkingText, streaming }) {
  const endRef = useRef(null)
  const scrollRef = useRef(null)
  const pinnedRef = useRef(true)

  // Only auto-scroll while the view is already at the bottom, so reading back
  // through history isn't yanked away by incoming tokens.
  function handleScroll() {
    const el = scrollRef.current
    if (!el) return
    pinnedRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80
  }

  useEffect(() => {
    if (pinnedRef.current) {
      endRef.current?.scrollIntoView({ block: 'end' })
    }
  }, [messages, streamText, thinkingText])

  const empty = messages.length === 0 && !streaming

  return (
    <div className="chat" ref={scrollRef} onScroll={handleScroll}>
      {empty && (
        <div className="chat__empty">
          <h2>Local chat, running on your machine</h2>
          <p>
            Qwen3.5-4B via llama.cpp. Nothing leaves this computer. Replies come
            in at a few tokens per second on CPU — that is expected.
          </p>
        </div>
      )}

      {messages.map((m) => (
        <MessageBubble key={m.id} role={m.role} content={m.content} />
      ))}

      {streaming && (
        <MessageBubble
          role="assistant"
          content={streamText}
          thinking={thinkingText}
          streaming
        />
      )}

      <div ref={endRef} />
    </div>
  )
}
