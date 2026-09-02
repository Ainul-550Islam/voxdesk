/**
 * The application shell.
 *
 * Permission-aware navigation: a link the account cannot use is not rendered.
 * **That is UX, not security.** Every route behind it is enforced again by
 * `require_permission` server-side, and hiding a link only saves someone a
 * 403 they could not act on anyway.
 */
import { useEffect, useState } from 'react'

import { navigate } from '../lib/router'
import { PERMISSIONS as P } from '../lib/permissions'

const NAV = [
  {
    group: 'Operations',
    items: [
      { path: '/', label: 'Overview', icon: '◫', permission: P.ANALYTICS_READ },
      { path: '/calls', label: 'Calls', icon: '☎', permission: P.CALL_READ },
      { path: '/leads', label: 'Leads', icon: '◧', permission: P.LEAD_READ },
      {
        path: '/appointments', label: 'Appointments', icon: '▤',
        permission: P.APPOINTMENT_READ,
      },
      {
        path: '/campaigns', label: 'Campaigns', icon: '➤',
        permission: P.CAMPAIGN_READ,
      },
    ],
  },
  {
    group: 'Insight',
    items: [
      { path: '/analytics', label: 'Analytics', icon: '◭', permission: P.ANALYTICS_READ },
    ],
  },
  {
    group: 'Configuration',
    items: [
      { path: '/agent', label: 'Agent', icon: '✦', permission: P.TENANT_UPDATE },
      { path: '/knowledge', label: 'Knowledge', icon: '▣', permission: P.KNOWLEDGE_READ },
      {
        path: '/integrations', label: 'Integrations', icon: '⇄',
        permission: P.INTEGRATION_READ,
      },
    ],
  },
  {
    group: 'Account',
    items: [
      { path: '/billing', label: 'Billing', icon: '$', permission: P.BILLING_READ },
      { path: '/team', label: 'Team', icon: '☰', permission: P.USER_READ },
      { path: '/audit', label: 'Audit log', icon: '⎙', permission: P.AUDIT_READ },
    ],
  },
]

export function visibleNav(can) {
  return NAV
    .map((section) => ({
      ...section,
      items: section.items.filter((item) => can(item.permission)),
    }))
    .filter((section) => section.items.length > 0)
}

export default function Shell({ me, can, path, title, onSignOut, children }) {
  const [menuOpen, setMenuOpen] = useState(false)
  const sections = visibleNav(can)

  // Close the drawer on navigation, or a mobile user taps a link and stares
  // at the menu that is still covering the page they asked for.
  useEffect(() => { setMenuOpen(false) }, [path])

  const go = (target) => (event) => {
    event.preventDefault()
    navigate(target)
  }

  return (
    <div className="shell">
      <a className="skip-link" href="#main-content">Skip to content</a>

      {menuOpen && (
        <div
          className="scrim"
          onClick={() => setMenuOpen(false)}
          aria-hidden="true"
        />
      )}

      <nav
        className="sidebar"
        data-open={menuOpen}
        aria-label="Main navigation"
        id="main-nav"
      >
        <div className="sidebar__brand">
          <div className="sidebar__brand-name">VoxDesk</div>
          <div className="sidebar__brand-sub">{me.tenant.name}</div>
        </div>

        <div className="sidebar__nav">
          {sections.map((section) => (
            <div key={section.group}>
              <div className="sidebar__group-label">{section.group}</div>
              {section.items.map((item) => {
                const active =
                  item.path === '/'
                    ? path === '/'
                    : path.startsWith(item.path)
                return (
                  <a
                    key={item.path}
                    href={`#${item.path}`}
                    className="nav-item"
                    aria-current={active ? 'page' : undefined}
                    onClick={go(item.path)}
                  >
                    <span className="nav-item__icon" aria-hidden="true">
                      {item.icon}
                    </span>
                    {item.label}
                  </a>
                )
              })}
            </div>
          ))}
        </div>
      </nav>

      <div className="main">
        <header className="topbar">
          <div className="topbar__left">
            <button
              type="button"
              className="btn btn--small hamburger"
              aria-label={menuOpen ? 'Close navigation' : 'Open navigation'}
              aria-expanded={menuOpen}
              aria-controls="main-nav"
              onClick={() => setMenuOpen((open) => !open)}
            >
              ☰
            </button>
            <span className="topbar__title">{title}</span>
          </div>

          <div className="topbar__right">
            <div className="topbar__user">
              <div className="topbar__email">{me.user.email}</div>
              <div className="muted" style={{ fontSize: 12 }}>
                {/* The role is a label, not a control. Authorization comes
                    from the permission list the server sent. */}
                <span className="badge badge--muted">{me.user.role}</span>
              </div>
            </div>
            <button type="button" className="btn btn--small" onClick={onSignOut}>
              Sign out
            </button>
          </div>
        </header>

        <main id="main-content" className="page" tabIndex={-1}>
          {children}
        </main>
      </div>
    </div>
  )
}
