/**
 * Integrations.
 *
 * Two independent backends, presented as one page:
 *
 *   CRM       app/api/integration_routes.py    prefix /api/integrations/crm
 *   Calendar  app/api/appointment_routes.py    prefix /api/calendar
 *
 * Each exposes a provider catalogue and a list of this tenant's rows, so the
 * page renders the *catalogue* as the set of cards and merges the configured
 * row into it. A provider with no row is shown as "Not configured" rather
 * than hidden -- "what could I connect?" is the main question this page
 * answers, and the catalogue is real API data, not a hard-coded list.
 *
 * ## Secrets
 *
 * Neither response model can carry a credential: `IntegrationOut` and
 * `CalendarIntegrationOut` are allowlists, and `credentials_encrypted` is not
 * in either. `connected` is a boolean derived from whether ciphertext exists.
 *
 * That is the real defence. On top of it this page treats `config` -- a free
 * dict -- as untrusted and masks any key that looks credential-shaped before
 * rendering (see `maskConfig`). If a future backend change ever let a token
 * into `config`, it would be masked here rather than painted on screen.
 *
 * Credentials only ever travel *toward* the server: the connect form posts
 * them and clears itself, no field is ever populated from a response, and
 * nothing is written to storage.
 *
 * ## Actions
 *
 * Only what the routes actually implement. There is no OAuth redirect
 * endpoint anywhere in the backend, so there is no "Sign in with Google"
 * button -- Google and Microsoft are connected by pasting tokens, which is
 * what `PUT /calendar/integrations/{provider}` accepts. `integration:sync`
 * exists as a permission but no route consumes it, so there is no sync
 * button either.
 */
import { useCallback, useMemo, useState } from 'react'

import {
  Alert, AsyncSection, DataTable, Dialog, EmptyState, Field,
} from '../components/ui'
import {
  calendarProviders, crmProviders, deleteCalendarIntegration,
  deleteCrmIntegration, disconnectCrmIntegration, listCalendarIntegrations,
  listCrmIntegrations, listCrmSyncs, saveCalendarIntegration,
  saveCrmIntegration, testCalendarIntegration, testCrmIntegration,
} from '../lib/api'
import { formatDateTime, humanise } from '../lib/format'
import { useAction, useApi } from '../lib/hooks'
import { PERMISSIONS as P } from '../lib/permissions'

/**
 * Key names that must never be shown, whatever the backend sends.
 *
 * `config` is a free-form dict validated against a per-provider allowlist
 * server-side, so nothing here should ever match. It is a backstop against a
 * future allowlist edit, not a substitute for one.
 */
const SECRET_KEY = /(secret|token|password|passwd|credential|api[-_]?key|private|signature|auth)/i

function maskConfig(config) {
  return Object.entries(config ?? {}).map(([key, value]) => [
    key,
    SECRET_KEY.test(key) ? '••••••••' : String(value),
  ])
}

/** Friendlier names for the provider ids the API returns. */
const PROVIDER_LABELS = {
  gohighlevel: 'GoHighLevel',
  hubspot: 'HubSpot',
  jobber: 'Jobber',
  webhook: 'Generic Webhook',
  google: 'Google Calendar',
  google_service_account: 'Google (service account)',
  microsoft: 'Microsoft Outlook',
  calcom: 'Cal.com',
  internal: 'Internal Calendar',
}

const PROVIDER_ICONS = {
  gohighlevel: '◆', hubspot: '◈', jobber: '◇', webhook: '⇢',
  google: '📅', google_service_account: '🔑', microsoft: '📆',
  calcom: '🗓', internal: '🏠',
}

const providerLabel = (id) => PROVIDER_LABELS[id] ?? humanise(id)

export default function Integrations({ me, can }) {
  const timezone = me.tenant.timezone
  const mayWrite = can(P.INTEGRATION_WRITE)

  const [flash, setFlash] = useState(null)

  const crmCatalogue = useApi(() => crmProviders(), [])
  const crmList = useApi(() => listCrmIntegrations(), [])
  const calCatalogue = useApi(() => calendarProviders(), [])
  const calList = useApi(() => listCalendarIntegrations(), [])
  const syncs = useApi(() => listCrmSyncs({ limit: 20 }), [])

  const reloadCrm = useCallback(() => {
    crmList.reload()
    syncs.reload()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [crmList.reload, syncs.reload])

  return (
    <>
      <div className="page__header">
        <div>
          <h1>Integrations</h1>
          <p className="page__description">
            The external systems this tenant is connected to. Credentials are
            stored encrypted and are never shown here.
          </p>
        </div>
      </div>

      {flash && (
        <Alert tone={flash.tone} onDismiss={() => setFlash(null)}>
          {flash.message}
        </Alert>
      )}

      <IntegrationGroup
        title="CRM"
        description="Where calls, leads and appointments are written."
        catalogue={crmCatalogue}
        list={crmList}
        listKey="integrations"
        kind="crm"
        timezone={timezone}
        mayWrite={mayWrite}
        onFlash={setFlash}
        onChanged={reloadCrm}
      />

      <IntegrationGroup
        title="Calendar"
        description="Where the agent checks availability and books appointments."
        catalogue={calCatalogue}
        list={calList}
        listKey={null}
        kind="calendar"
        timezone={timezone}
        mayWrite={mayWrite}
        onFlash={setFlash}
        onChanged={calList.reload}
      />

      <SyncFeed syncs={syncs} timezone={timezone} />
    </>
  )
}

/* ----------------------------------------------------------------- group --- */

function IntegrationGroup({
  title, description, catalogue, list, listKey, kind, timezone, mayWrite,
  onFlash, onChanged,
}) {
  // The CRM list is `{integrations: [...]}`; the calendar list is a bare
  // array. Two teams, two shapes, one page.
  const configured = useMemo(() => {
    const rows = listKey ? list.data?.[listKey] : list.data
    return Array.isArray(rows) ? rows : []
  }, [list.data, listKey])

  const providers = catalogue.data?.providers ?? []
  const byProvider = useMemo(
    () => new Map(configured.map((row) => [row.provider, row])),
    [configured]
  )

  // `&& !data` matters: after a mutation the list reloads, and treating that
  // as "loading" would swap the cards for a skeleton, remount them and throw
  // away each card's local state -- including the health result the user just
  // asked for. Only the *first* load blanks the section.
  const loaded = Boolean(catalogue.data) && Boolean(list.data)
  const loading = (catalogue.loading || list.loading) && !loaded
  const error = catalogue.error || list.error

  return (
    <section className="card" aria-label={`${title} integrations`}>
      <div className="card__header">
        <div>
          <h3>{title}</h3>
          <p className="muted" style={{ margin: '2px 0 0', fontSize: 13 }}>
            {description}
          </p>
        </div>
      </div>
      <div className="card__body">
        <AsyncSection
          loading={loading}
          error={error}
          onRetry={() => { catalogue.reload(); list.reload() }}
          resource={`${title.toLowerCase()} integrations`}
          isEmpty={Boolean(catalogue.data) && providers.length === 0}
          empty={
            <EmptyState
              icon="⇄"
              title={`No ${title} providers available`}
              description="This deployment has no adapters registered for this integration type."
            />
          }
        >
          {catalogue.data && providers.length > 0 && (
            <div className="grid grid--halves">
              {providers.map((provider) => (
                <ProviderCard
                  key={provider.provider}
                  catalogue={provider}
                  integration={byProvider.get(provider.provider) ?? null}
                  kind={kind}
                  timezone={timezone}
                  mayWrite={mayWrite}
                  onFlash={onFlash}
                  onChanged={onChanged}
                />
              ))}
            </div>
          )}
        </AsyncSection>
      </div>
    </section>
  )
}

/* ------------------------------------------------------------------ card --- */

/**
 * Three states, and they are genuinely different things:
 *
 *   not configured  no row at all
 *   configured      a row exists but holds no credentials (disconnected, or
 *                   a provider like `internal` that needs none)
 *   connected       credentials are stored -- which is NOT the same as
 *                   "they work". `connected` means ciphertext exists; only
 *                   `last_health_ok` says whether the provider accepted it.
 */
function ProviderCard({
  catalogue, integration, kind, timezone, mayWrite, onFlash, onChanged,
}) {
  const id = catalogue.provider
  const [editing, setEditing] = useState(false)
  const [confirming, setConfirming] = useState(null)
  const [health, setHealth] = useState(null)

  const exists = Boolean(integration)
  const connected = Boolean(integration?.connected)
  const needsCredentials = catalogue.credential_fields.length > 0

  const test = useAction(
    () => (kind === 'crm' ? testCrmIntegration(id) : testCalendarIntegration(id)),
    {
      onSuccess: (result) => {
        // A failed connection is a successful diagnosis: the backend returns
        // 200 with connected=false. Report it as information, not an error.
        setHealth(result)
        onChanged()
      },
    }
  )

  const toggle = useAction(
    (enable) => (kind === 'crm'
      // PUT is a full replace, so echo back every non-secret field the read
      // model gave us. `credentials` is omitted, which the backend documents
      // as "leave the stored credentials untouched".
      ? saveCrmIntegration(id, {
        is_enabled: enable,
        config: integration.config,
        field_mappings: integration.field_mappings,
        subscribed_events: integration.subscribed_events,
        share_transcripts: integration.share_transcripts,
      })
      : saveCalendarIntegration(id, {
        is_enabled: enable,
        is_primary: integration.is_primary,
        config: integration.config,
      })),
    {
      onSuccess: (result) => {
        onFlash({
          tone: 'ok',
          message: `${providerLabel(id)} ${result.is_enabled ? 'enabled' : 'disabled'}.`,
        })
        onChanged()
      },
    }
  )

  const remove = useAction(
    (mode) => {
      if (mode === 'disconnect') return disconnectCrmIntegration(id)
      return kind === 'crm'
        ? deleteCrmIntegration(id)
        : deleteCalendarIntegration(id)
    },
    {
      onSuccess: () => {
        setConfirming(null)
        setHealth(null)
        onFlash({
          tone: 'ok',
          message: `${providerLabel(id)} ${
            confirming === 'disconnect'
              ? 'disconnected. Its configuration was kept.'
              : 'removed. Sync history was kept.'
          }`,
        })
        onChanged()
      },
    }
  )

  const actionError = test.error || toggle.error || remove.error
  const busy = test.pending || toggle.pending || remove.pending

  return (
    <div className="card" style={{ display: 'flex', flexDirection: 'column' }}>
      <div className="card__header">
        <div className="row" style={{ gap: 10, alignItems: 'center' }}>
          <span aria-hidden="true" style={{ fontSize: 20 }}>
            {PROVIDER_ICONS[id] ?? '⇄'}
          </span>
          <div>
            <h4 style={{ margin: 0 }}>{providerLabel(id)}</h4>
            <div className="muted" style={{ fontSize: 12 }}>{id}</div>
          </div>
        </div>
        <ConnectionBadge integration={integration} />
      </div>

      <div className="card__body" style={{ flex: 1 }}>
        {!exists ? (
          <p className="muted" style={{ marginTop: 0 }}>
            Not configured for this tenant.
          </p>
        ) : (
          <dl className="kv">
            <dt>Enabled</dt>
            <dd>{integration.is_enabled ? 'Yes' : 'No'}</dd>

            {kind === 'calendar' && (
              <>
                <dt>Primary</dt>
                <dd>{integration.is_primary ? 'Yes' : 'No'}</dd>
              </>
            )}

            <dt>Credentials</dt>
            <dd>
              {connected
                ? 'Stored, encrypted'
                : needsCredentials
                  ? 'None stored'
                  : 'Not required'}
            </dd>

            {integration.connected_at && (
              <>
                <dt>Connected on</dt>
                <dd>{formatDateTime(integration.connected_at, timezone)}</dd>
              </>
            )}

            {kind === 'calendar' && integration.token_expires_at && (
              <>
                <dt>Token expires</dt>
                <dd>{formatDateTime(integration.token_expires_at, timezone)}</dd>
              </>
            )}

            <dt>Last checked</dt>
            <dd>
              {integration.last_health_check_at
                ? formatDateTime(integration.last_health_check_at, timezone)
                : 'Never'}
            </dd>

            {maskConfig(integration.config).map(([key, value]) => (
              <Fragmented key={key} label={humanise(key)} value={value} />
            ))}
          </dl>
        )}

        {integration?.last_error && (
          <Alert tone="warn">
            {/* Scrubbed server-side by `errors.safe_message`; rendered as
                text regardless. */}
            {integration.last_error}
          </Alert>
        )}

        <Capabilities items={catalogue.capabilities} />

        {health && (
          <Alert tone={health.connected ? 'ok' : 'warn'}>
            {health.safe_message}
            {typeof health.latency_ms === 'number'
              && ` (${Math.round(health.latency_ms)} ms)`}
          </Alert>
        )}

        {actionError && (
          <Alert
            tone="error"
            onDismiss={() => {
              test.clearError(); toggle.clearError(); remove.clearError()
            }}
          >
            {actionError.message}
          </Alert>
        )}

        {editing && (
          <ConnectForm
            catalogue={catalogue}
            integration={integration}
            kind={kind}
            onCancel={() => setEditing(false)}
            onSaved={() => {
              setEditing(false)
              onFlash({ tone: 'ok', message: `${providerLabel(id)} saved.` })
              onChanged()
            }}
          />
        )}
      </div>

      <div className="card__body" style={{ paddingTop: 0 }}>
        <div className="toolbar">
          {/* Testing needs only integration:read -- the backend gates it that
              way, because a diagnosis changes nothing. */}
          {exists && (
            <button
              type="button" className="btn btn--small"
              disabled={busy} onClick={() => test.run()}
            >
              {test.pending ? 'Testing…' : 'Test connection'}
            </button>
          )}

          {mayWrite && !editing && (
            <button
              type="button" className="btn btn--small"
              disabled={busy} onClick={() => setEditing(true)}
            >
              {exists ? 'Configure' : 'Connect'}
            </button>
          )}

          {mayWrite && exists && (
            <button
              type="button" className="btn btn--small"
              disabled={busy}
              onClick={() => toggle.run(!integration.is_enabled)}
            >
              {integration.is_enabled ? 'Disable' : 'Enable'}
            </button>
          )}

          {/* Disconnect keeps the config and drops the credentials. It only
              exists for CRM; the calendar API has no equivalent, so offering
              one there would be a dead button. */}
          {mayWrite && kind === 'crm' && connected && (
            <button
              type="button" className="btn btn--small"
              disabled={busy} onClick={() => setConfirming('disconnect')}
            >
              Disconnect
            </button>
          )}

          {mayWrite && exists && (
            <button
              type="button" className="btn btn--small btn--danger"
              disabled={busy} onClick={() => setConfirming('delete')}
            >
              Remove
            </button>
          )}
        </div>
      </div>

      <Dialog
        open={Boolean(confirming)}
        title={
          confirming === 'disconnect'
            ? `Disconnect ${providerLabel(id)}?`
            : `Remove ${providerLabel(id)}?`
        }
        onClose={() => setConfirming(null)}
        footer={
          <>
            <button
              type="button" className="btn"
              onClick={() => setConfirming(null)} disabled={remove.pending}
            >
              Cancel
            </button>
            <button
              type="button"
              className={confirming === 'delete' ? 'btn btn--danger' : 'btn btn--primary'}
              onClick={() => remove.run(confirming)}
              disabled={remove.pending}
            >
              {remove.pending
                ? 'Working…'
                : confirming === 'delete' ? 'Remove' : 'Disconnect'}
            </button>
          </>
        }
      >
        <p style={{ margin: 0 }}>
          {confirming === 'disconnect' ? (
            <>
              The stored credentials will be deleted and the integration
              disabled. Your configuration and field mappings are kept, so you
              can reconnect by pasting a new token.
            </>
          ) : (
            <>
              The integration and its stored credentials will be deleted.
              Sync history is kept as a record of what was already sent.
            </>
          )}
        </p>
      </Dialog>
    </div>
  )
}

/** A `<dt>/<dd>` pair. Named oddly because it returns a fragment, not a row. */
function Fragmented({ label, value }) {
  return (
    <>
      <dt>{label}</dt>
      <dd>{value}</dd>
    </>
  )
}

function ConnectionBadge({ integration }) {
  if (!integration) {
    return <span className="badge badge--muted">Not configured</span>
  }
  if (!integration.connected) {
    return <span className="badge badge--muted">Disconnected</span>
  }
  // `connected` only means credentials exist. Health is a separate question,
  // and conflating them would show a green badge for a revoked token.
  if (integration.last_health_ok === true) {
    return <span className="badge badge--ok">Healthy</span>
  }
  if (integration.last_health_ok === false) {
    return <span className="badge badge--danger">Failing</span>
  }
  return <span className="badge badge--info">Connected</span>
}

function Capabilities({ items }) {
  if (!items?.length) return null
  return (
    <details style={{ marginTop: 10 }}>
      <summary style={{ cursor: 'pointer', fontSize: 13 }}>
        {items.length} supported operations
      </summary>
      <div className="row" style={{ gap: 6, flexWrap: 'wrap', marginTop: 8 }}>
        {items.map((item) => (
          <span key={item} className="badge badge--muted">{humanise(item)}</span>
        ))}
      </div>
    </details>
  )
}

/* ------------------------------------------------------------------ form --- */

/**
 * Connect or reconfigure.
 *
 * The fields come from the provider catalogue (`credential_fields` and
 * `config_fields`), so this form is generated from real API metadata rather
 * than a hard-coded per-provider layout.
 *
 * Credential inputs are write-only and always start empty -- including when
 * editing an existing connection, because the API cannot return the stored
 * value and pretending otherwise with a fake row of dots would be a lie about
 * what is submitted. Leaving them blank on an update tells the backend to
 * keep what it has.
 */
function ConnectForm({ catalogue, integration, kind, onCancel, onSaved }) {
  const id = catalogue.provider
  const [credentials, setCredentials] = useState({})
  const [config, setConfig] = useState(() => {
    const initial = {}
    for (const field of catalogue.config_fields) {
      initial[field] = integration?.config?.[field] ?? ''
    }
    return initial
  })

  const save = useAction(
    () => {
      const supplied = Object.fromEntries(
        Object.entries(credentials).filter(([, value]) => value.trim() !== '')
      )
      const cleanedConfig = Object.fromEntries(
        Object.entries(config).filter(([, value]) => String(value).trim() !== '')
      )
      const body = kind === 'crm'
        ? {
          is_enabled: integration?.is_enabled ?? true,
          config: cleanedConfig,
          field_mappings: integration?.field_mappings ?? {},
          subscribed_events: integration?.subscribed_events ?? [],
          share_transcripts: integration?.share_transcripts ?? false,
        }
        : {
          is_enabled: integration?.is_enabled ?? true,
          is_primary: integration?.is_primary ?? false,
          config: cleanedConfig,
        }
      // Omit the key entirely when nothing was typed: `null` and `{}` mean
      // different things to the backend, and only omission preserves.
      if (Object.keys(supplied).length > 0) body.credentials = supplied
      return kind === 'crm'
        ? saveCrmIntegration(id, body)
        : saveCalendarIntegration(id, body)
    },
    {
      onSuccess: () => {
        // Drop the typed secrets from component state immediately.
        setCredentials({})
        onSaved()
      },
    }
  )

  return (
    <form
      className="stack"
      style={{ marginTop: 12 }}
      onSubmit={(event) => { event.preventDefault(); save.run() }}
    >
      <h5 style={{ margin: 0 }}>
        {integration ? 'Update configuration' : `Connect ${providerLabel(id)}`}
      </h5>

      {catalogue.credential_fields.map((field) => (
        <Field
          key={field}
          label={humanise(field)}
          htmlFor={`${id}-${field}`}
          hint={
            integration?.connected
              ? 'Leave blank to keep the stored value.'
              : 'Stored encrypted. It cannot be read back afterwards.'
          }
        >
          <input
            id={`${id}-${field}`}
            className="input"
            // `password` so it is not shoulder-surfed or captured by a
            // screenshot tool, and `off` so the browser never persists it.
            type="password"
            autoComplete="off"
            value={credentials[field] ?? ''}
            disabled={save.pending}
            onChange={(event) => setCredentials((previous) => ({
              ...previous, [field]: event.target.value,
            }))}
          />
        </Field>
      ))}

      {catalogue.credential_fields.length === 0 && (
        <p className="muted" style={{ margin: 0, fontSize: 13 }}>
          This provider needs no credentials.
        </p>
      )}

      {catalogue.config_fields.map((field) => (
        <Field key={field} label={humanise(field)} htmlFor={`${id}-cfg-${field}`}>
          <input
            id={`${id}-cfg-${field}`}
            className="input"
            type="text"
            value={config[field] ?? ''}
            disabled={save.pending}
            onChange={(event) => setConfig((previous) => ({
              ...previous, [field]: event.target.value,
            }))}
          />
        </Field>
      ))}

      {save.error && (
        <Alert tone="error" onDismiss={save.clearError}>
          {save.error.message}
        </Alert>
      )}

      <div className="toolbar">
        <button type="submit" className="btn btn--primary" disabled={save.pending}>
          {save.pending ? 'Saving…' : 'Save'}
        </button>
        <button
          type="button" className="btn"
          onClick={onCancel} disabled={save.pending}
        >
          Cancel
        </button>
      </div>
    </form>
  )
}

/* ------------------------------------------------------------------ sync --- */

const SYNC_TONE = {
  synced: 'ok', pending: 'info', processing: 'info',
  failed: 'warn', permanent_failure: 'danger',
}

function SyncFeed({ syncs, timezone }) {
  const rows = syncs.data?.syncs ?? []

  const columns = useMemo(() => [
    {
      key: 'provider', header: 'Provider',
      render: (row) => providerLabel(row.provider),
    },
    {
      key: 'event', header: 'Event',
      render: (row) => row.event_type ?? '—',
    },
    {
      key: 'entity', header: 'Record',
      render: (row) => humanise(row.entity_type),
    },
    {
      key: 'status', header: 'Status',
      render: (row) => (
        <span className={`badge badge--${SYNC_TONE[row.status] ?? 'muted'}`}>
          {humanise(row.status)}
        </span>
      ),
    },
    {
      key: 'attempts', header: 'Attempts', numeric: true,
      render: (row) => row.attempt_count,
    },
    {
      key: 'when', header: 'Last attempt',
      render: (row) => (row.last_attempt_at
        ? formatDateTime(row.last_attempt_at, timezone)
        : '—'),
    },
    {
      key: 'error', header: 'Detail',
      render: (row) => (row.last_error
        ? <span className="muted">{row.last_error}</span>
        : '—'),
    },
  ], [timezone])

  return (
    <section className="card" aria-label="Recent CRM syncs">
      <div className="card__header">
        <div>
          <h3>Recent CRM activity</h3>
          <p className="muted" style={{ margin: '2px 0 0', fontSize: 13 }}>
            The last 20 records this tenant sent to a CRM.
          </p>
        </div>
      </div>
      <div className="card__body">
        <AsyncSection
          loading={syncs.loading}
          error={syncs.error}
          onRetry={syncs.reload}
          resource="sync activity"
          isEmpty={Boolean(syncs.data) && rows.length === 0}
          empty={
            <EmptyState
              icon="⇄"
              title="No sync activity yet"
              description="Records appear here once a call or lead is sent to a connected CRM."
            />
          }
        >
          {rows.length > 0 && (
            <DataTable
              caption="Recent CRM syncs"
              columns={columns}
              rows={rows}
              keyOf={(row) => row.id}
            />
          )}
        </AsyncSection>
      </div>
    </section>
  )
}
