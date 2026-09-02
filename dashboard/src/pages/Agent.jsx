/**
 * Agent configuration.
 *
 * The editable surface is deliberately narrow, and the reason is the same one
 * `Leads.jsx` gives: the backend supports exactly six writable fields
 * (`VoiceSettings` -> `PATCH /api/tenants/{id}/voice`), so those six get real
 * controls and everything else is rendered as read-only fact. A form that
 * silently discards what you typed is worse than no form, and inventing a
 * write path the API does not have would do exactly that.
 *
 * Everything on this page comes from `GET /api/tenants/{id}/agent`. Nothing is
 * computed, defaulted or invented in the browser -- when a value is empty the
 * page says it is not set rather than showing a plausible-looking default,
 * because "Alex" appearing in a field the tenant never filled in is how the
 * old dashboard misled people.
 *
 * **No credential is rendered here because none is served.** The response
 * model omits every credential-shaped column on `Tenant`; the CRM key, the
 * A2P SIDs and the Twilio auth token are not in the payload at all, so there
 * is nothing to redact.
 */
import { useEffect, useMemo, useState } from 'react'

import {
  Alert, AsyncSection, EmptyState, Field, StatusBadge,
} from '../components/ui'
import { getAgentConfig, listPresets, updateVoice } from '../lib/api'
import { formatTime, humanise } from '../lib/format'
import { useAction, useApi } from '../lib/hooks'
import { PERMISSIONS as P } from '../lib/permissions'

/** The six fields `VoiceSettings` accepts. Nothing else may be sent. */
const WRITABLE = [
  'llm_preset', 'temperature', 'voice_id', 'humanize', 'vad_stop_secs',
  'speech_speed',
]

/** Pull just the writable subset out of a config response. */
function draftFrom(config) {
  return {
    llm_preset: config.llm_preset ?? '',
    temperature: config.temperature,
    voice_id: config.voice_id ?? '',
    humanize: config.humanize,
    vad_stop_secs: config.vad_stop_secs,
    speech_speed: config.speech_speed,
  }
}

/**
 * Only what actually changed, so a save cannot clobber a field the operator
 * never touched. `VoiceSettings` excludes `None`, so omitting a key leaves it
 * alone server-side.
 */
function changedFields(draft, config) {
  const body = {}
  for (const key of WRITABLE) {
    const next = draft[key]
    // `voice_id` is nullable server-side but an empty string in a text input;
    // normalise both sides before comparing so an untouched empty field is
    // not reported as a change.
    const current = typeof next === 'string' ? (config[key] ?? '') : config[key]
    if (next !== current) body[key] = next
  }
  return body
}

export default function Agent({ me, can }) {
  const tenantId = me.tenant.id
  const writable = can(P.TENANT_UPDATE)

  const config = useApi(() => getAgentConfig(tenantId), [tenantId])
  // The preset catalogue is a dropdown's worth of data; a failure to load it
  // must not take the page down, so its error is handled inline.
  const presets = useApi(() => listPresets(), [])

  const [draft, setDraft] = useState(null)
  const [flash, setFlash] = useState(null)

  // Re-seed the form whenever the server's copy changes -- after a save, or a
  // reload. The server's response is the source of truth, never the draft.
  useEffect(() => {
    if (config.data) setDraft(draftFrom(config.data))
  }, [config.data])

  const save = useAction(
    (body) => updateVoice(tenantId, body),
    {
      onSuccess: () => {
        setFlash('Agent settings saved. They apply from the next call onwards.')
        config.reload()
      },
    }
  )

  const dirty = useMemo(() => {
    if (!draft || !config.data) return false
    return Object.keys(changedFields(draft, config.data)).length > 0
  }, [draft, config.data])

  const set = (key) => (event) => {
    const target = event.target
    const value = target.type === 'checkbox'
      ? target.checked
      : target.type === 'number' || target.type === 'range'
        ? Number(target.value)
        : target.value
    setDraft((previous) => ({ ...previous, [key]: value }))
    setFlash(null)
  }

  const onSubmit = (event) => {
    event.preventDefault()
    if (!config.data) return
    const body = changedFields(draft, config.data)
    if (Object.keys(body).length === 0) return
    save.run(body)
  }

  return (
    <>
      <div className="page__header">
        <div>
          <h1>Agent</h1>
          <p className="page__description">
            How the voice agent answers, sounds and behaves on every call.
          </p>
        </div>
      </div>

      <AsyncSection
        loading={config.loading}
        error={config.error}
        onRetry={config.reload}
        resource="the agent configuration"
      >
        {config.data && draft && (
          <form className="stack" onSubmit={onSubmit} noValidate>
            {flash && (
              <Alert tone="ok" onDismiss={() => setFlash(null)}>
                {flash}
              </Alert>
            )}
            {save.error && (
              <Alert tone="error" onDismiss={save.clearError}>
                {save.error.message}
              </Alert>
            )}
            {!writable && (
              <Alert tone="info">
                You have read-only access to these settings. An owner or admin
                can change them.
              </Alert>
            )}

            <Identity config={config.data} />

            <Personality config={config.data} />

            <Model
              config={config.data}
              presets={presets}
              draft={draft}
              set={set}
              writable={writable}
            />

            <Speech draft={draft} set={set} writable={writable} />

            <Behaviour config={config.data} />

            <Compliance config={config.data} />

            <Channels config={config.data} />

            {writable && (
              <div className="toolbar" role="group" aria-label="Save changes">
                <button
                  type="submit"
                  className="btn btn--primary"
                  disabled={!dirty || save.pending}
                >
                  {save.pending ? 'Saving…' : 'Save changes'}
                </button>
                <button
                  type="button"
                  className="btn"
                  disabled={!dirty || save.pending}
                  onClick={() => { setDraft(draftFrom(config.data)); setFlash(null) }}
                >
                  Discard
                </button>
                <span className="muted" style={{ fontSize: 12 }} aria-live="polite">
                  {dirty ? 'Unsaved changes' : 'No changes'}
                </span>
              </div>
            )}
          </form>
        )}
      </AsyncSection>
    </>
  )
}

/* ------------------------------------------------------------- sections --- */

function Card({ title, description, children }) {
  return (
    <section className="card" aria-label={title}>
      <div className="card__header">
        <div>
          <h3>{title}</h3>
          {description && (
            <p className="muted" style={{ margin: '4px 0 0', fontSize: 13 }}>
              {description}
            </p>
          )}
        </div>
      </div>
      <div className="card__body">{children}</div>
    </section>
  )
}

/** A value the backend has no write API for. Shown, never faked as editable. */
function ReadOnlyValue({ children }) {
  return <div className="kv__value">{children}</div>
}

function NotSet({ label = 'Not set' }) {
  return <span className="muted">{label}</span>
}

function Identity({ config }) {
  return (
    <Card
      title="Identity"
      description="Set when the account was provisioned. Contact support to change these."
    >
      <div className="kv">
        <div className="kv__key">Business</div>
        <ReadOnlyValue>{config.name}</ReadOnlyValue>
        <div className="kv__key">Industry</div>
        <ReadOnlyValue>{humanise(config.industry)}</ReadOnlyValue>
        <div className="kv__key">Phone number</div>
        <ReadOnlyValue>
          <span className="mono">{config.twilio_number}</span>
        </ReadOnlyValue>
        <div className="kv__key">Timezone</div>
        <ReadOnlyValue>{config.timezone}</ReadOnlyValue>
      </div>
    </Card>
  )
}

function Personality({ config }) {
  return (
    <Card
      title="Personality"
      description="What the agent calls itself and how it opens a call. These are not editable from the dashboard yet."
    >
      <div className="kv">
        <div className="kv__key">Agent name</div>
        <ReadOnlyValue>
          {/* Whatever the tenant configured. Never a hard-coded default. */}
          {config.agent_name || <NotSet />}
        </ReadOnlyValue>
        <div className="kv__key">Language</div>
        <ReadOnlyValue>{config.language}</ReadOnlyValue>
      </div>

      <div style={{ marginTop: 14 }}>
        <div className="kv__key" style={{ marginBottom: 4 }}>Greeting</div>
        {/* Tenant-supplied text. Rendered as a child; React escapes it. */}
        <div className="turn__text">{config.greeting || <NotSet />}</div>
      </div>

      <div style={{ marginTop: 14 }}>
        <div className="kv__key" style={{ marginBottom: 4 }}>
          Extra instructions
        </div>
        <div className="turn__text">
          {config.system_prompt_extra || <NotSet label="No extra instructions" />}
        </div>
      </div>
    </Card>
  )
}

function Model({ config, presets, draft, set, writable }) {
  const options = presets.data ?? []
  const active = options.find((preset) => preset.key === draft.llm_preset)

  return (
    <Card
      title="Model"
      description="Which AI answers the phone. Changes apply from the next call."
    >
      <div className="grid grid--halves">
        <Field
          label="Preset"
          htmlFor="agent-preset"
          hint={
            active
              ? `${active.brand} · ${active.model} · ~${active.latency_ms}ms to first word`
              : 'The preset decides the provider and model.'
          }
        >
          <select
            id="agent-preset"
            className="select"
            value={draft.llm_preset}
            onChange={set('llm_preset')}
            disabled={!writable}
          >
            {/* A tenant on a preset the catalogue no longer lists must still
                see what they are on, rather than silently snapping to the
                first option. */}
            {!options.some((preset) => preset.key === draft.llm_preset) && (
              <option value={draft.llm_preset}>
                {draft.llm_preset || 'Not set'}
              </option>
            )}
            {options.map((preset) => (
              <option key={preset.key} value={preset.key}>
                {humanise(preset.key)} — {preset.brand}
              </option>
            ))}
          </select>
        </Field>

        <Field
          label="Temperature"
          htmlFor="agent-temperature"
          hint="Lower is more consistent, higher is more varied. 0 to 2."
        >
          <input
            id="agent-temperature"
            className="input"
            type="number"
            min={0}
            max={2}
            step={0.05}
            value={draft.temperature}
            onChange={set('temperature')}
            disabled={!writable}
          />
        </Field>
      </div>

      {presets.error && (
        <p className="muted" style={{ marginBottom: 0 }}>
          The preset catalogue could not be loaded, so only the current value is
          shown.
        </p>
      )}

      <div className="kv" style={{ marginTop: 4 }}>
        <div className="kv__key">Resolved provider</div>
        <ReadOnlyValue>
          {config.llm_provider ? humanise(config.llm_provider)
            : <NotSet label="From preset" />}
        </ReadOnlyValue>
        <div className="kv__key">Resolved model</div>
        <ReadOnlyValue>
          {config.llm_model
            ? <span className="mono">{config.llm_model}</span>
            : <NotSet label="From preset" />}
        </ReadOnlyValue>
      </div>
    </Card>
  )
}

function Speech({ draft, set, writable }) {
  return (
    <Card
      title="Voice and speech"
      description="Live tuning. If a caller says the agent interrupts or rushes, adjust these."
    >
      <div className="grid grid--halves">
        <Field
          label="Voice ID"
          htmlFor="agent-voice"
          hint="The text-to-speech voice. Leave empty for the account default."
        >
          <input
            id="agent-voice"
            className="input"
            type="text"
            value={draft.voice_id}
            onChange={set('voice_id')}
            disabled={!writable}
            placeholder="Account default"
          />
        </Field>

        <Field
          label="Speech speed"
          htmlFor="agent-speed"
          hint="1.0 is normal pace."
        >
          <input
            id="agent-speed"
            className="input"
            type="number"
            min={0.5}
            max={2}
            step={0.05}
            value={draft.speech_speed}
            onChange={set('speech_speed')}
            disabled={!writable}
          />
        </Field>

        <Field
          label="Silence before replying (seconds)"
          htmlFor="agent-vad"
          hint="How long the caller must pause before the agent speaks. Raise it if the agent cuts people off."
        >
          <input
            id="agent-vad"
            className="input"
            type="number"
            min={0.1}
            max={2}
            step={0.05}
            value={draft.vad_stop_secs}
            onChange={set('vad_stop_secs')}
            disabled={!writable}
          />
        </Field>

        <Field
          label="Natural speech"
          htmlFor="agent-humanize"
          hint="Adds the small hesitations and acknowledgements people expect."
        >
          <label className="row" style={{ gap: 8 }} htmlFor="agent-humanize">
            <input
              id="agent-humanize"
              type="checkbox"
              checked={draft.humanize}
              onChange={set('humanize')}
              disabled={!writable}
            />
            <span>{draft.humanize ? 'Enabled' : 'Disabled'}</span>
          </label>
        </Field>
      </div>
    </Card>
  )
}

function Behaviour({ config }) {
  const zone = config.timezone
  return (
    <Card
      title="Call behaviour"
      description="Business hours and where a call goes when a human is needed."
    >
      <div className="kv">
        <div className="kv__key">Business hours</div>
        <ReadOnlyValue>
          {/* Rendered in the tenant's own zone, which is what the backend
              compares against -- not the viewer's browser. */}
          {formatTime(`1970-01-01T${config.business_open}Z`, 'UTC')}
          {' – '}
          {formatTime(`1970-01-01T${config.business_close}Z`, 'UTC')}
          <span className="muted"> ({zone})</span>
        </ReadOnlyValue>
        <div className="kv__key">Appointment length</div>
        <ReadOnlyValue>{config.appointment_minutes} minutes</ReadOnlyValue>
        <div className="kv__key">Transfer to</div>
        <ReadOnlyValue>
          {config.escalation_number
            ? <span className="mono">{config.escalation_number}</span>
            : <NotSet label="No transfer number set" />}
        </ReadOnlyValue>
        <div className="kv__key">Notifications to</div>
        <ReadOnlyValue>
          {config.notify_sms_number
            ? <span className="mono">{config.notify_sms_number}</span>
            : <NotSet />}
        </ReadOnlyValue>
        <div className="kv__key">IVR menu</div>
        <ReadOnlyValue>
          <StatusBadge
            status={config.ivr_enabled ? 'active' : 'draft'}
            label={config.ivr_enabled ? 'Enabled' : 'Disabled'}
          />
        </ReadOnlyValue>
      </div>
    </Card>
  )
}

function Compliance({ config }) {
  return (
    <Card title="Recording" description="What callers are told, and whether audio is kept.">
      <div className="kv">
        <div className="kv__key">Call recording</div>
        <ReadOnlyValue>
          <StatusBadge
            status={config.record_calls ? 'active' : 'draft'}
            label={config.record_calls ? 'Recording' : 'Not recording'}
          />
        </ReadOnlyValue>
      </div>
      {config.record_calls && (
        <div style={{ marginTop: 14 }}>
          <div className="kv__key" style={{ marginBottom: 4 }}>Disclaimer</div>
          <div className="turn__text">{config.recording_disclaimer}</div>
        </div>
      )}
    </Card>
  )
}

function Channels({ config }) {
  const channels = [
    ['SMS', config.sms_enabled],
    ['WhatsApp', config.whatsapp_enabled],
  ]
  return (
    <Card title="Text channels" description="Where the agent also answers in writing.">
      <div className="kv">
        {channels.map(([label, enabled]) => (
          <ChannelRow key={label} label={label} enabled={enabled} />
        ))}
      </div>
    </Card>
  )
}

function ChannelRow({ label, enabled }) {
  return (
    <>
      <div className="kv__key">{label}</div>
      <div className="kv__value">
        <StatusBadge
          status={enabled ? 'active' : 'draft'}
          label={enabled ? 'Enabled' : 'Disabled'}
        />
      </div>
    </>
  )
}
