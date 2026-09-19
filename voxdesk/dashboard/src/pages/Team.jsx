/**
 * Team administration.
 *
 * Backed by `app/api/team_routes.py` and `app/auth/rbac.py`:
 *
 *   GET   /api/team/users              the tenant's users
 *   POST  /api/team/users              create (201)
 *   PATCH /api/team/users/{id}/role    change role
 *   PATCH /api/team/users/{id}/active  activate / deactivate
 *   GET   /auth/roles                  the real RBAC policy
 *
 * The role picker and the permission previews are built from `/auth/roles`,
 * which serves `describe_roles()` -- the actual policy object, including each
 * role's numeric level and full permission list. Nothing about roles is
 * hard-coded here, so an RBAC change shows up in this UI without a frontend
 * edit.
 *
 * ## Mirroring the server's rules
 *
 * `app/auth/service.py` refuses several things, and this page declines to
 * offer them rather than letting a user click into a 403:
 *
 *   - **you cannot change your own role** and **cannot deactivate yourself**;
 *   - you cannot touch a user at or above your level -- except an owner, who
 *     may manage other owners (`can_manage_user`);
 *   - you may only grant a role *strictly below your own* (`can_assign_role`),
 *     so **nobody can create or promote to owner**, not even an owner;
 *   - the last active owner can be neither demoted nor deactivated.
 *
 * The last-owner rule is the one case the client cannot always predict, so it
 * is not pre-emptively blocked -- the server's 409 is surfaced instead.
 *
 * ## Deactivation, not deletion
 *
 * There is no delete route. `user:delete` exists as a permission but no
 * endpoint consumes it. Deactivation revokes refresh tokens and bumps
 * `token_version`, which ends the session immediately, while keeping the row
 * so transcripts and audit entries retain a valid author.
 *
 * ## Passwords
 *
 * `UserCreateIn` requires a password: there is no invitation email anywhere
 * in the backend. The form therefore sets an initial password and says so
 * plainly rather than claiming an invite was sent. The value is held in
 * component state only, submitted once, and cleared -- never echoed back,
 * never stored, never logged.
 */
import { useMemo, useState } from 'react'

import {
  Alert, AsyncSection, DataTable, Dialog, EmptyState, Field, StatCard,
} from '../components/ui'
import { createUser, getRoles, listUsers, setUserActive, setUserRole } from '../lib/api'
import { formatDateTime, formatNumber, humanise } from '../lib/format'
import { useAction, useApi } from '../lib/hooks'
import { PERMISSIONS as P } from '../lib/permissions'

/** Tones for the real `UserRole` enum, ordered by privilege. */
const ROLE_TONE = {
  owner: 'danger', admin: 'warn', manager: 'info', agent: 'muted',
  viewer: 'muted',
}

export default function Team({ me, can }) {
  const timezone = me.tenant.timezone
  const mayCreate = can(P.USER_CREATE)
  const mayChangeRole = can(P.USER_ROLE_CHANGE)
  const mayUpdate = can(P.USER_UPDATE)

  const [flash, setFlash] = useState(null)
  const [dialog, setDialog] = useState(null)
  const [search, setSearch] = useState('')
  const [showInactive, setShowInactive] = useState(true)

  const users = useApi(() => listUsers(), [])
  const roles = useApi(() => getRoles(), [])

  const policy = roles.data?.roles ?? []
  const myLevel = policy.find((r) => r.role === me.user.role)?.level ?? 0

  /**
   * Roles this actor may grant: strictly below their own level, exactly as
   * `can_assign_role` computes it. For an owner that excludes `owner`.
   */
  const assignable = useMemo(
    () => (mayChangeRole ? policy.filter((r) => r.level < myLevel) : []),
    [policy, myLevel, mayChangeRole]
  )

  /** `can_manage_user`: an owner may manage anyone; others must outrank. */
  const canManage = useMemo(() => (target) => {
    if (!mayUpdate) return false
    if (me.user.role === 'owner') return true
    const targetLevel = policy.find((r) => r.role === target.role)?.level ?? 0
    return myLevel > targetLevel
  }, [mayUpdate, me.user.role, policy, myLevel])

  const all = users.data ?? []
  // Filtering is client-side because the endpoint has no search or paging
  // parameter -- inventing `?q=` would 422. The list is one tenant's staff,
  // so it is small and bounded by design.
  const visible = useMemo(() => {
    const needle = search.trim().toLowerCase()
    return all.filter((user) => {
      if (!showInactive && !user.is_active) return false
      if (!needle) return true
      return `${user.full_name} ${user.email} ${user.role}`
        .toLowerCase().includes(needle)
    })
  }, [all, search, showInactive])

  const active = all.filter((u) => u.is_active)
  const owners = active.filter((u) => u.role === 'owner')

  const columns = useMemo(() => [
    {
      key: 'person', header: 'Member',
      render: (user) => (
        <div>
          {/* Tenant-supplied. React escapes it. */}
          <div style={{ fontWeight: 550 }}>
            {user.full_name || <span className="muted">No name set</span>}
            {user.id === me.user.id && (
              <span className="badge badge--muted" style={{ marginLeft: 8 }}>
                You
              </span>
            )}
          </div>
          <div className="muted" style={{ fontSize: 12 }}>{user.email}</div>
        </div>
      ),
    },
    {
      key: 'role', header: 'Role',
      render: (user) => (
        <span className={`badge badge--${ROLE_TONE[user.role] ?? 'muted'}`}>
          {humanise(user.role)}
        </span>
      ),
    },
    {
      key: 'status', header: 'Status',
      render: (user) => (user.is_active
        ? <span className="badge badge--ok">Active</span>
        : <span className="badge badge--muted">Deactivated</span>),
    },
    {
      key: 'created', header: 'Added',
      render: (user) => formatDateTime(user.created_at, timezone),
    },
    {
      key: 'seen', header: 'Last sign-in',
      render: (user) => (user.last_login_at
        ? formatDateTime(user.last_login_at, timezone)
        : <span className="muted">Never</span>),
    },
    {
      key: 'actions', header: 'Actions',
      render: (user) => (
        <RowActions
          user={user}
          isSelf={user.id === me.user.id}
          manageable={canManage(user)}
          assignable={assignable}
          onRole={() => setDialog({ kind: 'role', user })}
          onActive={() => setDialog({ kind: 'active', user })}
        />
      ),
    },
  ], [timezone, me.user.id, canManage, assignable])

  return (
    <>
      <div className="page__header">
        <div>
          <h1>Team</h1>
          <p className="page__description">
            Who can sign in to this workspace, and what each of them may do.
          </p>
        </div>
        {mayCreate && (
          <button
            type="button" className="btn btn--primary"
            onClick={() => setDialog({ kind: 'create' })}
          >
            Add member
          </button>
        )}
      </div>

      {flash && (
        <Alert tone={flash.tone} onDismiss={() => setFlash(null)}>
          {flash.message}
        </Alert>
      )}

      <div className="grid grid--stats">
        <StatCard label="Members" value={formatNumber(all.length)} />
        <StatCard label="Active" value={formatNumber(active.length)} />
        <StatCard
          label="Deactivated"
          value={formatNumber(all.length - active.length)}
        />
        <StatCard
          label="Owners"
          value={formatNumber(owners.length)}
          hint="A tenant must always keep one"
        />
      </div>

      <section className="card" aria-label="Team members">
        <div className="card__header">
          <h3>Members</h3>
          <div className="row" style={{ gap: 12 }}>
            <label className="sr-only" htmlFor="team-search">
              Search members
            </label>
            <input
              id="team-search"
              className="input"
              type="search"
              placeholder="Search name, email or role"
              style={{ width: 'auto' }}
              value={search}
              onChange={(event) => setSearch(event.target.value)}
            />
            <label className="row" style={{ gap: 6, fontSize: 13 }}>
              <input
                type="checkbox"
                checked={showInactive}
                onChange={(event) => setShowInactive(event.target.checked)}
              />
              Show deactivated
            </label>
          </div>
        </div>
        <div className="card__body">
          <AsyncSection
            loading={users.loading}
            error={users.error}
            onRetry={users.reload}
            resource="the team"
            isEmpty={Boolean(users.data) && all.length === 0}
            empty={
              <EmptyState
                icon="☰"
                title="No team members yet"
                description="Add someone to give them access to this workspace."
              />
            }
          >
            {all.length > 0 && (
              visible.length > 0 ? (
                <DataTable
                  caption="Team members"
                  columns={columns}
                  rows={visible}
                  keyOf={(user) => user.id}
                />
              ) : (
                <EmptyState
                  icon="☰"
                  title="No members match"
                  description="Try a different search, or show deactivated members."
                />
              )
            )}
          </AsyncSection>
        </div>
      </section>

      <RolePolicy roles={roles} />

      <TeamDialog
        dialog={dialog}
        assignable={assignable}
        onClose={() => setDialog(null)}
        onDone={(message) => {
          setDialog(null)
          setFlash(message)
          users.reload()
        }}
      />
    </>
  )
}

/* -------------------------------------------------------------- actions --- */

function RowActions({ user, isSelf, manageable, assignable, onRole, onActive }) {
  // The server refuses both of these on yourself, so neither is offered.
  if (isSelf) {
    return (
      <span className="muted" title="You cannot change your own role or status">
        —
      </span>
    )
  }
  if (!manageable) {
    return (
      <span className="muted" title="This member is at or above your level">
        —
      </span>
    )
  }

  return (
    <div className="row" style={{ gap: 6, flexWrap: 'wrap' }}>
      {assignable.length > 0 && (
        <button type="button" className="btn btn--small" onClick={onRole}>
          Change role
        </button>
      )}
      <button
        type="button"
        className={`btn btn--small${user.is_active ? ' btn--danger' : ''}`}
        onClick={onActive}
      >
        {user.is_active ? 'Deactivate' : 'Reactivate'}
      </button>
    </div>
  )
}

/* --------------------------------------------------------------- policy --- */

/** The real RBAC policy, so "what does manager mean?" has an answer. */
function RolePolicy({ roles }) {
  const list = roles.data?.roles ?? []

  return (
    <section className="card" aria-label="What each role can do">
      <div className="card__header">
        <div>
          <h3>What each role can do</h3>
          <p className="muted" style={{ margin: '2px 0 0', fontSize: 13 }}>
            Straight from the server&apos;s permission policy.
          </p>
        </div>
      </div>
      <div className="card__body">
        <AsyncSection
          loading={roles.loading}
          error={roles.error}
          onRetry={roles.reload}
          resource="the role policy"
        >
          {list.length > 0 && (
            <div className="stack">
              {list.map((role) => (
                <details key={role.role}>
                  <summary style={{ cursor: 'pointer' }}>
                    <span className={`badge badge--${ROLE_TONE[role.role] ?? 'muted'}`}>
                      {humanise(role.role)}
                    </span>
                    <span className="muted" style={{ fontSize: 13 }}>
                      {' '}{role.permissions.length} permissions
                    </span>
                  </summary>
                  <div className="row" style={{ gap: 6, flexWrap: 'wrap', marginTop: 8 }}>
                    {role.permissions.map((permission) => (
                      <span key={permission} className="badge badge--muted">
                        {permission}
                      </span>
                    ))}
                  </div>
                </details>
              ))}
            </div>
          )}
        </AsyncSection>
      </div>
    </section>
  )
}

/* --------------------------------------------------------------- dialog --- */

function TeamDialog({ dialog, assignable, onClose, onDone }) {
  if (!dialog) return null
  if (dialog.kind === 'create') {
    return <CreateDialog assignable={assignable} onClose={onClose} onDone={onDone} />
  }
  if (dialog.kind === 'role') {
    return (
      <RoleDialog
        user={dialog.user} assignable={assignable}
        onClose={onClose} onDone={onDone}
      />
    )
  }
  return <ActiveDialog user={dialog.user} onClose={onClose} onDone={onDone} />
}

function CreateDialog({ assignable, onClose, onDone }) {
  const [form, setForm] = useState({
    email: '', full_name: '', password: '',
    role: assignable[assignable.length - 1]?.role ?? 'viewer',
  })
  const set = (key) => (event) =>
    setForm((previous) => ({ ...previous, [key]: event.target.value }))

  const create = useAction(() => createUser(form), {
    onSuccess: (user) => {
      // Wipe the password from state the moment it is no longer needed.
      setForm((previous) => ({ ...previous, password: '' }))
      onDone({
        tone: 'ok',
        // No email is sent by the backend, so do not imply one was.
        message: `${user.email} can now sign in as ${humanise(user.role)}. Share the password with them directly -- no invitation email is sent.`,
      })
    },
  })

  return (
    <Dialog
      open
      title="Add a team member"
      onClose={onClose}
      footer={
        <>
          <button type="button" className="btn" onClick={onClose} disabled={create.pending}>
            Cancel
          </button>
          <button
            type="submit" form="create-member" className="btn btn--primary"
            disabled={create.pending}
          >
            {create.pending ? 'Creating…' : 'Create member'}
          </button>
        </>
      }
    >
      <form
        id="create-member"
        className="stack"
        onSubmit={(event) => { event.preventDefault(); create.run() }}
      >
        <Alert tone="info">
          This creates the account immediately with a password you set. The
          system does not send an invitation email, so you will need to pass
          the password to them yourself and ask them to change it.
        </Alert>

        <Field label="Email" htmlFor="new-email">
          <input
            id="new-email" className="input" type="email" required
            autoComplete="off" value={form.email}
            disabled={create.pending} onChange={set('email')}
          />
        </Field>

        <Field label="Full name" htmlFor="new-name">
          <input
            id="new-name" className="input" type="text"
            autoComplete="off" value={form.full_name}
            disabled={create.pending} onChange={set('full_name')}
          />
        </Field>

        <Field
          label="Initial password"
          htmlFor="new-password"
          hint="At least 12 characters, mixing three of: lowercase, uppercase, digits, symbols."
        >
          <input
            id="new-password" className="input" type="password" required
            minLength={12}
            // Never let a password manager capture a credential an admin is
            // typing on someone else's behalf.
            autoComplete="new-password"
            value={form.password}
            disabled={create.pending} onChange={set('password')}
          />
        </Field>

        <Field label="Role" htmlFor="new-role">
          <select
            id="new-role" className="select" value={form.role}
            disabled={create.pending} onChange={set('role')}
          >
            {assignable.map((role) => (
              <option key={role.role} value={role.role}>
                {humanise(role.role)}
              </option>
            ))}
          </select>
        </Field>

        {/* Not a bug worth hiding: the server refuses to grant a role at or
            above the actor's own, so an owner cannot mint another owner. */}
        <p className="muted" style={{ fontSize: 12, margin: 0 }}>
          You can only grant roles below your own.
        </p>

        {create.error && (
          <Alert tone="error" onDismiss={create.clearError}>
            {create.error.message}
          </Alert>
        )}
      </form>
    </Dialog>
  )
}

function RoleDialog({ user, assignable, onClose, onDone }) {
  const [role, setRole] = useState(user.role)
  const change = useAction(() => setUserRole(user.id, role), {
    onSuccess: (updated) => onDone({
      tone: 'ok',
      message: `${updated.email} is now ${humanise(updated.role)}.`,
    }),
  })

  const chosen = assignable.find((r) => r.role === role)

  return (
    <Dialog
      open
      title={`Change role for ${user.full_name || user.email}`}
      onClose={onClose}
      footer={
        <>
          <button type="button" className="btn" onClick={onClose} disabled={change.pending}>
            Cancel
          </button>
          <button
            type="button" className="btn btn--primary"
            disabled={change.pending || role === user.role}
            onClick={() => change.run()}
          >
            {change.pending ? 'Saving…' : 'Change role'}
          </button>
        </>
      }
    >
      <div className="stack">
        <dl className="kv">
          <dt>Current role</dt>
          <dd>{humanise(user.role)}</dd>
        </dl>

        <Field label="New role" htmlFor="change-role">
          <select
            id="change-role" className="select" value={role}
            disabled={change.pending}
            onChange={(event) => setRole(event.target.value)}
          >
            {/* The current role is listed so the select is never empty, even
                when it is not itself assignable (e.g. another owner). */}
            {!assignable.some((r) => r.role === user.role) && (
              <option value={user.role}>{humanise(user.role)} (current)</option>
            )}
            {assignable.map((entry) => (
              <option key={entry.role} value={entry.role}>
                {humanise(entry.role)}
              </option>
            ))}
          </select>
        </Field>

        {chosen && (
          <p className="muted" style={{ margin: 0, fontSize: 13 }}>
            {humanise(chosen.role)} has {chosen.permissions.length} permissions.
          </p>
        )}

        <Alert tone="warn">
          Changing a role signs this person out of every device immediately.
        </Alert>

        {change.error && (
          <Alert tone="error" onDismiss={change.clearError}>
            {change.error.message}
          </Alert>
        )}
      </div>
    </Dialog>
  )
}

function ActiveDialog({ user, onClose, onDone }) {
  const deactivating = user.is_active
  const act = useAction(() => setUserActive(user.id, !user.is_active), {
    onSuccess: (updated) => onDone({
      tone: 'ok',
      message: updated.is_active
        ? `${updated.email} can sign in again.`
        : `${updated.email} has been deactivated and signed out.`,
    }),
  })

  return (
    <Dialog
      open
      title={deactivating
        ? `Deactivate ${user.full_name || user.email}?`
        : `Reactivate ${user.full_name || user.email}?`}
      onClose={onClose}
      footer={
        <>
          <button type="button" className="btn" onClick={onClose} disabled={act.pending}>
            Cancel
          </button>
          <button
            type="button"
            className={deactivating ? 'btn btn--danger' : 'btn btn--primary'}
            disabled={act.pending}
            onClick={() => act.run()}
          >
            {act.pending ? 'Working…' : deactivating ? 'Deactivate' : 'Reactivate'}
          </button>
        </>
      }
    >
      <div className="stack">
        <p style={{ margin: 0 }}>
          {deactivating ? (
            <>
              They will be signed out of every device immediately and will not
              be able to sign in again. Their account is kept, so past calls
              and notes still show who handled them, and you can reactivate
              them at any time. There is no way to delete a member.
            </>
          ) : (
            <>
              They will be able to sign in again with their existing password
              and their previous role.
            </>
          )}
        </p>

        {act.error && (
          <Alert tone="error" onDismiss={act.clearError}>
            {act.error.message}
          </Alert>
        )}
      </div>
    </Dialog>
  )
}