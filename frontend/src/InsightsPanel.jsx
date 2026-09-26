import { useState } from 'react'
import { fetchBlastRadius, fetchHotspots, fetchMeta, exportGraph, downloadGraphExport, watchControl } from './api.js'

// v4 insights panel: blast radius, hotspots, graph export, watch/provider
// status. All pure graph-math + config surfaces — no LLM latency here.
export default function InsightsPanel({ onFocusNode, onHighlight }) {
  const [tab, setTab] = useState(null) // null | 'blast' | 'hotspots' | 'status'
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)

  // blast radius
  const [blastInput, setBlastInput] = useState('')
  const [blastHops, setBlastHops] = useState(3)
  const [blast, setBlast] = useState(null)

  const [hotspots, setHotspots] = useState(null)
  const [meta, setMeta] = useState(null)

  const close = () => {
    setTab(null)
    setError(null)
  }

  const runBlast = async () => {
    const id = blastInput.trim()
    if (!id) return
    setBusy(true)
    setError(null)
    try {
      const res = await fetchBlastRadius(id, blastHops)
      setBlast(res)
      onHighlight?.(res.affected)
      if (onFocusNode) onFocusNode(id)
    } catch (e) {
      setError(e.message)
    } finally {
      setBusy(false)
    }
  }

  const runHotspots = async () => {
    setBusy(true)
    setError(null)
    try {
      const res = await fetchHotspots(15)
      setHotspots(res.hotspots)
    } catch (e) {
      setError(e.message)
    } finally {
      setBusy(false)
    }
  }

  const runStatus = async () => {
    setBusy(true)
    setError(null)
    try {
      setMeta(await fetchMeta())
    } catch (e) {
      setError(e.message)
    } finally {
      setBusy(false)
    }
  }

  const doExport = async (format) => {
    setBusy(true)
    setError(null)
    try {
      const text = await exportGraph(format)
      downloadGraphExport(format, text)
    } catch (e) {
      setError(e.message)
    } finally {
      setBusy(false)
    }
  }

  const toggleWatch = async () => {
    setBusy(true)
    setError(null)
    try {
      const action = meta?.watch?.active ? 'stop' : 'start'
      const status = await watchControl(action)
      setMeta((m) => ({ ...m, watch: status }))
    } catch (e) {
      setError(e.message)
    } finally {
      setBusy(false)
    }
  }

  const openTab = (name) => {
    setTab(name)
    setBlast(null)
    setError(null)
    if (name === 'hotspots') runHotspots()
    if (name === 'status') runStatus()
  }

  return (
    <div style={{ borderTop: '1px solid #21262d', padding: '10px 16px' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 8 }}>
        <span style={{ fontSize: 12, fontWeight: 600, color: '#e6edf3' }}>📐 Insights</span>
        <span style={{ fontSize: 10, color: '#6e7681' }}>blast radius · hotspots · export — no LLM</span>
      </div>

      <div style={{ display: 'flex', gap: 4, flexWrap: 'wrap' }}>
        {[
          { name: 'blast', label: '💥 Blast radius' },
          { name: 'hotspots', label: '🔥 Hotspots' },
          { name: 'status', label: '⚙️ Status' },
        ].map((t) => (
          <button key={t.name} onClick={() => (tab === t.name ? close() : openTab(t.name))}
            style={{ ...chip, background: tab === t.name ? '#1f6feb33' : '#161b22', borderColor: tab === t.name ? '#1f6feb66' : '#30363d' }}>
            {t.label}
          </button>
        ))}
      </div>

      {error && <div style={{ color: '#f85149', fontSize: 12, marginTop: 8 }}>{error}</div>}

      {tab === 'blast' && (
        <div style={{ marginTop: 8 }}>
          <div style={{ display: 'flex', gap: 6 }}>
            <input
              value={blastInput}
              onChange={(e) => setBlastInput(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && runBlast()}
              placeholder="node id (e.g. db.py::save_order)"
              style={{ flex: 1, background: '#161b22', border: '1px solid #30363d', borderRadius: 6, color: '#e6edf3', padding: '6px 9px', fontSize: 12 }}
            />
            <select value={blastHops} onChange={(e) => setBlastHops(Number(e.target.value))}
              style={{ background: '#161b22', border: '1px solid #30363d', borderRadius: 6, color: '#e6edf3', fontSize: 12 }}>
              {[1, 2, 3, 4, 5].map((h) => <option key={h} value={h}>{h} hop{h > 1 ? 's' : ''}</option>)}
            </select>
            <button onClick={runBlast} disabled={busy || !blastInput.trim()} style={runBtn}>
              {busy ? '…' : 'Go'}
            </button>
          </div>
          {blast && (
            <div style={{ marginTop: 8, fontSize: 12 }}>
              <div style={{ color: '#e6edf3' }}>
                <strong style={{ color: blast.total > 20 ? '#f85149' : blast.total > 8 ? '#d29922' : '#3fb950' }}>
                  {blast.total} node{blast.total === 1 ? '' : 's'}
                </strong> affected within {blast.hops} hop{blast.hops === 1 ? '' : 's'} of <code style={{ fontSize: 11 }}>{blast.root}</code>
              </div>
              {blast.rings.map((ring) => (
                <div key={ring.hop} style={{ marginTop: 6 }}>
                  <div style={{ fontSize: 10, color: '#6e7681', marginBottom: 2 }}>hop {ring.hop}</div>
                  {ring.nodes.slice(0, 12).map((n) => (
                    <button key={n.id} onClick={() => onFocusNode?.(n.id)}
                      style={{ display: 'block', width: '100%', textAlign: 'left', background: 'transparent', border: 'none', color: '#8b949e', fontSize: 11, padding: '1px 0', cursor: 'pointer' }}>
                      <span style={{ color: '#58a6ff' }}>{n.id}</span>{' '}
                      <span style={{ color: '#6e7681' }}>{n.type} · {n.file}:{n.line_start}</span>
                    </button>
                  ))}
                  {ring.nodes.length > 12 && <div style={{ fontSize: 10, color: '#6e7681' }}>+{ring.nodes.length - 12} more…</div>}
                </div>
              ))}
              {blast.total === 0 && <div style={{ color: '#6e7681', marginTop: 4 }}>nothing depends on this node 🎉</div>}
            </div>
          )}
        </div>
      )}

      {tab === 'hotspots' && (
        <div style={{ marginTop: 8 }}>
          {hotspots && (
            <div>
              <div style={{ fontSize: 10, color: '#6e7681', marginBottom: 4 }}>ranked by fan-in + fan-out — the risky parts</div>
              {hotspots.map((h, i) => (
                <button key={h.id} onClick={() => onFocusNode?.(h.id)}
                  style={{ display: 'flex', width: '100%', alignItems: 'center', gap: 8, background: 'transparent', border: 'none', padding: '3px 0', cursor: 'pointer', textAlign: 'left' }}>
                  <span style={{ fontSize: 10, color: '#6e7681', width: 14 }}>{i + 1}</span>
                  <span style={{
                    fontSize: 10, color: '#fff', borderRadius: 3, padding: '1px 5px',
                    background: h.degree > 15 ? '#f8514944' : h.degree > 8 ? '#d2992244' : '#23863644',
                  }}>{h.degree}</span>
                  <span style={{ fontSize: 11, color: '#58a6ff', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{h.id}</span>
                  <span style={{ fontSize: 10, color: '#6e7681', marginLeft: 'auto' }}>in {h.fan_in} / out {h.fan_out}</span>
                </button>
              ))}
            </div>
          )}
        </div>
      )}

      {tab === 'status' && (
        <div style={{ marginTop: 8, fontSize: 12 }}>
          {meta && (
            <div>
              <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 6 }}>
                <span style={{ color: meta.watch?.active ? '#3fb950' : '#6e7681' }}>
                  {meta.watch?.active ? '👁 watching for file changes' : '👁 file watch off'}
                </span>
                <button onClick={toggleWatch} disabled={busy} style={{ ...chip, marginLeft: 'auto' }}>
                  {meta.watch?.active ? 'stop' : 'start'}
                </button>
              </div>
              <div style={{ fontSize: 10, color: '#6e7681', marginBottom: 4 }}>provider chain (failover order):</div>
              {(meta.providers || []).map((p) => (
                <div key={p.name} style={{ display: 'flex', alignItems: 'center', gap: 6, padding: '2px 0' }}>
                  <span style={{
                    fontSize: 9, borderRadius: 3, padding: '1px 5px',
                    background: !p.has_key ? '#30363d' : p.circuit_open ? '#f8514944' : '#23863644',
                    color: !p.has_key ? '#6e7681' : p.circuit_open ? '#f85149' : '#3fb950',
                  }}>
                    {!p.has_key ? 'no key' : p.circuit_open ? 'cooling down' : 'ready'}
                  </span>
                  <span style={{ color: '#e6edf3', fontSize: 11 }}>{p.name}</span>
                  <span style={{ color: '#6e7681', fontSize: 10, marginLeft: 'auto' }}>{p.rpm_limit} rpm</span>
                </div>
              ))}
              {meta.last_provider && (
                <div style={{ marginTop: 6, fontSize: 11, color: '#8b949e' }}>
                  last LLM answer served by <span style={{ color: '#58a6ff' }}>{meta.last_provider}</span>
                </div>
              )}
              <div style={{ display: 'flex', gap: 6, marginTop: 10, flexWrap: 'wrap' }}>
                {['json', 'graphml', 'dot'].map((f) => (
                  <button key={f} onClick={() => doExport(f)} disabled={busy} style={chip}>
                    ⬇ {f}
                  </button>
                ))}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  )
}

const chip = {
  fontSize: 10,
  background: '#161b22',
  border: '1px solid #30363d',
  borderRadius: 10,
  color: '#8b949e',
  padding: '3px 8px',
  cursor: 'pointer',
}

const runBtn = {
  background: '#238636',
  color: '#fff',
  border: 'none',
  borderRadius: 6,
  padding: '6px 10px',
  fontSize: 12,
  cursor: 'pointer',
  fontWeight: 600,
}
