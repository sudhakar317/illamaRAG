import { useState } from 'react'

/**
 * One message. Reasoning blocks arrive as a separate stream from the answer and
 * are rendered collapsed, so raw <think> tags never appear in the text.
 */
export default function MessageBubble({ role, content, thinking, streaming }) {
  const [showThinking, setShowThinking] = useState(false)
  const isUser = role === 'user'

  return (
    <div className={`msg msg--${isUser ? 'user' : 'assistant'}`}>
      <div className="msg__role">{isUser ? 'You' : 'Assistant'}</div>

      {thinking && (
        <div className="thinking">
          <button
            className="thinking__toggle"
            onClick={() => setShowThinking((v) => !v)}
          >
            {showThinking ? '▾' : '▸'} reasoning
          </button>
          {showThinking && <pre className="thinking__body">{thinking}</pre>}
        </div>
      )}

      <div className="msg__body">
        {content}
        {streaming && <span className="cursor" />}
      </div>
    </div>
  )
}
