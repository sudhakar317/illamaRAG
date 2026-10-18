import { memo, useCallback, useEffect, useRef, useState } from 'react'
import * as api from '../api'
import Icon from './Icon'

const ACCEPT = '.pdf,.txt,.md,.markdown,.html,.htm'

const TYPE_ICON = { pdf: 'file', text: 'file', html: 'globe', url: 'link' }

const isWorking = (d) => d.status === 'indexing' || d.status === 'pending'

function statusBadge(doc) {
  if (doc.status === 'indexing') {
    return {
      tone: 'busy',
      label: doc.chunk_count ? `Indexing · ${doc.chunk_count}` : 'Indexing',
    }
  }
  if (doc.status === 'pending') return { tone: 'busy', label: 'Queued' }
  if (doc.status === 'failed') return { tone: 'bad', label: 'Failed' }
  return { tone: 'ok', label: 'Ready' }
}

/** Insert or replace one document, keeping newest first like the server. */
function upsert(list, doc) {
  if (!list) return [doc]
  const i = list.findIndex((d) => d.id === doc.id)
  if (i === -1) return [doc, ...list]
  const next = list.slice()
  next[i] = doc
  return next
}

/**
 * Live document list. The server pushes a snapshot on connect and then one
 * event per change, so nothing is re-fetched on a timer: while a document
 * indexes, only that document's row is replaced, and only its card re-renders.
 * EventSource reconnects by itself, and each reconnect starts with a fresh
 * snapshot, so a dropped connection cannot leave the list stale.
 */
function useDocuments() {
  const [documents, setDocuments] = useState(null)
  const [connected, setConnected] = useState(true)

  useEffect(() => {
    const source = new EventSource('/api/documents/events')
    source.onopen = () => setConnected(true)
    source.onerror = () => setConnected(false)
    source.onmessage = (e) => {
      let event
      try {
        event = JSON.parse(e.data)
      } catch {
        return
      }
      if (event.type === 'snapshot') setDocuments(event.documents)
      else if (event.type === 'document') setDocuments((list) => upsert(list, event.document))
      else if (event.type === 'deleted') {
        setDocuments((list) => list && list.filter((d) => d.id !== event.id))
      }
    }
    return () => source.close()
  }, [])

  return { documents, setDocuments, connected }
}

/** One document card. Memoised: unchanged documents keep their object
 *  identity across updates, so only the card that changed re-renders. */
const DocCard = memo(function DocCard({ doc, onDelete }) {
  const badge = statusBadge(doc)
  return (
    <div className={`doc card doc--${doc.status}`}>
      <span className={`doc__icon doc__icon--${doc.source_type}`}>
        <Icon name={TYPE_ICON[doc.source_type] || 'file'} size={18} />
      </span>
      <div className="doc__body">
        <div className="doc__title" title={doc.source || doc.title}>
          {doc.title}
        </div>
        <div className="doc__meta">
          <span className="doc__type">{doc.source_type}</span>
          {doc.status === 'indexed' && (
            <span>
              {doc.chunk_count} chunk{doc.chunk_count === 1 ? '' : 's'}
            </span>
          )}
        </div>
        {doc.status === 'failed' && doc.error && (
          <div className="doc__error">{doc.error}</div>
        )}
      </div>
      <span className={`badge badge--${badge.tone}`}>
        {badge.tone === 'busy' && <span className="spinner" />}
        {badge.label}
      </span>
      <button
        className="iconbtn iconbtn--sm doc__delete"
        onClick={() => onDelete(doc.id)}
        aria-label={`Delete ${doc.title}`}
        title="Remove"
      >
        <Icon name="trash" size={15} />
      </button>
      {isWorking(doc) && <span className="doc__progress" />}
    </div>
  )
})

/** The knowledge base as its own section: add sources, watch them index. */
export default function KnowledgeView({ health, onChanged }) {
  const { documents, setDocuments, connected } = useDocuments()
  const [url, setUrl] = useState('')
  const [uploading, setUploading] = useState(null) // name of the file in flight
  const [addingUrl, setAddingUrl] = useState(false)
  const [error, setError] = useState(null)
  const [dragging, setDragging] = useState(false)
  const fileRef = useRef(null)

  const working = (documents || []).some(isWorking)

  // Notify only when indexing *finishes*, so the shared health counts update
  // once rather than on every progress event.
  const wasWorking = useRef(false)
  useEffect(() => {
    if (wasWorking.current && !working) onChanged?.()
    wasWorking.current = working
  }, [working, onChanged])

  async function handleFiles(files) {
    const list = Array.from(files || [])
    if (!list.length) return
    setError(null)
    for (const file of list) {
      setUploading(file.name)
      try {
        // The stream announces it too; applying the response as well shows
        // the card immediately even if that event is still in flight.
        const doc = await api.uploadDocument(file)
        setDocuments((d) => upsert(d, doc))
      } catch (e) {
        setError(`${file.name}: ${e.message}`)
      }
    }
    setUploading(null)
    onChanged?.()
  }

  async function handleAddUrl(e) {
    e.preventDefault()
    const value = url.trim()
    if (!value) return
    setAddingUrl(true)
    setError(null)
    try {
      const doc = await api.addUrl(value)
      setDocuments((d) => upsert(d, doc))
      setUrl('')
    } catch (e) {
      setError(e.message)
    }
    setAddingUrl(false)
    onChanged?.()
  }

  // Stable, so memoised cards are not re-rendered just because this changed.
  const handleDelete = useCallback(
    async (id) => {
      try {
        await api.deleteDocument(id)
        setDocuments((list) => list && list.filter((d) => d.id !== id))
        onChanged?.()
      } catch (e) {
        setError(e.message)
      }
    },
    [setDocuments, onChanged],
  )

  const docCount = documents?.length ?? health?.knowledge?.documents ?? 0
  const chunkCount = documents
    ? documents.reduce((n, d) => n + (d.chunk_count || 0), 0)
    : health?.knowledge?.chunks ?? 0
  const embedderMissing = health?.embedder && health.embedder.exists === false

  return (
    <div className="kb">
      <div className="kb__inner">
        <section className="kb__hero">
          <h2 className="kb__title">Knowledge base</h2>
          <div className="kb__stats">
            <div className="stat">
              <span className="stat__value">{docCount}</span>
              <span className="stat__label">documents</span>
            </div>
            <div className="stat">
              <span className="stat__value">{chunkCount}</span>
              <span className="stat__label">chunks</span>
            </div>
          </div>
        </section>

        {embedderMissing && (
          <div className="notice notice--warn">
            <Icon name="alert" size={16} />
            Embedding model missing: uploads cannot be indexed.
          </div>
        )}

        <section className="kb__add">
          <div
            className={`card dropzone ${dragging ? 'dropzone--active' : ''} ${
              uploading ? 'dropzone--busy' : ''
            }`}
            role="button"
            tabIndex={0}
            onKeyDown={(e) => {
              if (e.key === 'Enter' || e.key === ' ') {
                e.preventDefault()
                fileRef.current?.click()
              }
            }}
            onDragOver={(e) => {
              e.preventDefault()
              setDragging(true)
            }}
            onDragLeave={() => setDragging(false)}
            onDrop={(e) => {
              e.preventDefault()
              setDragging(false)
              handleFiles(e.dataTransfer.files)
            }}
            onClick={() => fileRef.current?.click()}
          >
            <input
              ref={fileRef}
              type="file"
              accept={ACCEPT}
              multiple
              hidden
              onChange={(e) => {
                handleFiles(e.target.files)
                e.target.value = ''
              }}
            />
            <span className="dropzone__icon">
              <Icon name="upload" size={22} />
            </span>
            <strong>{uploading ? `Uploading ${uploading}…` : 'Upload'}</strong>
            <span className="dropzone__hint">PDF · TXT · MD · HTML</span>
          </div>

          <form className="card urlcard" onSubmit={handleAddUrl}>
            <div className="urlcard__row">
              <span className="urlcard__icon" title="Add a web page">
                <Icon name="globe" size={18} />
              </span>
              <input
                type="url"
                className="field"
                placeholder="https://example.com/article"
                value={url}
                onChange={(e) => setUrl(e.target.value)}
                disabled={addingUrl}
                aria-label="Web page address"
              />
              <button
                className="sendbtn"
                disabled={addingUrl || !url.trim()}
                aria-label="Add web page"
                title="Add"
              >
                {addingUrl ? <span className="spinner" /> : <Icon name="plus" size={18} strokeWidth={2.2} />}
              </button>
            </div>
          </form>
        </section>

        {error && (
          <div className="notice notice--bad" role="alert">
            <Icon name="alert" size={16} />
            <span className="notice__text">{error}</span>
            <button className="iconbtn iconbtn--sm" onClick={() => setError(null)} aria-label="Dismiss">
              <Icon name="x" size={14} />
            </button>
          </div>
        )}

        <section>
          <div className="kb__listhead">
            <h3>Documents</h3>
            {working && (
              <span className="pill pill--busy" title="Indexing">
                <span className="spinner" />
              </span>
            )}
            {!connected && !working && (
              <span className="pill pill--muted" title="Live updates paused, reconnecting">
                <span className="spinner" />
              </span>
            )}
          </div>

          {documents === null ? (
            <div className="docs">
              {[0, 1, 2].map((i) => (
                <div key={i} className="doc doc--skeleton" />
              ))}
            </div>
          ) : documents.length === 0 ? (
            <div className="kb__empty">
              <Icon name="layers" size={28} />
              <p>No documents yet</p>
            </div>
          ) : (
            <div className="docs">
              {documents.map((doc) => (
                <DocCard key={doc.id} doc={doc} onDelete={handleDelete} />
              ))}
            </div>
          )}
        </section>
      </div>
    </div>
  )
}
