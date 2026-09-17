import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import ForceGraph2D from 'react-force-graph-2d'
import Minimap from './Minimap.jsx'
import AgentPanel from './AgentPanel.jsx'
import DocsPanel from './DocsPanel.jsx'
import { parseRepo, fetchGraph, explainNode, askQuestion } from './api.js'

const TYPE_COLORS = {
  module: '#58a6ff',
  class: '#bc8cff',
  function: '#3fb950',
  method: '#f0883e',
}

export default function App() {
  const fgRef = useRef()
  const [graphData, setGraphData] = useState(null)
  const [meta, setMeta] = useState(null)
  const [repoInput, setRepoInput] = useState('')
  const [busy, setBusy] = useState('')
  const [error, setError] = useState(null)
  const [selected, setSelected] = useState(null)
  const [explanation, setExplanation] = useState(null)
  const [filterTypes, setFilterTypes] = useState({ module: true, class: true, function: true, method: true })
  const [highlight, setHighlight] = useState(() => new Set())
  const [query, setQuery] = useState('')
  const [focusId, setFocusId] = useState(null)
  const [chat, setChat] = useState({ messages: [], input: '', pending: false })
  const dgRef = useRef(null)
  const fittedRef = useRef(false)

  // stable identity — a fresh arrow here would re-trigger AgentPanel's
  // onHighlight effect every render and loop React into "max update depth"
  const handleHighlight = useCallback((ids) => setHighlight(new Set(ids || [])), [])

  const fitView = useCallback(() => {
    const fg = fgRef.current
    if (!fg || !graphData || graphData.nodes.length === 0) return
    let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity
    for (const n of graphData.nodes) {
      if (n.x == null) continue
      if (n.x < minX) minX = n.x
      if (n.x > maxX) maxX = n.x
      if (n.y < minY) minY = n.y
      if (n.y > maxY) maxY = n.y
    }
    if (!isFinite(minX)) return
    const w = window.innerWidth - 380
    const h = window.innerHeight
    const k = Math.min(w / Math.max(maxX - minX, 1), h / Math.max(maxY - minY, 1)) * 0.9
    fg.centerAt((minX + maxX) / 2, (minY + maxY) / 2, 400)
    fg.zoom(Math.min(k, 4), 400)
  }, [graphData])

  // Load existing graph on startup if the backend already has one
  useEffect(() => {
    console.log('[CG] startup effect ran')
    fetchGraph()
      .then((g) => { console.log('[CG] fetchGraph resolved, nodes=', g.nodes.length); hydrate(g) })
      .catch((e) => console.error('[CodeGraph] graph load failed:', e))
  }, [])

  const hydrate = (g) => {
    console.log('[CG] hydrating')
    const nodes = g.nodes.map((n) => ({ ...n }))
    const links = g.edges.map((e, i) => ({ ...e, id: `e${i}`, source: e.from, target: e.to }))
    dgRef.current = g
    fittedRef.current = false
    setGraphData({ nodes, links })
  }

  const doParse = async () => {
    if (!repoInput.trim()) return
    setBusy('Parsing repo…')
    setError(null)
    try {
      await parseRepo(repoInput.trim())
      const g = await fetchGraph()
      hydrate(g)
      const res = await fetch('/api/meta').then((r) => r.json())
      setMeta(res)
      setSelected(null)
      setExplanation(null)
      setChat({ messages: [], input: '', pending: false })
    } catch (e) {
      setError(e.message)
    } finally {
      setBusy('')
    }
  }

  const visibleData = useMemo(() => {
    if (!graphData) return null
    const nodes = graphData.nodes.filter((n) => filterTypes[n.type])
    const ids = new Set(nodes.map((n) => n.id))
    const links = graphData.links.filter((l) => ids.has(l.source.id || l.source) && ids.has(l.target.id || l.target))
    return { nodes, links }
  }, [graphData, filterTypes])

  const matches = useMemo(() => {
    if (!query.trim() || !graphData) return []
    const q = query.trim().toLowerCase()
    return graphData.nodes.filter((n) =>
      n.name?.toLowerCase().includes(q) ||
      n.qualname?.toLowerCase().includes(q) ||
      n.file?.toLowerCase().includes(q)
    ).slice(0, 8)
  }, [graphData, query])

  const focusNode = (node) => {
    const fg = fgRef.current
    if (!fg || node.x === undefined) return
    const zoomK = fg.zoom() || 1
    // canvas sits 380px right of the viewport edge — recenter into visible area
    fg.centerAt(node.x + 190 / zoomK, node.y, 600)
    fg.zoom(2.5, 600)
  }

  const handleNodeClick = useCallback(async (node) => {
    setSelected(node)
    setExplanation({ loading: true })
    try {
      const res = await explainNode(node.id)
      setExplanation(res)
    } catch (e) {
      setExplanation({ error: e.message })
    }
  }, [])

  const ask = async () => {
    const q = chat.input.trim()
    if (!q || chat.pending) return
    setChat((c) => ({ ...c, pending: true, input: '', messages: [...c.messages, { role: 'user', text: q }] }))
    try {
      const res = await askQuestion(q)
      const cited = new Set(res.cited_nodes?.length ? res.cited_nodes : res.used_nodes)
      setHighlight(cited)
      const firstId = [...cited][0]
      const fn = graphData?.nodes.find((x) => x.id === firstId)
      if (fn) setTimeout(() => focusNode(fn), 100)
      setChat((c) => ({ ...c, messages: [...c.messages, { role: 'assistant', text: res.answer, cited: [...cited] }] }))
    } catch (e) {
      setChat((c) => ({ ...c, messages: [...c.messages, { role: 'assistant', text: `⚠️ ${e.message}` }] }))
    } finally {
      setChat((c) => ({ ...c, pending: false }))
    }
  }

  const nodeColor = (n, isMatch, cited) => {
    if (cited) return '#ffffff'
    if (isMatch) return '#d29922'
    return TYPE_COLORS[n.type] || '#8b949e'
  }

  const paintNode = useCallback((node, ctx, globalScale) => {
    const q = query.trim().toLowerCase()
    const searching = q.length > 0
    const cited = highlight.has(node.id)
    const isMatch = searching && (
      node.name?.toLowerCase().includes(q) ||
      node.qualname?.toLowerCase().includes(q) ||
      node.file?.toLowerCase().includes(q)
    )
    const dimmed = searching && !isMatch && !cited
    const showLabel = searching ? (isMatch || cited) : (cited || globalScale >= 1.6)

    const label = node.name || node.id
    const fontSize = 12 / globalScale
    ctx.font = `${cited ? 'bold ' : ''}${fontSize}px sans-serif`
    ctx.globalAlpha = dimmed ? 0.12 : 1
    ctx.fillStyle = nodeColor(node, isMatch, cited)
    ctx.beginPath()
    ctx.arc(node.x, node.y, r, 0, 2 * Math.PI)
    ctx.fill()
    if (cited || isMatch) {
      ctx.strokeStyle = cited ? '#ffffff' : '#d29922'
      ctx.lineWidth = (cited ? 2 : 1.5) / globalScale
      ctx.stroke()
    }
    if (showLabel) {
      ctx.fillStyle = 'rgba(230,237,243,0.9)'
      ctx.textAlign = 'center'
      ctx.fillText(label, node.x, node.y + r + fontSize + 1)
    }
    ctx.globalAlpha = 1
  }, [highlight, query])

  const r = 4

  return (
    <div style={{ display: 'flex', height: '100vh', overflow: 'hidden' }}>
      {/* Left: chat panel */}
      <div style={{ width: 380, minWidth: 380, display: 'flex', flexDirection: 'column', borderRight: '1px solid #21262d', background: '#0d1117' }}>
        <header style={{ padding: '14px 16px', borderBottom: '1px solid #21262d' }}>
          <h1 style={{ margin: 0, fontSize: 18 }}>🕸️ CodeGraph</h1>
          <div style={{ fontSize: 12, color: '#8b949e', marginTop: 4 }}>
            {meta?.repo ? `repo: ${meta.repo}` : 'no repo loaded'}
          </div>
        </header>

        <div style={{ padding: '12px 16px', borderBottom: '1px solid #21262d' }}>
          <div style={{ display: 'flex', gap: 8 }}>
            <input
              value={repoInput}
              onChange={(e) => setRepoInput(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && doParse()}
              placeholder="/absolute/path/to/repo"
              style={{ flex: 1, background: '#161b22', border: '1px solid #30363d', borderRadius: 6, color: '#e6edf3', padding: '8px 10px', fontSize: 13 }}
            />
            <button onClick={doParse} disabled={!!busy} style={btnPrimary}>{busy || 'Parse'}</button>
          </div>
          {error && <div style={{ color: '#f85149', fontSize: 12, marginTop: 6 }}>{error}</div>}
          <div style={{ display: 'flex', gap: 10, marginTop: 8, fontSize: 12 }}>
            {Object.keys(filterTypes).map((t) => (
              <label key={t} style={{ display: 'flex', alignItems: 'center', gap: 4, cursor: 'pointer', color: filterTypes[t] ? '#e6edf3' : '#6e7681' }}>
                <input type="checkbox" checked={filterTypes[t]} onChange={() => setFilterTypes((f) => ({ ...f, [t]: !f[t] }))} />
                <span style={{ color: TYPE_COLORS[t] }}>●</span> {t}
              </label>
            ))}
          </div>
        </div>

        <AgentPanel
          repo={meta?.repo || null}
          onHighlight={handleHighlight}
        />

        <DocsPanel />

        <div style={{ flex: 1, overflowY: 'auto', padding: '12px 16px', display: 'flex', flexDirection: 'column', gap: 10 }}>
          {chat.messages.length === 0 && (
            <div style={{ color: '#6e7681', fontSize: 13, lineHeight: 1.5 }}>
              Ask a question about the codebase — answers are grounded in the dependency graph, and cited nodes light up on the right.
            </div>
          )}
          {chat.messages.map((m, i) => (
            <div key={i} style={{
              alignSelf: m.role === 'user' ? 'flex-end' : 'flex-start',
              maxWidth: '92%',
              background: m.role === 'user' ? '#1f6feb33' : '#161b22',
              border: '1px solid ' + (m.role === 'user' ? '#1f6feb66' : '#21262d'),
              borderRadius: 8,
              padding: '8px 12px',
              fontSize: 13,
              whiteSpace: 'pre-wrap',
              lineHeight: 1.45,
            }}>
              {m.text}
              {m.cited?.length > 0 && (
                <div style={{ marginTop: 6, display: 'flex', flexWrap: 'wrap', gap: 4 }}>
                  {m.cited.map((c) => (
                    <span key={c} style={{ fontSize: 10, background: '#21262d', borderRadius: 4, padding: '2px 6px', color: '#58a6ff', cursor: 'pointer' }}
                      onClick={() => { const n = graphData?.nodes.find((x) => x.id === c); if (n) { handleNodeClick(n); focusNode(n) } }}>
                      {c}
                    </span>
                  ))}
                </div>
              )}
            </div>
          ))}
          {chat.pending && <div style={{ color: '#6e7681', fontSize: 13 }}>thinking…</div>}
        </div>

        <div style={{ padding: '12px 16px', borderTop: '1px solid #21262d', display: 'flex', gap: 8 }}>
          <input
            value={chat.input}
            onChange={(e) => setChat((c) => ({ ...c, input: e.target.value }))}
            onKeyDown={(e) => e.key === 'Enter' && ask()}
            placeholder="how does auth work here?"
            style={{ flex: 1, background: '#161b22', border: '1px solid #30363d', borderRadius: 6, color: '#e6edf3', padding: '8px 10px', fontSize: 13 }}
          />
          <button onClick={ask} disabled={chat.pending} style={btnPrimary}>Ask</button>
        </div>
      </div>

      {/* Right: graph + search overlay + side panel */}
      <div style={{ flex: 1, position: 'relative' }}>
        {graphData && (
          <div style={{ position: 'absolute', top: 16, left: 16, zIndex: 10, width: 300 }}>
            <input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              onKeyDown={(e) => e.key === 'Escape' && setQuery('')}
              placeholder="🔍 Search nodes…"
              style={{ width: '100%', boxSizing: 'border-box', background: 'rgba(22,27,34,0.92)', border: '1px solid #30363d', borderRadius: 6, color: '#e6edf3', padding: '8px 10px', fontSize: 13, outline: 'none' }}
            />
            {query.trim() && (
              <div style={{ marginTop: 6, background: 'rgba(22,27,34,0.95)', border: '1px solid #30363d', borderRadius: 8, overflow: 'hidden' }}>
                {matches.length === 0 && <div style={{ padding: '8px 12px', fontSize: 12, color: '#6e7681' }}>no matches</div>}
                {matches.map((n) => (
                  <button key={n.id}
                    onClick={() => { handleNodeClick(n); focusNode(n) }}
                    style={{ display: 'flex', alignItems: 'center', gap: 8, width: '100%', textAlign: 'left', background: 'transparent', border: 'none', borderBottom: '1px solid #21262d', padding: '7px 12px', cursor: 'pointer' }}>
                    <span style={{ color: TYPE_COLORS[n.type], fontSize: 10 }}>●</span>
                    <span style={{ fontSize: 12, color: '#e6edf3', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{n.id}</span>
                  </button>
                ))}
              </div>
            )}
          </div>
        )}
        {visibleData ? (
          <ForceGraph2D
            ref={fgRef}
            graphData={visibleData}
            backgroundColor="#0d1117"
            nodeCanvasObject={paintNode}
            nodeRelSize={4}
            linkWidth={(l) => (highlight.has(l.source.id || l.source) && highlight.has(l.target.id || l.target)) ? 2 : 0.5}
            linkColor={(l) => (highlight.has(l.source.id || l.source) && highlight.has(l.target.id || l.target)) ? 'rgba(88,166,255,0.9)' : 'rgba(139,148,158,0.25)'}
            onNodeClick={handleNodeClick}
            cooldownTicks={100}
            onEngineStop={() => { if (!fittedRef.current) { fittedRef.current = true; fitView() } }}
            width={window.innerWidth - 380}
            height={window.innerHeight}
          />
        ) : (
          <div style={{ display: 'flex', height: '100%', alignItems: 'center', justifyContent: 'center', flexDirection: 'column', color: '#6e7681' }}>
            <div style={{ fontSize: 42 }}>🕸️</div>
            <p>Parse a repo on the left to build its dependency graph.</p>
            <p style={{ fontSize: 12 }}>Try: <code>./sample-repo/shop</code> (relative to project root)</p>
          </div>
        )}

        {graphData && (
          <div style={{ position: 'absolute', bottom: 16, left: 16, zIndex: 10 }}>
            <Minimap
              data={visibleData}
              highlight={highlight}
              query={query}
              fgRef={fgRef}
              graphW={window.innerWidth - 380}
              graphH={window.innerHeight}
            />
            <div style={{ display: 'flex', gap: 6, marginTop: 6, justifyContent: 'center' }}>
              {[
                { label: '+', act: () => fgRef.current?.zoom(Math.min((fgRef.current?.zoom() || 1) * 1.4, 12), 200) },
                { label: '−', act: () => fgRef.current?.zoom(Math.max((fgRef.current?.zoom() || 1) / 1.4, 0.05), 200) },
                { label: '⤢ fit', act: () => { fittedRef.current = true; fitView() } },
              ].map((b) => (
                <button key={b.label} onClick={b.act} style={{ width: 44, background: 'rgba(22,27,34,0.92)', border: '1px solid #30363d', borderRadius: 6, color: '#e6edf3', fontSize: 13, padding: '4px 0', cursor: 'pointer' }}>{b.label}</button>
              ))}
            </div>
          </div>
        )}

        {selected && (
          <div style={panelStyle}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
              <strong style={{ fontSize: 14, marginRight: 'auto' }}>{selected.id}</strong>
              <button onClick={() => focusNode(selected)} title="Center & zoom" style={btnGhost}>⌖</button>
              <button onClick={() => { setSelected(null); setExplanation(null); setHighlight(new Set()); setQuery('') }} style={btnGhost}>×</button>
            </div>
            <div style={{ fontSize: 11, color: '#8b949e', margin: '6px 0 10px' }}>
              {selected.type} · {selected.file}:{selected.line_start}–{selected.line_end}
            </div>
            {explanation?.loading && <div style={{ color: '#6e7681', fontSize: 13 }}>explaining…</div>}
            {explanation?.error && <div style={{ color: '#f85149', fontSize: 13 }}>{explanation.error}</div>}
            {explanation?.explanation && (
              <div style={{ fontSize: 13, whiteSpace: 'pre-wrap', lineHeight: 1.5 }}>{explanation.explanation}</div>
            )}
          </div>
        )}
      </div>
    </div>
  )
}

const btnPrimary = {
  background: '#238636',
  color: '#fff',
  border: 'none',
  borderRadius: 6,
  padding: '8px 14px',
  fontSize: 13,
  cursor: 'pointer',
  fontWeight: 600,
}

const btnGhost = {
  background: 'transparent',
  color: '#8b949e',
  border: 'none',
  fontSize: 16,
  cursor: 'pointer',
  lineHeight: 1,
}

const panelStyle = {
  position: 'absolute',
  top: 16,
  right: 16,
  width: 360,
  maxHeight: 'calc(100vh - 32px)',
  overflowY: 'auto',
  background: '#161b22',
  border: '1px solid #30363d',
  borderRadius: 10,
  padding: 14,
  boxShadow: '0 8px 30px rgba(0,0,0,0.5)',
}
