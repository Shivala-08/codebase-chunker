// Dev (vite :5173) goes through the /api proxy; served from the backend
// itself (:8000) the routes live at the root, so no prefix.
const base = location.port === '5173' ? '/api' : ''

export async function parseRepo(repoPath) {
  const res = await fetch(`${base}/parse`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ repo_path: repoPath }),
  })
  if (!res.ok) throw new Error((await res.json()).detail || `parse failed: ${res.status}`)
  return res.json()
}

export async function fetchGraph() {
  // 15s hard stop: a hung body must surface as an error, never a silent spin
  const ctrl = new AbortController()
  const timer = setTimeout(() => ctrl.abort(), 15000)
  let res
  try {
    res = await fetch(`${base}/graph`, { signal: ctrl.signal, cache: 'no-store' })
    if (!res.ok) throw new Error('No graph available — parse a repo first.')
    const text = await res.text()
    const parsed = JSON.parse(text)
    // tolerate a backend that double-encodes the graph as a JSON string
    return typeof parsed === 'string' ? JSON.parse(parsed) : parsed
  } finally {
    clearTimeout(timer)
  }
}

export async function explainNode(nodeId) {
  const res = await fetch(`${base}/node/${encodeURIComponent(nodeId)}/explain`)
  if (!res.ok) throw new Error((await res.json()).detail || 'explain failed')
  return res.json()
}

export async function askQuestion(question) {
  const res = await fetch(`${base}/chat`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ question }),
  })
  if (!res.ok) throw new Error((await res.json()).detail || 'chat failed')
  return res.json()
}

export async function runAgentTask(repoPath, task) {
  const res = await fetch(`${base}/agent/task`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ repo_path: repoPath, task }),
  })
  if (!res.ok) throw new Error((await res.json()).detail || 'agent task failed')
  return res.json()
}

export async function generateDocs() {
  const res = await fetch(`${base}/docs/generate`, { method: 'POST' })
  if (!res.ok) throw new Error((await res.json()).detail || 'docs generation failed')
  return res.json()
}

export async function listDocs() {
  const res = await fetch(`${base}/docs`)
  if (!res.ok) throw new Error((await res.json()).detail || 'no docs yet')
  return res.json()
}

export async function fetchDocPage(name) {
  const res = await fetch(`${base}/docs/${encodeURIComponent(name)}`)
  if (!res.ok) throw new Error((await res.json()).detail || 'doc page failed')
  return res.text()
}

// --- v4 ---

export async function fetchBlastRadius(nodeId, hops = 3) {
  const res = await fetch(`${base}/node/${encodeURIComponent(nodeId)}/blast-radius?hops=${hops}`)
  if (!res.ok) throw new Error((await res.json()).detail || 'blast radius failed')
  return res.json()
}

export async function fetchHotspots(limit = 15) {
  const res = await fetch(`${base}/graph/hotspots?limit=${limit}`)
  if (!res.ok) throw new Error((await res.json()).detail || 'hotspots failed')
  return res.json()
}

export async function fetchMeta() {
  const res = await fetch(`${base}/meta`)
  if (!res.ok) throw new Error('meta failed')
  return res.json()
}

export async function exportGraph(format) {
  const res = await fetch(`${base}/graph/export?format=${encodeURIComponent(format)}`)
  if (!res.ok) throw new Error((await res.json()).detail || 'export failed')
  return res.text()
}

export function downloadGraphExport(format, text) {
  const ext = format === 'json' ? 'json' : format === 'graphml' ? 'graphml' : 'dot'
  const blob = new Blob([text], { type: format === 'json' ? 'application/json' : 'text/plain' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = `codegraph-export.${ext}`
  a.click()
  URL.revokeObjectURL(url)
}

export async function watchControl(action) {
  const res = await fetch(`${base}/watch/${action}`, { method: 'POST' })
  if (!res.ok) throw new Error((await res.json()).detail || 'watch control failed')
  return res.json()
}
