import React from 'react'
import { createRoot } from 'react-dom/client'
import App from './App.jsx'
import ErrorBoundary from './ErrorBoundary.jsx'

// NOTE: no <React.StrictMode> — its dev-only double effect-invoke fires the
// 1.6MB /graph fetch twice on mount and the second response body stalls the
// hydration path (first load shows the eternal "Parse a repo" empty state).
createRoot(document.getElementById('root')).render(
  <ErrorBoundary>
    <App />
  </ErrorBoundary>,
)
