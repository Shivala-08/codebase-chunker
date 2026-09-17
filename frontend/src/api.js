const base = '/api'

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
  console.log('[CG api] fetchGraph: starting')
  const ctrl = new AbortController()
  const timer = setTimeout(() => ctrl.abort(), 15000)
  let res
  try {
    res = await fetch(`${base}/graph`, { signal: ctrl.signal })
    console.log('[CG api] fetchGraph: got headers', res.status)
    const text = await res.text()
    console.log('[CG api] fetchGraph: got body', text.length, 'bytes')
    if (!res.ok) throw new Error('No graph available — parse a repo first.')
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
