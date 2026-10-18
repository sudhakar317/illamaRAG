import { useEffect, useState } from 'react'
import { getHealth } from '../api'

/**
 * Model and memory status. Memory is shown because this project runs close to
 * the machine's RAM limit, so "how much is left" is genuinely useful.
 */
export default function HealthBadge({ refreshKey }) {
  const [health, setHealth] = useState(null)
  const [error, setError] = useState(null)

  useEffect(() => {
    let cancelled = false
    getHealth()
      .then((h) => !cancelled && (setHealth(h), setError(null)))
      .catch((e) => !cancelled && setError(e.message))
    return () => {
      cancelled = true
    }
  }, [refreshKey])

  if (error) {
    return (
      <div className="health health--bad" title={error}>
        <span className="health__dot" />
        server offline
      </div>
    )
  }
  if (!health) {
    return (
      <div className="health">
        <span className="health__dot health__dot--idle" />
        checking…
      </div>
    )
  }

  const { model, memory } = health
  const ready = model.exists
  const free = memory?.system?.available_gb

  return (
    <div className={`health ${ready ? '' : 'health--bad'}`}>
      <span className={`health__dot ${model.loaded ? '' : 'health__dot--idle'}`} />
      <span className="health__model" title={model.path}>
        {model.name.replace('.gguf', '')}
      </span>
      <span className="health__meta">
        {model.loaded ? 'loaded' : 'idle'} · {model.n_ctx} ctx
        {free != null && ` · ${free} GB free`}
      </span>
    </div>
  )
}
