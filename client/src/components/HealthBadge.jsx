import Icon from './Icon'

/**
 * Model and memory status. Memory is shown because this project runs close to
 * the machine's RAM limit, so "how much is left" is genuinely useful.
 *
 * The health payload is fetched once in App and shared, so the sidebar card and
 * the header dot can never disagree.
 */

function describe(health, error) {
  if (error) return { tone: 'bad', label: 'Offline', detail: error }
  if (!health) return { tone: 'idle', label: 'Connecting', detail: '' }
  if (!health.model.exists) {
    return { tone: 'bad', label: 'Model missing', detail: health.model.path }
  }
  return health.model.loaded
    ? { tone: 'ok', label: 'Loaded', detail: 'Model is loaded in memory' }
    : { tone: 'idle', label: 'Ready', detail: 'Model loads on the first message' }
}

/** Compact indicator for the header. */
export function StatusDot({ health, error }) {
  const { tone, label, detail } = describe(health, error)
  return (
    <div
      className={`statusdot statusdot--${tone}`}
      title={detail ? `${label} · ${detail}` : label}
      role="status"
      aria-label={label}
    >
      <span className="statusdot__dot" />
    </div>
  )
}

/** Fuller card for the sidebar footer. */
export default function HealthCard({ health, error }) {
  const { tone, label, detail } = describe(health, error)
  const model = health?.model
  const free = health?.memory?.system?.available_gb

  return (
    <div className={`healthcard healthcard--${tone}`} title={detail || undefined}>
      <div className="healthcard__icon">
        <Icon name="cpu" size={17} />
      </div>
      <div className="healthcard__body">
        <div className="healthcard__name">
          {model ? model.name.replace('.gguf', '') : 'Local model'}
        </div>
        <div className="healthcard__meta" title={detail || undefined}>
          <span className="statusdot__dot" />
          {label}
        </div>
        {free != null && <div className="healthcard__stats">{free} GB free</div>}
      </div>
    </div>
  )
}
