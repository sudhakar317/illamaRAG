import { useState } from 'react'

function formatWhen(iso) {
  if (!iso) return ''
  // SQLite's datetime('now') is UTC without a timezone marker, so say so
  // explicitly -- otherwise the browser reads it as local time and every
  // timestamp is off by the UTC offset.
  const date = new Date(iso.replace(' ', 'T') + 'Z')
  if (Number.isNaN(date.getTime())) return ''

  const minutes = Math.floor((Date.now() - date.getTime()) / 60000)
  if (minutes < 1) return 'just now'
  if (minutes < 60) return `${minutes}m ago`
  if (minutes < 1440) return `${Math.floor(minutes / 60)}h ago`
  return date.toLocaleDateString()
}

export default function SessionSidebar({
  sessions,
  activeId,
  onSelect,
  onNew,
  onRename,
  onDelete,
  disabled,
}) {
  const [editingId, setEditingId] = useState(null)
  const [draft, setDraft] = useState('')

  function startRename(session) {
    setEditingId(session.id)
    setDraft(session.title)
  }

  function commitRename(id) {
    const title = draft.trim()
    if (title) onRename(id, title)
    setEditingId(null)
  }

  return (
    <aside className="sidebar">
      <div className="sidebar__head">
        <h1 className="sidebar__brand">illamaRAG</h1>
        <button className="btn btn--new" onClick={onNew} disabled={disabled}>
          + New chat
        </button>
      </div>

      <div className="sidebar__list">
        {sessions.length === 0 && (
          <p className="sidebar__empty">No chats yet. Start one above.</p>
        )}

        {sessions.map((s) => (
          <div
            key={s.id}
            className={`session ${s.id === activeId ? 'session--active' : ''}`}
            onClick={() => editingId !== s.id && onSelect(s.id)}
          >
            {editingId === s.id ? (
              <input
                className="session__edit"
                value={draft}
                autoFocus
                onChange={(e) => setDraft(e.target.value)}
                onBlur={() => commitRename(s.id)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') commitRename(s.id)
                  if (e.key === 'Escape') setEditingId(null)
                }}
                onClick={(e) => e.stopPropagation()}
              />
            ) : (
              <>
                <div className="session__title">{s.title}</div>
                <div className="session__meta">
                  {s.message_count} msg · {formatWhen(s.updated_at)}
                </div>
                <div className="session__actions">
                  <button
                    title="Rename"
                    onClick={(e) => {
                      e.stopPropagation()
                      startRename(s)
                    }}
                  >
                    ✎
                  </button>
                  <button
                    title="Delete"
                    onClick={(e) => {
                      e.stopPropagation()
                      onDelete(s.id)
                    }}
                  >
                    🗑
                  </button>
                </div>
              </>
            )}
          </div>
        ))}
      </div>
    </aside>
  )
}
