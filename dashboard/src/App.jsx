/**
 * The application root.
 *
 * STEP 8 phase 1: this file used to *be* the product — header, stat tiles,
 * call table and a transcript panel, all inline, on one screen. The Shell,
 * router and seven pages already existed in the tree but nothing imported
 * them, so none of it ran. This composes them.
 *
 * Responsibilities kept deliberately small:
 *
 *   1. own the session (`bootstrap`, `logout`, the 401 handler),
 *   2. build one `can()` from the server's permission list,
 *   3. resolve the current hash route to a page,
 *   4. refuse to render a page the account has no permission for.
 *
 * Everything else belongs to a page. The audit's F5 finding was that fetch
 * logic lived here; it now lives in `useApi`, and this file makes no data
 * request of its own beyond `/auth/me`.
 */
import { useCallback, useEffect, useMemo, useState } from 'react'

import Login from './components/Login'
import Shell from './components/Shell'
import { EmptyState } from './components/ui'
import { bootstrap, logout, setUnauthorizedHandler } from './lib/api'
import { makeCan, PERMISSIONS as P } from './lib/permissions'
import { match, navigate, useRoute } from './lib/router'

import Agent from './pages/Agent'
import Analytics from './pages/Analytics'
import Appointments from './pages/Appointments'
import CallDetail from './pages/CallDetail'
import Calls from './pages/Calls'
import Campaigns from './pages/Campaigns'
import Billing from './pages/Billing'
import Audit from './pages/Audit'
import Team from './pages/Team'
import Integrations from './pages/Integrations'
import Knowledge from './pages/Knowledge'
import Leads from './pages/Leads'
import Overview from './pages/Overview'

/**
 * The route table.
 *
 * `permission` is the *client* half of the gate and is UX only — it decides
 * whether we render the page or an explanatory panel, and it is what keeps a
 * manager from staring at a bare 403. The server enforces the same permission
 * again on every request the page makes, and that is the boundary that
 * matters. Phase 2 adds these one page at a time;
 */
const ROUTES = [
  {
    pattern: '/',
    title: 'Overview',
    permission: P.ANALYTICS_READ,
    render: (props) => <Overview {...props} />,
  },
  {
    pattern: '/overview',
    title: 'Overview',
    permission: P.ANALYTICS_READ,
    render: (props) => <Overview {...props} />,
  },
  {
    pattern: '/calls',
    title: 'Calls',
    permission: P.CALL_READ,
    render: (props) => <Calls {...props} />,
  },
  {
    // Before `/calls` would match it — `match()` is exact on segment count,
    // so ordering is not load-bearing, but keeping the specific route first
    // survives someone later making the matcher prefix-based.
    pattern: '/calls/:id',
    title: 'Call detail',
    permission: P.CALL_READ,
    render: (props, params) => <CallDetail {...props} callId={params.id} />,
  },
  {
    pattern: '/leads',
    title: 'Leads',
    permission: P.LEAD_READ,
    render: (props) => <Leads {...props} />,
  },
  {
    pattern: '/appointments',
    title: 'Appointments',
    permission: P.APPOINTMENT_READ,
    render: (props) => <Appointments {...props} />,
  },
  {
    pattern: '/campaigns',
    title: 'Campaigns',
    permission: P.CAMPAIGN_READ,
    render: (props) => <Campaigns {...props} />,
  },
  {
    pattern: '/analytics',
    title: 'Analytics',
    permission: P.ANALYTICS_READ,
    render: (props) => <Analytics {...props} />,
  },
  {
    // Read access is `tenant:read`, which every role has -- a viewer may see
    // how the agent is configured. The write controls inside the page are
    // gated separately on `tenant:update`, and the server enforces that again
    // on `PATCH .../voice`.
    pattern: '/agent',
    title: 'Agent',
    permission: P.TENANT_READ,
    render: (props) => <Agent {...props} />,
  },
  {
    // Read is `knowledge:read`, which every role has. Upload, reindex and
    // archive are gated on `knowledge:write` inside the page, and a hard
    // purge additionally on `knowledge:delete` -- the server re-checks all
    // three.
    pattern: '/knowledge',
    title: 'Knowledge',
    permission: P.KNOWLEDGE_READ,
    render: (props) => <Knowledge {...props} />,
  },
  {
    // Read covers both the CRM and calendar catalogues and the tenant's
    // configured rows. Connect, disable and remove are gated on
    // `integration:write` inside the page; the server re-checks. Note the
    // backend gates "test connection" on read, not write -- a diagnosis
    // changes nothing.
    pattern: '/integrations',
    title: 'Integrations',
    permission: P.INTEGRATION_READ,
    render: (props) => <Integrations {...props} />,
  },
  {
    // `billing:read` is owner+admin. Every mutation is gated on
    // `billing:write`, which is owner-only, inside the page -- and the
    // server re-checks.
    pattern: '/billing',
    title: 'Billing',
    permission: P.BILLING_READ,
    render: (props) => <Billing {...props} />,
  },
  {
    // `user:read` is owner+admin only -- manager and below get the
    // access-denied state. Create, role change and deactivate are gated
    // separately inside the page, and the server re-checks all of them.
    pattern: '/team',
    title: 'Team',
    permission: P.USER_READ,
    render: (props) => <Team {...props} />,
  },
  {
    // `audit:read` is owner+admin, same as the team page. View-only: the
    // endpoint offers no mutation, and neither does this route.
    pattern: '/audit',
    title: 'Audit log',
    permission: P.AUDIT_READ,
    render: (props) => <Audit {...props} />,
  },
]

/** Resolve a path to `{ route, params }`, or `null` for a 404. */
export function resolveRoute(path) {
  for (const route of ROUTES) {
    const params = match(route.pattern, path)
    if (params) return { route, params }
  }
  return null
}

export default function App() {
  const [me, setMe] = useState(null)
  const [checking, setChecking] = useState(true)
  const path = useRoute()

  // A 401 from anywhere drops the session and returns us to the login screen.
  // `api.js` has already cleared the token by the time this runs.
  useEffect(() => {
    setUnauthorizedHandler(() => setMe(null))
  }, [])

  // Page load: trade the HttpOnly refresh cookie for a session, if there is
  // one. This is unchanged from the legacy App and is the reason a reload
  // does not sign you out even though the access token is only in memory.
  useEffect(() => {
    let alive = true
    bootstrap()
      .then((session) => { if (alive) setMe(session) })
      .finally(() => { if (alive) setChecking(false) })
    return () => { alive = false }
  }, [])

  const can = useMemo(() => makeCan(me?.permissions), [me])

  const onSignOut = useCallback(() => {
    // `logout()` clears the token and fires the unauthorized handler, which
    // is what actually drops `me`. Failure still clears locally.
    logout()
  }, [])

  if (checking) {
    return (
      <div className="boot" role="status" aria-live="polite">
        Loading…
      </div>
    )
  }

  if (!me) {
    return <Login onSuccess={() => bootstrap().then(setMe)} />
  }

  const resolved = resolveRoute(path)

  let title = 'Not found'
  let body

  if (!resolved) {
    body = (
      <EmptyState
        icon="?"
        title="That page does not exist"
        description="The link may be out of date, or the page may not be built yet."
        action={
          <button type="button" className="btn" onClick={() => navigate('/')}>
            Go to overview
          </button>
        }
      />
    )
  } else if (!can(resolved.route.permission)) {
    // Deliberately *not* a redirect. Bouncing someone to the overview for a
    // link a colleague sent them is indistinguishable from a broken link.
    title = resolved.route.title
    body = (
      <EmptyState
        icon="🔒"
        title="You do not have access to this page"
        description="Your role does not include permission to view it. Ask an owner or admin if you need access."
      />
    )
  } else {
    title = resolved.route.title
    body = resolved.route.render({ me, can }, resolved.params)
  }

  return (
    <Shell me={me} can={can} path={path} title={title} onSignOut={onSignOut}>
      {body}
    </Shell>
  )
}
