import { useState } from 'react'
import HealthCard from './HealthBadge'
import Icon from './Icon'

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
  view,
  onViewChange,
  sessions,
  activeId,
  onSelect,
  onNew,
  onRename,
  onDelete,
  disabled,
  isOpen,
  onClose,
  health,
  healthError,
  documentCount,
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
    <aside className={`sidebar glass ${isOpen ? 'sidebar--open' : ''}`}>
      <div className="sidebar__head">
        <div className="brand">
          <span className="brand__mark">
            <Icon name="sparkle" size={16} strokeWidth={2} />
          </span>
          <span className="brand__name">illamaRAG</span>
        </div>
        <button
          className="iconbtn sidebar__close"
          onClick={onClose}
          aria-label="Close menu"
        >
          <Icon name="x" />
        </button>
      </div>

      <button className="btn btn--primary btn--block" onClick={onNew} disabled={disabled}>
        <Icon name="plus" size={16} strokeWidth={2.2} /> New chat
      </button>

      <nav className="nav" aria-label="Sections">
        <button
          className={`nav__item ${view === 'chat' ? 'nav__item--active' : ''}`}
          onClick={() => onViewChange('chat')}
          aria-current={view === 'chat' ? 'page' : undefined}
        >
          <Icon name="chat" size={17} /> Chats
          <span className="nav__count">{sessions.length}</span>
        </button>
        <button
          className={`nav__item ${view === 'knowledge' ? 'nav__item--active' : ''}`}
          onClick={() => onViewChange('knowledge')}
          aria-current={view === 'knowledge' ? 'page' : undefined}
        >
          <Icon name="book" size={17} /> Knowledge
          <span className="nav__count">{documentCount}</span>
        </button>
      </nav>

      <div className="sidebar__section">Recent</div>

      <div className="sidebar__list">
        {sessions.length === 0 && (
          <p className="sidebar__empty">No chats yet. Start one above.</p>
        )}

        {sessions.map((s) => (
          <div
            key={s.id}
            className={`session ${
              s.id === activeId && view === 'chat' ? 'session--active' : ''
            }`}
            onClick={() => {
              if (editingId === s.id) return
              onSelect(s.id)
            }}
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
                    aria-label={`Rename ${s.title}`}
                    onClick={(e) => {
                      e.stopPropagation()
                      startRename(s)
                    }}
                  >
                    <Icon name="edit" size={14} />
                  </button>
                  <button
                    title="Delete"
                    aria-label={`Delete ${s.title}`}
                    onClick={(e) => {
                      e.stopPropagation()
                      onDelete(s.id)
                    }}
                  >
                    <Icon name="trash" size={14} />
                  </button>
                </div>
              </>
            )}
          </div>
        ))}
      </div>

      <div className="sidebar__foot">
        <HealthCard health={health} error={healthError} />
      </div>
    </aside>
  )
}
