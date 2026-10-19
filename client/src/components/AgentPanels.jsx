import { useEffect, useRef, useState } from 'react'
import Icon from './Icon'

/*
 * The agent's surfaces inside a message:
 *
 *   AgentWork      what it is doing right now, live, then a timeline of every
 *                  step it took and the claim tree it reasoned over
 *   AnswerBasis    one line under an answer: what rests on documents, on you,
 *                  and what is still not established
 *   QuestionCard   its questions, answered in place
 *   ProposalCard   what it learned from you, held for approval
 *
 * All of it is drawn from server events; nothing here decides anything.
 */

const STATE = {
  open: { label: 'Searching', tone: 'busy' },
  blocked: { label: 'Waiting', tone: 'idle' },
  unsearchable: { label: 'Ask you', tone: 'busy' },
  asked: { label: 'Asked', tone: 'busy' },
  exhausted: { label: 'Not found', tone: 'warn' },
  unknown: { label: 'Unknown', tone: 'warn' },
  supported: { label: 'Confirmed', tone: 'ok' },
  answered: { label: 'Answered', tone: 'ok' },
  refuted: { label: 'Contradicted', tone: 'bad' },
}

const DONT_KNOW = "I don't know"

/** "850ms", "4.2s", "1m 42s" */
export function formatDuration(ms) {
  if (ms == null) return ''
  if (ms < 1000) return `${Math.round(ms)}ms`
  const s = ms / 1000
  if (s < 60) return `${s.toFixed(1)}s`
  // Round once, then split: rounding the remainder alone gives "2m 60s".
  const whole = Math.round(s)
  return `${Math.floor(whole / 60)}m ${whole % 60}s`
}

/** " · 41 tokens" while writing, " · reading" before the first token. */
function progressText(progress) {
  if (!progress) return ''
  if (progress.tokens) return ` · ${progress.tokens} tokens`
  return progress.phase ? ` · ${progress.phase}` : ''
}

/** Re-renders every `ms` while `active`, for a ticking clock. */
function useTick(active, ms = 250) {
  const [, setN] = useState(0)
  useEffect(() => {
    if (!active) return undefined
    const t = setInterval(() => setN((n) => n + 1), ms)
    return () => clearInterval(t)
  }, [active, ms])
}

// ------------------------------------------------------------------ claims

function ClaimNode({ claim, childrenOf }) {
  const state = STATE[claim.state] || { label: claim.state, tone: 'idle' }
  const kids = childrenOf(claim.id)
  const aboutYou = claim.kind === 'profile_fact'
  const stated = claim.origin === 'stated'

  return (
    <li className={`claim claim--${state.tone}`}>
      <div className="claim__row">
        <span className="claim__id">{claim.id}</span>
        <span className="claim__text">{claim.text}</span>
        <span className={`claim__kind ${aboutYou || stated ? 'claim__kind--you' : ''}`}>
          {stated ? 'you said' : aboutYou ? 'about you' : 'documents'}
        </span>
        <span className={`claim__state claim__state--${state.tone}`}>{state.label}</span>
      </div>

      {claim.answer && !stated && (
        <div className="claim__detail claim__answer">
          <Icon name="check" size={12} />
          <span>
            {claim.answer_source === 'profile' ? 'From your profile: ' : 'You said: '}
            {claim.answer}
          </span>
        </div>
      )}

      {claim.evidence?.map((e, i) => (
        <div key={i} className="claim__detail claim__quote">
          <span>“{e.quote}”</span>
          <span className="claim__cite">{e.citation}</span>
        </div>
      ))}

      {!claim.evidence?.length && claim.related?.length > 0 && (
        <div className="claim__detail claim__quote claim__quote--related">
          <span className="claim__cite">Closest passage, does not settle it</span>
          <span>“{claim.related[0].snippet}”</span>
          <span className="claim__cite">{claim.related[0].citation}</span>
        </div>
      )}

      {claim.queries_tried?.length > 0 && (
        <div className="claim__queries" title="Searches tried, in order">
          {claim.queries_tried.map((q, i) => (
            <code key={i}>{q}</code>
          ))}
        </div>
      )}

      {kids.length > 0 && (
        <ul className="tree tree--nested">
          {kids.map((k) => (
            <ClaimNode key={k.id} claim={k} childrenOf={childrenOf} />
          ))}
        </ul>
      )}
    </li>
  )
}

function ClaimTree({ claims }) {
  const ids = new Set(claims.map((c) => c.id))
  const childrenOf = (id) => claims.filter((c) => c.depends_on === id)
  const roots = claims.filter((c) => !c.depends_on || !ids.has(c.depends_on))
  return (
    <ul className="tree">
      {roots.map((c) => (
        <ClaimNode key={c.id} claim={c} childrenOf={childrenOf} />
      ))}
    </ul>
  )
}

// ------------------------------------------------------------------- steps

function StepRow({ step, startedAt, progress }) {
  const running = step.status === 'running'
  useTick(running)
  const elapsed = running ? Date.now() - (startedAt || Date.now()) : step.ms
  return (
    <li className={`step step--${step.status}`}>
      <span className="step__mark" aria-hidden="true">
        {running ? <span className="spinner" /> : <Icon name="check" size={12} strokeWidth={2.6} />}
      </span>
      <div className="step__body">
        <div className="step__head">
          <span className="step__label">{step.label}</span>
          <span className="step__time">
            {formatDuration(elapsed)}
            {running && progressText(progress)}
          </span>
        </div>
        {step.details?.length > 0 && (
          <ul className="step__details">
            {step.details.map((d, i) => (
              <li key={i}>{d}</li>
            ))}
          </ul>
        )}
      </div>
    </li>
  )
}

/**
 * What the agent is doing, while it does it, and how it got there afterwards.
 *
 * `trace` = { claims, steps, progress, stepStarts }. stepStarts maps a step id
 * to the client time it started, so a running step's clock ticks locally
 * between server events instead of jumping when the next one lands.
 */
export function AgentWork({ trace, live, answering = false, defaultOpen = false }) {
  const [open, setOpen] = useState(defaultOpen || live)
  const wasLive = useRef(live)
  const steps = trace?.steps || []
  const claims = trace?.claims || []
  const running = steps.find((s) => s.status === 'running')
  useTick(live)

  // Open while live so the work is watched as it happens; fold away once
  // it finishes, leaving the answer in front.
  useEffect(() => {
    if (live) setOpen(true)
    else if (wasLive.current) setOpen(defaultOpen)
    wasLive.current = live
  }, [live, defaultOpen])

  // Once the answer itself is streaming, the timeline makes way for it; the
  // header stays, still saying what is happening.
  useEffect(() => {
    if (answering) setOpen(false)
  }, [answering])

  if (!live && !steps.length && !claims.length) return null

  const total = steps.reduce((sum, s) => sum + (s.ms || 0), 0)
  const startedAt = running ? trace?.stepStarts?.[running.id] : null
  const progress = running && trace?.progress?.step === running.id ? trace.progress : null

  let summary
  if (live) {
    const label = running?.label || 'Starting'
    const elapsed = running ? Date.now() - (startedAt || Date.now()) : 0
    summary = (
      <>
        <span className="spinner" />
        <span className="work__now">{label}</span>
        <span className="work__meta">
          {running ? formatDuration(elapsed) : ''}
          {progressText(progress)}
        </span>
      </>
    )
  } else {
    summary = (
      <>
        <Icon name="layers" size={14} />
        <span className="work__now">
          Worked through {steps.length} step{steps.length === 1 ? '' : 's'}
        </span>
        <span className="work__meta">{total ? `in ${formatDuration(total)}` : ''}</span>
      </>
    )
  }

  return (
    <section className={`work ${live ? 'work--live' : ''} ${open ? 'work--open' : ''}`}>
      <button
        className="work__head"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        aria-live={live ? 'polite' : undefined}
      >
        {summary}
        <Icon name="chevron" size={14} className="work__chevron" />
      </button>

      {open && (
        <div className="work__body">
          {steps.length > 0 && (
            <ol className="steps">
              {steps.map((s) => (
                <StepRow
                  key={s.id}
                  step={s}
                  startedAt={trace?.stepStarts?.[s.id]}
                  progress={trace?.progress?.step === s.id ? trace.progress : null}
                />
              ))}
            </ol>
          )}
          {claims.length > 0 && (
            <>
              <div className="work__sub">Claims</div>
              <ClaimTree claims={claims} />
            </>
          )}
        </div>
      )}
    </section>
  )
}

// ------------------------------------------------------------------- basis

/** One line on what an answer rests on, so its confidence is visible at a glance. */
export function AnswerBasis({ claims }) {
  if (!claims?.length) return null
  const docs = claims.filter((c) => c.state === 'supported' || c.state === 'refuted').length
  const you = claims.filter(
    (c) => (c.state === 'answered' && c.answer_source === 'user') || c.origin === 'stated',
  ).length
  const profile = claims.filter((c) => c.state === 'answered' && c.answer_source === 'profile').length
  const gaps = claims.filter(
    (c) => ['exhausted', 'unknown', 'blocked'].includes(c.state) && c.origin !== 'stated',
  ).length
  const parts = [
    docs && { tone: 'ok', icon: 'book', text: `${docs} from documents` },
    you && { tone: 'you', icon: 'chat', text: `${you} from you` },
    profile && { tone: 'you', icon: 'cpu', text: `${profile} from your profile` },
    gaps && { tone: 'warn', icon: 'alert', text: `${gaps} not in the documents` },
  ].filter(Boolean)
  if (!parts.length) return null
  return (
    <div className="basis" aria-label="What this answer rests on">
      {parts.map((p) => (
        <span key={p.text} className={`basis__chip basis__chip--${p.tone}`}>
          <Icon name={p.icon} size={12} />
          {p.text}
        </span>
      ))}
    </div>
  )
}

// --------------------------------------------------------------- questions

/** The agent's questions, answered in place. */
export function QuestionCard({ questions, disabled, onSubmit }) {
  const [values, setValues] = useState({})
  const set = (id, value) => setValues((v) => ({ ...v, [id]: value }))
  const answered = questions.filter((q) => (values[q.claim_id] || '').trim()).length

  function submit(e) {
    e.preventDefault()
    if (disabled) return
    // A blank answer is sent as "I don't know": the server treats both alike.
    onSubmit(
      Object.fromEntries(questions.map((q) => [q.claim_id, (values[q.claim_id] || '').trim()])),
    )
  }

  return (
    <form className="qcard" onSubmit={submit}>
      <div className="qcard__head">
        <Icon name="sparkle" size={14} />
        <span>The documents don't settle this, so I need to ask</span>
      </div>

      {questions.map((q, i) => {
        const value = values[q.claim_id] || ''
        return (
          <div key={q.claim_id} className="qcard__q">
            <label className="qcard__text" htmlFor={`q-${q.claim_id}`}>
              <span className="qcard__n">{i + 1}</span>
              {q.text}
            </label>
            {q.why && <p className="qcard__why">{q.why}</p>}

            {q.related?.length > 0 && (
              <blockquote className="qcard__related">
                <span className="qcard__related-label">Closest I found</span>
                <span>“{q.related[0].snippet}”</span>
                <cite>{q.related[0].citation}</cite>
              </blockquote>
            )}

            {q.type === 'choice' && q.options?.length > 0 ? (
              <div className="qcard__options" role="radiogroup">
                {[...q.options, DONT_KNOW].map((opt) => (
                  <button
                    type="button"
                    key={opt}
                    role="radio"
                    aria-checked={value === opt}
                    className={`chip ${value === opt ? 'chip--on' : ''}`}
                    onClick={() => set(q.claim_id, opt)}
                    disabled={disabled}
                  >
                    {opt}
                  </button>
                ))}
              </div>
            ) : (
              <div className="qcard__field">
                <textarea
                  id={`q-${q.claim_id}`}
                  rows={q.kind === 'profile_fact' ? 1 : 3}
                  value={value}
                  onChange={(e) => set(q.claim_id, e.target.value)}
                  onKeyDown={(e) => {
                    // Ctrl/Cmd+Enter sends, as in most review forms.
                    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) submit(e)
                  }}
                  placeholder={q.kind === 'profile_fact' ? 'A word or two' : 'Type it out'}
                  disabled={disabled}
                />
                <button
                  type="button"
                  className={`chip ${value === DONT_KNOW ? 'chip--on' : ''}`}
                  onClick={() => set(q.claim_id, DONT_KNOW)}
                  disabled={disabled}
                >
                  {DONT_KNOW}
                </button>
              </div>
            )}

            {q.kind !== 'profile_fact' && (
              <p className="qcard__hint">
                Or add a document that covers it in Knowledge, then ask again.
              </p>
            )}
          </div>
        )
      })}

      <div className="qcard__foot">
        <span className="qcard__count">
          {answered} of {questions.length} answered
        </span>
        <button type="submit" className="btn btn--primary" disabled={disabled}>
          <Icon name="send" size={15} strokeWidth={2.2} />
          Send answers
        </button>
      </div>
    </form>
  )
}

// --------------------------------------------------------------- proposals

function ProposalRow({ item, onSave, onDiscard }) {
  const [draft, setDraft] = useState({
    value: item.value || '',
    title: item.title || '',
    note: item.note || '',
  })
  const isProfile = item.destination === 'profile'
  const settled = item.status !== 'pending'

  return (
    <li className={`proposal proposal--${item.status}`}>
      <div className="proposal__where">
        <Icon name={isProfile ? 'cpu' : 'book'} size={13} />
        {isProfile ? (
          <span>
            Your profile · <code>{item.key}</code>
          </span>
        ) : (
          <span>Knowledge base · shared note, marked as stated by you</span>
        )}
      </div>

      {item.warning && (
        <div className="proposal__warning" role="note">
          <Icon name="alert" size={13} />
          <span>{item.warning}</span>
        </div>
      )}

      {isProfile ? (
        <input
          className="proposal__input"
          value={draft.value}
          onChange={(e) => setDraft({ ...draft, value: e.target.value })}
          disabled={settled}
          aria-label={`Value for ${item.key}`}
        />
      ) : (
        <>
          <input
            className="proposal__input proposal__title"
            value={draft.title}
            onChange={(e) => setDraft({ ...draft, title: e.target.value })}
            disabled={settled}
            aria-label="Note title"
          />
          <textarea
            className="proposal__input"
            rows={3}
            value={draft.note}
            onChange={(e) => setDraft({ ...draft, note: e.target.value })}
            disabled={settled}
            aria-label="Note text"
          />
        </>
      )}

      <div className="proposal__actions">
        {item.status === 'pending' ? (
          <>
            <button
              className="btn btn--primary btn--sm"
              onClick={() =>
                onSave(item.id, isProfile ? { value: draft.value } : { title: draft.title, note: draft.note })
              }
            >
              <Icon name="check" size={14} strokeWidth={2.4} />
              Save
            </button>
            <button className="actionbtn" onClick={() => onDiscard(item.id)}>
              Discard
            </button>
          </>
        ) : (
          <span className={`proposal__status proposal__status--${item.status}`}>
            {item.status === 'saving' && <span className="spinner" />}
            {item.status === 'saving'
              ? 'Saving…'
              : item.status === 'saved'
                ? isProfile
                  ? 'Saved to your profile'
                  : 'Saved, and searchable from now on'
                : 'Discarded'}
          </span>
        )}
      </div>
    </li>
  )
}

/** What the agent learned from you, held for approval before anything is written. */
export function ProposalCard({ proposals, onSave, onDiscard }) {
  const pending = proposals.filter((p) => p.status === 'pending').length
  return (
    <section className="pcard">
      <div className="qcard__head">
        <Icon name="upload" size={14} />
        <span>{pending ? 'Keep what you told me?' : 'What you told me'}</span>
      </div>
      <ul className="pcard__list">
        {proposals.map((p) => (
          <ProposalRow key={p.id} item={p} onSave={onSave} onDiscard={onDiscard} />
        ))}
      </ul>
      {pending > 0 && (
        <p className="qcard__hint">
          Nothing is stored until you save it. Next time, I'll use these instead of asking.
        </p>
      )}
    </section>
  )
}
