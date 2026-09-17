import { useState } from 'react'
import { generateDocs, listDocs, fetchDocPage } from './api.js'

// Mode 4 (v2): living docs. Regenerates markdown pages (LLM narrative +
// mechanical Mermaid diagrams) from the current graph; pages open in a
// slide-over. Mermaid blocks are shown as source — pasteable into any
// Mermaid renderer or GitHub markdown.
export default function DocsPanel() {
  const [docs, setDocs] = useState(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)
  const [open, setOpen] = useState(false)
  const [page, setPage] = useState(null) // { name, content }

  const generate = async () => {
    setBusy(true)
    setError(null)
    try {
      await generateDocs()
      const list = await listDocs()
      setDocs(list)
      setOpen(true)
    } catch (e) {
      setError(e.message)
    } finally {
      setBusy(false)
    }
  }

  const openPage = async (name) => {
    try {
      const content = await fetchDocPage(name)
      setPage({ name, content })
    } catch (e) {
      setError(e.message)
    }
  }

  return (
    <div style={{ borderTop: '1px solid #21262d', padding: '10px 16px' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
        <button
          onClick={docs && !open ? () => setOpen(true) : generate}
          disabled={busy}
          style={{ ...btn, background: '#21262d' }}
        >
          {busy ? 'generating…' : docs ? '📚 Docs ready' : '📚 Generate docs'}
        </button>
        {docs && (
          <span style={{ fontSize: 11, color: '#6e7681' }}>
            {docs.pages.length} page(s){open ? '' : ' — click to browse'}
          </span>
        )}
        {open && (
          <button onClick={() => setOpen(false)} style={{ ...btn, background: 'transparent', color: '#8b949e', padding: '4px 8px' }}>
            hide
          </button>
        )}
      </div>
      {error && <div style={{ color: '#f85149', fontSize: 12, marginTop: 6 }}>{error}</div>}

      {open && docs && (
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 4, marginTop: 8 }}>
          {docs.pages.map((p) => (
            <button
              key={p}
              onClick={() => openPage(p)}
              style={{
                fontSize: 10, background: '#161b22', border: '1px solid #30363d',
                borderRadius: 10, color: '#58a6ff', padding: '3px 8px', cursor: 'pointer',
              }}
            >
              {p.replace('.md', '')}
            </button>
          ))}
        </div>
      )}

      {page && (
        <div style={{
          position: 'fixed', top: 16, left: 396, bottom: 16, width: 'min(680px, calc(100vw - 430px))',
          zIndex: 40, background: '#0d1117ee', border: '1px solid #30363d', borderRadius: 10,
          boxShadow: '0 8px 30px rgba(0,0,0,0.55)', display: 'flex', flexDirection: 'column',
          backdropFilter: 'blur(4px)',
        }}>
          <div style={{ display: 'flex', alignItems: 'center', padding: '10px 14px', borderBottom: '1px solid #21262d' }}>
            <strong style={{ fontSize: 13, marginRight: 'auto' }}>📘 {page.name}</strong>
            <button onClick={() => setPage(null)} style={ghost}>×</button>
          </div>
          <pre style={{
            margin: 0, padding: 16, overflow: 'auto', flex: 1,
            fontSize: 12.5, lineHeight: 1.55, whiteSpace: 'pre-wrap', color: '#e6edf3',
            fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace',
          }}>
            {page.content}
          </pre>
        </div>
      )}
    </div>
  )
}

const btn = {
  color: '#e6edf3',
  border: '1px solid #30363d',
  borderRadius: 6,
  padding: '6px 12px',
  fontSize: 12,
  cursor: 'pointer',
  fontWeight: 600,
}

const ghost = {
  background: 'transparent',
  color: '#8b949e',
  border: 'none',
  fontSize: 15,
  cursor: 'pointer',
  lineHeight: 1,
}
