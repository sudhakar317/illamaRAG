import { useEffect, useRef } from 'react'

export default function Composer({ value, onChange, onSend, onStop, streaming }) {
  const ref = useRef(null)

  // Grow with the content instead of scrolling inside a one-line box.
  useEffect(() => {
    const el = ref.current
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
    // Enter sends; Shift+Enter inserts a newline.
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      if (!streaming && value.trim()) onSend()
    }
  }

  return (
    <div className="composer">
      <textarea
        ref={ref}
        className="composer__input"
        rows={1}
        value={value}
        placeholder={streaming ? 'Generating…' : 'Send a message (Enter to send, Shift+Enter for a new line)'}
        onChange={(e) => onChange(e.target.value)}
        onKeyDown={handleKeyDown}
        disabled={streaming}
      />
      {streaming ? (
        <button className="btn btn--stop" onClick={onStop}>
          Stop
        </button>
      ) : (
        <button className="btn btn--send" onClick={onSend} disabled={!value.trim()}>
          Send
        </button>
      )}
    </div>
  )
}
