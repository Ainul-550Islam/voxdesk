import { useState } from 'react'
import { login, ApiError } from '../lib/api'

const wrap = {
  minHeight: '100vh', display: 'grid', placeItems: 'center',
  background: '#f9fafb', fontFamily: 'system-ui',
}
const card = {
  background: '#fff', padding: 32, borderRadius: 14, width: 360,
  boxShadow: '0 1px 3px rgba(0,0,0,.1)',
}
const field = {
  width: '100%', padding: '10px 12px', marginTop: 6, marginBottom: 16,
  border: '1px solid #d1d5db', borderRadius: 8, fontSize: 14, boxSizing: 'border-box',
}

export default function Login({ onSuccess }) {
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(false)

  async function submit(e) {
    e.preventDefault()
    setBusy(true)
    setError(null)
    try {
      onSuccess(await login(email, password))
    } catch (err) {
      // Show the server's own generic message. Never distinguish "no such
      // user" from "wrong password" in the UI.
      setError(err instanceof ApiError ? err.message : 'Could not sign in.')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div style={wrap}>
      <form style={card} onSubmit={submit}>
        <h1 style={{ margin: 0, fontSize: 22 }}>VoxDesk</h1>
        <p style={{ color: '#6b7280', marginTop: 4, marginBottom: 24, fontSize: 14 }}>
          Sign in to your workspace
        </p>

        <label style={{ fontSize: 13, fontWeight: 600 }}>
          Email
          <input
            style={field} type="email" value={email} required autoFocus
            autoComplete="username" onChange={(e) => setEmail(e.target.value)}
          />
        </label>

        <label style={{ fontSize: 13, fontWeight: 600 }}>
          Password
          <input
            style={field} type="password" value={password} required
            autoComplete="current-password"
            onChange={(e) => setPassword(e.target.value)}
          />
        </label>

        {error && (
          <div
            role="alert"
            style={{
              background: '#fef2f2', color: '#b91c1c', padding: '10px 12px',
              borderRadius: 8, fontSize: 13, marginBottom: 16,
            }}
          >
            {error}
          </div>
        )}

        <button
          type="submit" disabled={busy}
          style={{
            width: '100%', padding: 11, borderRadius: 8, border: 0,
            background: busy ? '#9ca3af' : '#111827', color: '#fff',
            fontSize: 15, fontWeight: 600, cursor: busy ? 'default' : 'pointer',
          }}
        >
          {busy ? 'Signing in…' : 'Sign in'}
        </button>

        <p style={{ color: '#9ca3af', fontSize: 12, marginTop: 20, marginBottom: 0 }}>
          Accounts are created by your workspace owner.
        </p>
      </form>
    </div>
  )
}