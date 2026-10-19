import { forwardRef, useEffect, useImperativeHandle, useRef, useState } from 'react'
import Icon from './Icon'

const NARROW = '(max-width: 760px)'

/** True while the viewport is phone-sized. */
function useIsNarrow() {
  const [narrow, setNarrow] = useState(
    () => typeof window !== 'undefined' && window.matchMedia(NARROW).matches,
  )

  useEffect(() => {
    const query = window.matchMedia(NARROW)
    const onChange = (e) => setNarrow(e.matches)
    query.addEventListener('change', onChange)
    return () => query.removeEventListener('change', onChange)
  }, [])

  return narrow
}

const Composer = forwardRef(function Composer(
  {
    value,
    onChange,
    onSend,
    onStop,
    streaming,
    useRag,
    onToggleRag,
    agentMode,
    onToggleAgent,
    documentCount,
  },
  ref,
) {
  const inputRef = useRef(null)
  const narrow = useIsNarrow()

  useImperativeHandle(ref, () => ({ focus: () => inputRef.current?.focus() }), [])

  // Grow with the content instead of scrolling inside a one-line box.
  useEffect(() => {
    const el = inputRef.current
    if (!el) return

    // When empty, hand height back to the stylesheet rather than measuring.
    // On first paint the effect can run before Vite has injected the CSS, and
    // measuring an unstyled textarea returns a wildly wrong scrollHeight that
    // then sticks as an inline style.
    if (!value) {
      el.style.height = ''
      return
    }

    el.style.height = 'auto'
    el.style.height = `${Math.min(el.scrollHeight, 200)}px`
  }, [value])

  function handleKeyDown(e) {
    // Enter sends on desktop, where Shift+Enter is available for a newline.
    // On a phone keyboard there is no practical Shift+Enter, so Enter inserts
    // a newline there and sending is done with the button.
    if (narrow) return
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      if (!streaming && value.trim()) onSend()
    }
  }

  const ragTitle = !useRag
    ? 'Documents off'
    : documentCount
      ? `Documents on (${documentCount})`
      : 'Documents on (none added)'

  return (
    <div className="composer">
      <div className="composer__box glass">
        <button
          className={`ragbtn ${agentMode ? 'ragbtn--on' : ''}`}
          onClick={onToggleAgent}
          role="switch"
          aria-checked={agentMode}
          aria-label="Agent mode"
          title={
            agentMode
              ? 'Agent on: reasons over claims, asks when the documents fall short'
              : 'Agent off: plain chat'
          }
          disabled={streaming}
        >
          <Icon name="cpu" size={18} />
        </button>

        {/* The agent always searches the documents, so the switch would only
            mislead while it is on. */}
        {!agentMode && (
          <button
            className={`ragbtn ${useRag ? 'ragbtn--on' : ''}`}
            onClick={onToggleRag}
            role="switch"
            aria-checked={useRag}
            aria-label="Use documents"
            title={ragTitle}
          >
            <Icon name="book" size={18} />
            {useRag && documentCount > 0 && <span className="ragbtn__count">{documentCount}</span>}
          </button>
        )}

        <textarea
          ref={inputRef}
          className="composer__input"
          rows={1}
          value={value}
          placeholder={agentMode ? 'Ask the agent…' : 'Message…'}
          onChange={(e) => onChange(e.target.value)}
          onKeyDown={handleKeyDown}
          disabled={streaming}
          aria-label="Message"
        />

        {streaming ? (
          <button className="sendbtn sendbtn--stop" onClick={onStop} aria-label="Stop" title="Stop">
            <Icon name="stop" size={14} strokeWidth={0} className="icon--fill" />
          </button>
        ) : (
          <button
            className="sendbtn"
            onClick={onSend}
            disabled={!value.trim()}
            aria-label="Send"
            title="Send"
          >
            <Icon name="send" size={18} strokeWidth={2.2} />
          </button>
        )}
      </div>
    </div>
  )
})

export default Composer
