import React from 'react'

// Last line of defense: a crash anywhere in the app renders this panel with
// the actual error instead of a silent black page.
export default class ErrorBoundary extends React.Component {
  constructor(props) {
    super(props)
    this.state = { error: null }
  }

  static getDerivedStateFromError(error) {
    return { error }
  }

  componentDidCatch(error, info) {
    console.error('[CodeGraph] crash:', error, info?.componentStack)
  }

  render() {
    if (this.state.error) {
      return (
        <div style={{ padding: 24, fontFamily: 'ui-monospace, Menlo, monospace', fontSize: 13, color: '#f85149' }}>
          <h2 style={{ fontSize: 16, marginBottom: 8 }}>💥 CodeGraph crashed</h2>
          <pre style={{ whiteSpace: 'pre-wrap', color: '#e6edf3' }}>{String(this.state.error?.message || this.state.error)}</pre>
          <button
            onClick={() => { this.setState({ error: null }); location.reload() }}
            style={{ marginTop: 12, background: '#238636', color: '#fff', border: 'none', borderRadius: 6, padding: '8px 14px', fontSize: 13, cursor: 'pointer', fontWeight: 600 }}
          >
            Reload
          </button>
        </div>
      )
    }
    return this.props.children
  }
}
