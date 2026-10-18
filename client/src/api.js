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

/**
 * Stream a reply, invoking onEvent for each server event.
 *
 * Uses fetch + a stream reader rather than EventSource, because EventSource
 * cannot issue a POST and the request needs a JSON body.
 */
export async function streamChat({ message, sessionId, signal, onEvent }) {
  const res = await fetch('/api/chat/stream', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ message, session_id: sessionId }),
    signal,
  })
  if (!res.ok || !res.body) {
    throw new Error(`Server returned ${res.status}`)
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
