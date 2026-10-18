import { useEffect, useRef, useState } from 'react'
import Icon from './Icon'
import RichText from './RichText'

const PHASE_LABEL = {
  searching: 'Searching',
  thinking: 'Thinking',
}

const TYPE_ICON = { pdf: 'file', text: 'file', html: 'globe', url: 'link' }

/** Copies text, flipping the icon to a tick for a moment as feedback. */
function CopyButton({ text, label = 'Copy' }) {
  const [copied, setCopied] = useState(false)
  useEffect(() => {
    if (!copied) return
    const t = setTimeout(() => setCopied(false), 1400)
    return () => clearTimeout(t)
  }, [copied])

  return (
    <button
      className="actionbtn"
      onClick={() => navigator.clipboard?.writeText(text).then(() => setCopied(true))}
      title={copied ? 'Copied' : label}
      aria-label={label}
    >
      <Icon name={copied ? 'check' : 'copy'} size={15} />
    </button>
  )
}

/**
 * The sources a reply drew on, as their own section of the message rather than
 * chips mixed into the text. `highlight` is the number of a citation marker the
 * user just clicked in the answer.
 */
function SourcesSection({ sources, highlight, messageId }) {
  const [open, setOpen] = useState(true)

  // Clicking a marker opens the section so the highlighted card is visible.
  useEffect(() => {
    if (highlight != null) setOpen(true)
  }, [highlight])

  return (
    <section className={`sources ${open ? 'sources--open' : ''}`}>
      <button
        className="sources__head"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
      >
        <Icon name="layers" size={15} />
        <span>Sources</span>
        <span className="sources__count">{sources.length}</span>
        <Icon name="chevron" size={15} className="sources__chevron" />
      </button>

      {open && (
        <ol className="sources__list">
          {sources.map((s) => {
            const sub = s.section || (s.page != null ? `Page ${s.page}` : null)
            // Scores are cosine similarity; ~0.5 is the relevance floor and
            // ~0.9 a near-exact match, so map that range onto the meter.
            const strength =
              s.score != null ? Math.max(0.08, Math.min(1, (s.score - 0.45) / 0.45)) : null
            const isLink = s.source_type === 'url' && s.source
            const Tag = isLink ? 'a' : 'div'
            return (
              <li key={`${s.n}-${s.citation}`}>
                <Tag
                  id={`src-${messageId}-${s.n}`}
                  className={`source ${highlight === s.n ? 'source--hit' : ''}`}
                  // Links stay clickable, but never carry a referrer out.
                  {...(isLink
                    ? { href: s.source, target: '_blank', rel: 'noreferrer noopener' }
                    : {})}
                  title={s.source || s.citation}
                >
                  <span className="source__n">{s.n}</span>
                  <span className={`source__icon doc__icon--${s.source_type}`}>
                    <Icon name={TYPE_ICON[s.source_type] || 'file'} size={15} />
                  </span>
                  <span className="source__body">
                    <span className="source__title">{s.title || s.citation}</span>
                    <span className="source__sub">
                      {sub && <span>{sub}</span>}
                      {isLink && <span className="source__host">{hostOf(s.source)}</span>}
                    </span>
                  </span>
                  {strength != null && (
                    <span className="source__score" title={`Match ${s.score}`}>
                      <span className="meter">
                        <span className="meter__fill" style={{ width: `${strength * 100}%` }} />
                      </span>
                      <span className="source__pct">{Math.round(s.score * 100)}%</span>
                    </span>
                  )}
                </Tag>
              </li>
            )
          })}
        </ol>
      )}
    </section>
  )
}

function hostOf(url) {
  try {
    return new URL(url).host
  } catch {
    return url
  }
}

/** Shown until the first word arrives; after that the caret is enough. */
function StreamStatus({ phase }) {
  return (
    <div className="streamstatus" aria-live="polite">
      <span className="dots"><i /><i /><i /></span>
      <span>{PHASE_LABEL[phase] || 'Thinking'}</span>
    </div>
  )
}

/**
 * One message. Reasoning blocks arrive as a separate stream from the answer and
 * are rendered collapsed, so raw <think> tags never appear in the text.
 */
export default function MessageBubble({
  id,
  role,
  content,
  thinking,
  sources,
  streaming,
  phase,
}) {
  const [showThinking, setShowThinking] = useState(false)
  const [highlight, setHighlight] = useState(null)
  const clearRef = useRef(null)
  const isUser = role === 'user'

  useEffect(() => () => clearTimeout(clearRef.current), [])

  function handleCite(n) {
    setHighlight(n)
    // Wait a frame so an opening Sources section has rendered the card.
    requestAnimationFrame(() =>
      document
        .getElementById(`src-${id}-${n}`)
        ?.scrollIntoView({ block: 'nearest', behavior: 'smooth' }),
    )
    clearTimeout(clearRef.current)
    clearRef.current = setTimeout(() => setHighlight(null), 1800)
  }

  if (isUser) {
    return (
      <div className="msg msg--user">
        {/* The user's own text is shown verbatim; only model output is
            formatted, since the user did not write Markdown. */}
        <div className="msg__bubble">{content}</div>
      </div>
    )
  }

  const hasSources = sources?.length > 0

  return (
    <div className={`msg msg--assistant ${streaming ? 'msg--streaming' : ''}`}>
      <div className="msg__avatar" aria-hidden="true">
        <Icon name="sparkle" size={15} strokeWidth={2} />
      </div>

      <article className="msg__card">
        {streaming && !content && <StreamStatus phase={phase} />}

        {thinking && (
          <div className="thinking">
            <button
              className="thinking__toggle"
              onClick={() => setShowThinking((v) => !v)}
              aria-expanded={showThinking}
            >
              <Icon name="chevron" size={13} className={showThinking ? 'rot90' : ''} />
              Reasoning
            </button>
            {showThinking && <pre className="thinking__body">{thinking}</pre>}
          </div>
        )}

        {(content || !streaming) && (
          <div className="msg__body">
            <RichText
              text={content}
              onCite={hasSources ? handleCite : undefined}
              trailing={streaming ? <span key="caret" className="caret" /> : null}
            />
          </div>
        )}

        {hasSources && (
          <SourcesSection sources={sources} highlight={highlight} messageId={id} />
        )}

        {!streaming && content && (
          <div className="msg__actions">
            <CopyButton text={content} />
          </div>
        )}
      </article>
    </div>
  )
}
