import React from 'react'
import ReactDOM from 'react-dom/client'

import App from './App'
// The audit found `styles/app.css` (402 lines) imported from nowhere, so every
// `className` in Shell, ui, charts and the pages rendered unstyled. This is
// the only place it belongs: the real entry path, before the tree mounts.
import './styles/app.css'

ReactDOM.createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
)