import { useEffect, useState } from 'react'
import { runAgentTask } from './api.js'

const PRESETS = [
  'Add a docstring to every function missing one',
  'Rename the variable `total` to `order_total` in cart.py and update all call sites',
  'Add a null check to checkout before saving the order',
]

const jsonLang = { fontSize: 12, lineHeight: 1.5 }

// Mode 3 (v1) agent panel: task in -> proposed diff + sandboxed tests + PR out.
// The tool proposes; the human approves. Nothing here merges.
export default function AgentPanel({ repo, onHighlight }) {
  const [task, setTask] = useState('')
  const [pending, setPending] = useState(false)
  const [result, setResult] = useState(null)
  const [error, setError] = useState(null)
  const [showDiff, setShowDiff] = useState(true)

  useEffect(() => () => onHighlight?.(null), [onHighlight])

  const run = async (t) => {
    const taskText = (t ?? task).trim()
    if (!taskText || pending) return
    if (!repo) { setError('Parse a repo first — the agent works on the loaded graph + repo path.'); return }
    setPending(true)
    setError(null)
    setResult(null)
    try {
      const res = await runAgentTask(repo, taskText)
      setResult(res)
      onHighlight?.(res.retrieval?.used_nodes || [])
    } catch (e) {
      setError(e.message)
    } finally {
      setPending(false)
    }
  }

  const close = () => {
    setResult(null)
    setError(null)
    onHighlight?.(null)
  }

  const testBadge = !result?.tests ? null : result.tests.ran
    ? result.tests.ok
      ? { text: '✅ tests passed', color: '#3fb950', bg: '#12351f' }
      : { text: '❌ tests failed', color: '#f85149', bg: '#3a1416' }
    : { text: '⚠️ tests not run', color: '#d29922', bg: '#33290f' }

  return (
    <div style={{ borderTop: '1px solid #21262d' }}>
      <div style={{ padding: '10px 16px' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 8 }}>
          <span style={{ fontSize: 12, fontWeight: 600, color: '#e6edf3' }}>🤖 Agent task</span>
          <span style={{ fontSize: 10, color: '#6e7681' }}>proposes a diff on a new branch — never merges</span>
        </div>
        <div style={{ display: 'flex', gap: 6 }}>
          <input
            value={task}
            onChange={(e) => setTask(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && run()}
            placeholder="e.g. add a null check in checkout()"
            style={{ flex: 1, background: '#161b22', border: '1px solid #30363d', borderRadius: 6, color: '#e6edf3', padding: '7px 10px', fontSize: 12 }}
          />
          <button onClick={() => run()} disabled={pending || !task.trim()} style={{ ...btn, opacity: pending || !task.trim() ? 0.5 : 1 }}>
            {pending ? 'working…' : 'Run'}
          </button>
        </div>
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 4, marginTop: 6 }}>
          {PRESETS.map((p) => (
            <button key={p} onClick={() => { setTask(p); run(p) }} disabled={pending}
              style={{ fontSize: 10, background: '#161b22', border: '1px solid #30363d', borderRadius: 10, color: '#8b949e', padding: '3px 8px', cursor: 'pointer' }}>
              {p.length > 42 ? p.slice(0, 42) + '…' : p}
            </button>
          ))}
        </div>
        {error && <div style={{ color: '#f85149', fontSize: 12, marginTop: 8, whiteSpace: 'pre-wrap' }}>{error}</div>}
      </div>

      {pending && (
        <div style={{ padding: '0 16px 12px', color: '#6e7681', fontSize: 12 }}>
          retrieving subgraph → generating diff → applying on branch → running tests…
        </div>
      )}

      {result && (
        <div style={{ margin: '0 16px 14px', border: '1px solid #30363d', borderRadius: 8, overflow: 'hidden' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '8px 10px', background: '#161b22' }}>
            <strong style={{ fontSize: 12, marginRight: 'auto' }}>Proposal ready</strong>
            {testBadge && (
              <span style={{ fontSize: 10, color: testBadge.color, background: testBadge.bg, borderRadius: 4, padding: '2px 6px' }}>
                {testBadge.text}
              </span>
            )}
            <button onClick={close} style={ghost}>×</button>
          </div>

          <div style={{ padding: '8px 10px', fontSize: 12, lineHeight: 1.6 }}>
            <div>🌿 <code style={jsonLang}>{result.branch}</code></div>
            <div style={{ color: '#8b949e' }}>
              commit <code>{result.retrieval?.commit || '?'}</code> · {result.files.length} file(s) · {result.seconds}s
            </div>
            <div style={{ marginTop: 4 }}>
              {result.pr?.opened
                ? <>🔔 <a href={result.pr.url} target="_blank" rel="noreferrer" style={{ color: '#58a6ff' }}>Pull request opened</a> <span style={{ color: '#6e7681' }}>({result.pr.via})</span></>
                : <span style={{ color: '#d29922' }}>📦 {result.pr?.note || 'commit left on the branch for local review'}</span>}
            </div>
          </div>

          {result.tests?.ran && (
            <div style={{ margin: '0 10px 10px', background: '#0d1117', border: '1px solid #21262d', borderRadius: 6, padding: '6px 8px', maxHeight: 120, overflowY: 'auto' }}>
              <div style={{ fontSize: 10, color: '#6e7681', marginBottom: 4 }}>$ {result.tests.cmd}</div>
              <pre style={{ margin: 0, fontSize: 11, whiteSpace: 'pre-wrap', color: result.tests.ok ? '#3fb950' : '#f85149' }}>
                {result.tests.output_tail}
              </pre>
            </div>
          )}

          <button onClick={() => setShowDiff((s) => !s)} style={{ ...ghost, display: 'block', width: '100%', textAlign: 'left', padding: '6px 10px', borderTop: '1px solid #21262d', fontSize: 12 }}>
            {showDiff ? '▾' : '▸'} diff ({result.diff.splitlines().length} lines)
          </button>
          {showDiff && (
            <pre style={{ margin: 0, maxHeight: 260, overflowY: 'auto', padding: '8px 10px', background: '#0d1117', borderTop: '1px solid #21262d', fontSize: 11, lineHeight: 1.45, whiteSpace: 'pre-wrap' }}>
              {result.diff.split('\n').map((ln, i) => (
                <div key={i} style={{
                  color: ln.startsWith('+') && !ln.startsWith('+++') ? '#3fb950'
                    : ln.startsWith('-') && !ln.startsWith('---') ? '#f85149'
                    : ln.startsWith('@@') ? '#58a6ff' : '#8b949e',
                }}>{ln || ' '}</div>
              ))}
            </pre>
          )}
        </div>
      )}
    </div>
  )
}

const btn = {
  background: '#238636',
  color: '#fff',
  border: 'none',
  borderRadius: 6,
  padding: '7px 12px',
  fontSize: 12,
  cursor: 'pointer',
  fontWeight: 600,
}

const ghost = {
  background: 'transparent',
  color: '#8b949e',
  border: 'none',
  fontSize: 13,
  cursor: 'pointer',
  lineHeight: 1,
}
