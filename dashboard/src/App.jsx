import { useEffect, useState, useCallback } from 'react'
import {
  bootstrap, logout, getCalls, getStats, getTranscript,
  setUnauthorizedHandler, ApiError,
} from './lib/api'
import Login from './components/Login'
import StatCards from './components/StatCards'
import CallTable from './components/CallTable'

export default function App() {
  const [me, setMe] = useState(null)          // { user, tenant, permissions }
  const [checking, setChecking] = useState(true)
  const [calls, setCalls] = useState([])
  const [stats, setStats] = useState(null)
  const [transcript, setTranscript] = useState(null)
  const [notice, setNotice] = useState(null)

  // Any 401 anywhere in the app drops us back to the login screen.
  useEffect(() => {
    setUnauthorizedHandler(() => setMe(null))
  }, [])

  // Page load: try to resume the session from the HttpOnly refresh cookie.
  useEffect(() => {
    bootstrap()
      .then(setMe)
      .finally(() => setChecking(false))
  }, [])

  const tenantId = me?.tenant?.id

  const load = useCallback(async () => {
    if (!tenantId) return
    try {
      const [c, s] = await Promise.all([getCalls(tenantId), getStats(tenantId)])
      setCalls(c)
      setStats(s)
    } catch (err) {
      if (err instanceof ApiError && err.status === 403) {
        setNotice('Your role does not include access to the call log.')
      }
    }
  }, [tenantId])

  useEffect(() => { load() }, [load])

  if (checking) {
    return (
      <div style={{ display: 'grid', placeItems: 'center', minHeight: '100vh',
                    fontFamily: 'system-ui', color: '#6b7280' }}>
        Loading…
      </div>
    )
  }

  // Protected route: nothing below renders without a session.
  if (!me) {
    return <Login onSuccess={() => bootstrap().then(setMe)} />
  }

  const can = (perm) => me.permissions.includes(perm)

  return (
    <div style={{ fontFamily: 'system-ui', background: '#f9fafb',
                  minHeight: '100vh', padding: 24 }}>
      <header style={{ display: 'flex', justifyContent: 'space-between',
                       alignItems: 'flex-start' }}>
        <div>
          <h1 style={{ margin: 0 }}>VoxDesk</h1>
          <p style={{ color: '#6b7280', marginTop: 4 }}>
            {me.tenant.name} — {me.tenant.twilio_number}
          </p>
        </div>
        <div style={{ textAlign: 'right', fontSize: 13 }}>
          <div style={{ fontWeight: 600 }}>{me.user.email}</div>
          <div style={{ color: '#6b7280', textTransform: 'capitalize' }}>
            {me.user.role}
          </div>
          <button
            onClick={() => logout()}
            style={{ marginTop: 8, padding: '6px 12px', borderRadius: 7,
                     border: '1px solid #d1d5db', background: '#fff',
                     cursor: 'pointer' }}
          >
            Sign out
          </button>
        </div>
      </header>

      {notice && (
        <div style={{ background: '#fffbeb', color: '#92400e', padding: '10px 14px',
                      borderRadius: 8, fontSize: 13, margin: '16px 0' }}>
          {notice}
        </div>
      )}

      {can('analytics:read') && <StatCards stats={stats} />}

      {can('call:read') ? (
        <>
          <h3>Recent calls</h3>
          <CallTable
            calls={calls}
            onSelect={(id) =>
              getTranscript(id)
                .then(setTranscript)
                .catch(() => setNotice('That transcript is not available to you.'))
            }
          />
        </>
      ) : (
        <p style={{ color: '#6b7280' }}>Your role does not include the call log.</p>
      )}

      {transcript && (
        <div style={{ marginTop: 20, background: '#fff', padding: 16, borderRadius: 10 }}>
          <button onClick={() => setTranscript(null)}>close</button>
          {transcript.map((t, i) => (
            <p key={i}>
              <b>{t.speaker === 'user' ? 'Caller' : 'Alex'}:</b> {t.text}
            </p>
          ))}
        </div>
      )}
    </div>
  )
}
