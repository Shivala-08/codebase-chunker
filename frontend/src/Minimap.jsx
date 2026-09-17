import { useEffect, useRef } from 'react'

const TYPE_COLORS = {
  module: '#58a6ff',
  class: '#bc8cff',
  function: '#3fb950',
  method: '#f0883e',
}

// Live minimap: every node as a 2px dot, viewport rectangle, click/drag to
// navigate. Redraws only when the camera or data actually changes (rAF-gated).
export default function Minimap({ data, highlight, query, fgRef, graphW, graphH, width = 230, height = 170 }) {
  const canvasRef = useRef()
  const mapRef = useRef(null)      // {s, ox, oy}: graph -> minimap transform
  const prevRef = useRef({})       // last-drawn state for change detection
  const dragRef = useRef(false)

  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas) return
    const ctx = canvas.getContext('2d')
    const dpr = window.devicePixelRatio || 1
    canvas.width = width * dpr
    canvas.height = height * dpr
    let raf

    const draw = () => {
      raf = requestAnimationFrame(draw)
      const fg = fgRef.current
      if (!fg || !data || data.nodes.length === 0) return
      const k = fg.zoom() ?? 1
      const c = fg.centerAt() ?? { x: 0, y: 0 }
      const p = prevRef.current
      if (p.k === k && p.cx === c.x && p.cy === c.y && p.data === data && p.hl === highlight && p.q === query) return
      prevRef.current = { k, cx: c.x, cy: c.y, data, hl: highlight, q: query }

      ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
      ctx.clearRect(0, 0, width, height)

      let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity
      for (const n of data.nodes) {
        if (n.x == null) continue
        if (n.x < minX) minX = n.x
        if (n.x > maxX) maxX = n.x
        if (n.y < minY) minY = n.y
        if (n.y > maxY) maxY = n.y
      }
      if (!isFinite(minX)) return

      const pad = 6
      const bw = Math.max(maxX - minX, 1)
      const bh = Math.max(maxY - minY, 1)
      const s = Math.min((width - 2 * pad) / bw, (height - 2 * pad) / bh)
      const ox = pad + ((width - 2 * pad) - bw * s) / 2 - minX * s
      const oy = pad + ((height - 2 * pad) - bh * s) / 2 - minY * s
      mapRef.current = { s, ox, oy }

      const q = query.trim().toLowerCase()
      for (const n of data.nodes) {
        if (n.x == null) continue
        const cited = highlight.has(n.id)
        const isMatch = q && (
          n.name?.toLowerCase().includes(q) ||
          n.qualname?.toLowerCase().includes(q) ||
          n.file?.toLowerCase().includes(q))
        ctx.globalAlpha = (q && !isMatch && !cited) ? 0.25 : (cited || isMatch) ? 1 : 0.6
        ctx.fillStyle = cited ? '#ffffff' : isMatch ? '#d29922' : (TYPE_COLORS[n.type] || '#8b949e')
        ctx.fillRect(ox + n.x * s - 1, oy + n.y * s - 1, 2, 2)
      }
      ctx.globalAlpha = 1

      const gw = graphW / k
      const gh = graphH / k
      ctx.strokeStyle = 'rgba(88,166,255,0.9)'
      ctx.lineWidth = 1.5
      ctx.strokeRect(ox + (c.x - gw / 2) * s, oy + (c.y - gh / 2) * s, gw * s, gh * s)
    }
    draw()
    return () => cancelAnimationFrame(raf)
  }, [data, highlight, query, fgRef, graphW, graphH, width, height])

  const toGraph = (e) => {
    const m = mapRef.current
    const fg = fgRef.current
    if (!m || !fg) return
    const r = canvasRef.current.getBoundingClientRect()
    fg.centerAt((e.clientX - r.left - m.ox) / m.s, (e.clientY - r.top - m.oy) / m.s)
  }

  return (
    <canvas
      ref={canvasRef}
      style={{ width, height, display: 'block', background: 'rgba(13,17,23,0.85)', border: '1px solid #30363d', borderRadius: 8, cursor: 'crosshair' }}
      onMouseDown={(e) => { dragRef.current = true; toGraph(e) }}
      onMouseMove={(e) => { if (dragRef.current && e.buttons === 1) toGraph(e) }}
      onMouseUp={() => { dragRef.current = false }}
      onMouseLeave={() => { dragRef.current = false }}
      title="Minimap — click/drag to navigate"
    />
  )
}
