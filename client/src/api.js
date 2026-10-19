// All requests go to the same origin; Vite proxies /api to the FastAPI server.

async function request(path, options = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  })
  if (!res.ok) {
    let detail = `Request failed (${res.status})`
    try {
      const body = await res.json()
      if (body?.detail) detail = body.detail
    } catch {
      // Response had no JSON body; the status-code message is enough.
    }
    throw new Error(detail)
  }
  return res.status === 204 ? null : res.json()
}

export const getHealth = () => request('/api/health')
export const listSessions = () => request('/api/sessions')
export const createSession = () =>
  request('/api/sessions', { method: 'POST', body: JSON.stringify({}) })
export const getMessages = (id) => request(`/api/sessions/${id}/messages`)
export const renameSession = (id, title) =>
  request(`/api/sessions/${id}`, { method: 'PATCH', body: JSON.stringify({ title }) })
export const deleteSession = (id) =>
  request(`/api/sessions/${id}`, { method: 'DELETE' })
export const clearSession = (id) =>
  request(`/api/sessions/${id}/clear`, { method: 'POST' })

// ---------------------------------------------------------------- knowledge

export const listDocuments = () => request('/api/documents')
export const deleteDocument = (id) =>
  request(`/api/documents/${id}`, { method: 'DELETE' })
export const addUrl = (url) =>
  request('/api/documents/url', { method: 'POST', body: JSON.stringify({ url }) })

/** Upload a file. fetch sets the multipart boundary itself, so no headers here. */
export async function uploadDocument(file) {
  const form = new FormData()
  form.append('file', file)
  const res = await fetch('/api/documents', { method: 'POST', body: form })
  if (!res.ok) {
    let detail = `Upload failed (${res.status})`
    try {
      const body = await res.json()
      if (body?.detail) detail = body.detail
    } catch {
      // No JSON body; the status message is enough.
    }
    throw new Error(detail)
  }
  return res.json()
}

/**
 * Read a Server-Sent Events body, invoking onEvent for each JSON frame.
 *
 * Uses fetch + a stream reader rather than EventSource, because EventSource
 * cannot issue a POST and these requests need a JSON body.
 */
async function readEvents(res, onEvent) {
  if (!res.ok || !res.body) {
    let detail = `Server returned ${res.status}`
    try {
      const body = await res.json()
      if (body?.detail) detail = body.detail
    } catch {
      // No JSON body; the status message is enough.
    }
    throw new Error(detail)
  }

  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''

  while (true) {
    const { done, value } = await reader.read()
    if (done) break

    // stream:true so a multi-byte character split across reads survives.
    buffer += decoder.decode(value, { stream: true })

    // Frames end with a blank line, and a single read can land mid-frame --
    // so keep the remainder buffered rather than parsing per chunk.
    let boundary
    while ((boundary = buffer.indexOf('\n\n')) !== -1) {
      const frame = buffer.slice(0, boundary)
      buffer = buffer.slice(boundary + 2)
      if (!frame.startsWith('data: ')) continue
      try {
        onEvent(JSON.parse(frame.slice(6)))
      } catch {
        // A malformed frame should not tear down the whole stream.
      }
    }
  }
}

/** Stream a plain chat reply. */
export async function streamChat({ message, sessionId, useRag = true, signal, onEvent }) {
  const res = await fetch('/api/chat/stream', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ message, session_id: sessionId, use_rag: useRag }),
    signal,
  })
  await readEvents(res, onEvent)
}

// -------------------------------------------------------------------- agent

/**
 * Run the agent: either a new question (message), or the answers to the
 * questions a run asked (runId + answers, keyed by claim id).
 */
export async function streamAgent({ message, sessionId, runId, answers, signal, onEvent }) {
  const res = await fetch('/api/agent/stream', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ message, session_id: sessionId, run_id: runId, answers }),
    signal,
  })
  await readEvents(res, onEvent)
}

export const getAgentState = (sessionId) =>
  request(`/api/agent/state?session_id=${encodeURIComponent(sessionId)}`)
export const saveProposal = (id, edits = {}) =>
  request(`/api/agent/proposals/${id}/save`, { method: 'POST', body: JSON.stringify(edits) })
export const discardProposal = (id) =>
  request(`/api/agent/proposals/${id}/discard`, { method: 'POST' })
