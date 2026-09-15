# VoxDesk — Incident response plan (SOC 2-ready)

## Severity levels

| Severity | Definition | Response time |
|---|---|---|
| SEV-1 | Voice/billing down for all tenants, or data breach | 15 min |
| SEV-2 | Degraded for some tenants / single feature down | 60 min |
| SEV-3 | Non-critical bug | next business day |

## Roles

- **Incident Commander** — single decision maker.
- **Responder** — investigates and fixes.
- **Communications** — customer and regulatory notification (if required).

## Response flow

1. **Detect** — Grafana alert, Sentry, or customer report. Record the request
   id / alert id.
2. **Declare** — pick severity, open an incident channel, start a timeline.
3. **Contain** — stop the bleeding first: roll back (`scripts/deploy.sh` at the
   last-good SHA), scale, or revoke the compromised credential.
4. **Diagnose** — use `X-Request-ID` + JSON logs, Grafana, Sentry traces.
5. **Fix & verify** — smallest safe change, verify with tests, deploy.
6. **Post-mortem** — timeline, root cause, and **at least one** follow-up
   control/automation, tracked to completion.

## Data-breach annex

If personal data (recordings/transcripts) may have been exposed:

1. Preserve evidence (logs, access records) — do not destroy.
2. Assess scope: which tenants, which data, how exposed.
3. Notify the affected tenants within 72h (GDPR) or the applicable window.
4. Notify your supervisory authority where required.
5. Document the cause and corrective controls.

## Contacts / escalation

Fill in: primary on-call, secondary on-call, provider support numbers
(Twilio, hosting), and legal counsel. Keep this file current with every team
change — an out-of-date contact list is an audit finding.
