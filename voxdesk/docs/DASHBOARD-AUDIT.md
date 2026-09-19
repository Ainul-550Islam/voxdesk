# STEP 8 pre-work: audit of the existing dashboard

Written **before** anything was modified.

## What existed

The whole dashboard is **414 lines across six files**.

| File | Lines | What it does |
|---|--:|---|
| `src/lib/api.js` | 140 | Auth + five data calls |
| `src/App.jsx` | 127 | The entire application, one screen |
| `src/components/Login.jsx` | 92 | Sign-in form |
| `src/components/CallTable.jsx` | 30 | Five-column table |
| `src/components/StatCards.jsx` | 20 | Five stat tiles |
| `src/main.jsx` | 5 | Mount |

No router. No pages. No layout. No charts. No pagination. No tests. Two
dependencies (`react`, `react-dom`) and two dev dependencies (`vite`,
`@vitejs/plugin-react`).

## Findings

### The one that misleads a buyer

**F1 — "Est. revenue captured" is a made-up number, displayed as money.**

```jsx
{ label: 'Est. revenue captured', value: `$${stats.estimated_value_usd}` }
```

and on the server:

```python
# This is the number you put on the invoice.
"estimated_value_usd": booked * 150,
```

$150 is a constant. It is not the tenant's average job value, it is not
derived from any appointment, lead or invoice, and it is not configurable. A
tenant with two bookings sees "$300 revenue captured" whatever those bookings
were worth. This is exactly what requirement 33 prohibits, and STEP 7's audit
already flagged the backend half of it (F11).

### Architecture

| # | Finding |
|---|---|
| F2 | **One screen.** `App.jsx` is the whole product: header, stats, table, transcript modal. There is no routing, so there is no URL for a call, no deep link, no browser back. |
| F3 | **No pagination anywhere.** `getCalls()` requests `limit=50` by relying on the server default and renders all of it. There is no page control, no total count, and no way to see call 51. |
| F4 | **No filtering or search at all.** Not in the UI and not in the API — `list_calls` accepts only `limit`. |
| F5 | **Fetch logic is inline.** `load()` in `App.jsx` is the only data flow; there is no shared loading, error or retry handling to reuse. |
| F6 | **No loading, empty or error states** beyond a single `Loading…` on first boot and one `notice` string. An empty tenant renders an empty `<table>` with headers and nothing else. |
| F7 | **Everything is inline styles.** No design tokens, no responsive rules, no breakpoints. The table has no horizontal scroll container, so on a phone it overflows the viewport. |
| F8 | **No accessibility work.** No landmarks, no `<label>` association, no focus management on the transcript overlay, no `scope` on table headers, and status is communicated **by colour alone** (`#059669` for booked, `#d97706` for escalated). |

### Data and correctness

| # | Finding |
|---|---|
| F9 | **The agent's name is hard-coded to "Alex"** in the transcript renderer: `t.speaker === 'user' ? 'Caller' : 'Alex'`. `Tenant.agent_name` exists and is configurable; a tenant who renamed their agent sees the wrong name on every line. |
| F10 | **`booking_rate` has an undocumented denominator.** It is `booked / all calls in the window`, which counts calls that could never have booked — wrong numbers, missed calls, a caller asking for opening hours. Requirement 12 asks for a defined numerator and denominator. |
| F11 | **Timestamps render in the browser's timezone.** `new Date(c.started_at).toLocaleString()` uses whatever locale the laptop has. `Tenant.timezone` exists (STEP 6) and is ignored, so a US business viewed from Dhaka reads every call time wrong. |
| F12 | **`duration` is rendered as raw seconds** — `Math.round(c.duration)}s` — so a 22-minute call shows as `1320s`. |
| F13 | **Nine of twelve backend surfaces are unreachable from the UI.** Billing (STEP 7), CRM integrations (STEP 5), calendar and appointments (STEP 6), knowledge (STEP 4), audit log, campaigns, leads, agent configuration, compliance — all shipped, all invisible. `getTeam()` is defined in `api.js` and never called. |

### Security

| # | Finding | Verdict |
|---|---|---|
| F14 | Access token in a module-level variable, refresh token in an HttpOnly cookie | **Correct, keep.** Nothing in `localStorage`. |
| F15 | Single 401 retry then logout | **Correct, keep.** No refresh loop. |
| F16 | Transcript rendered as `{t.text}` | **Safe.** React escapes by default; there is no `dangerouslySetInnerHTML` anywhere in the tree. Worth a regression test rather than a fix. |
| F17 | `tenantId` comes from `me.tenant.id` and is put in the URL path | Harmless today — every backend route re-derives the tenant from the JWT and ignores the path — but it teaches the wrong pattern and invites a future endpoint that trusts it. |
| F18 | `can()` exists and is used for two sections | Right idea, but `me.permissions` is read directly in `App.jsx` with no shared helper, so the next page will re-invent it. |

## Direct answers to the questions the brief asked

**What does the frontend already do?**
Sign in, resume a session from the refresh cookie, show five stat tiles and up
to fifty recent calls, and open a transcript in an inline panel. That is all.

**What is mocked or faked?**
One thing, and it is the most prominent number on the screen:
`estimated_value_usd = booked × 150`. No other placeholder, sample dataset or
hard-coded chart exists — because no charts exist.

**Does it handle roles?**
Partially. `me.permissions` gates the stats block and the call table. Nothing
else, because nothing else is built.

**Is pagination present?**
No, on either side.

**Can the existing endpoints support an analytics page?**
No. `/stats` returns six numbers for a fixed day-window and no time series, no
conversion funnel, no usage breakdown, and no per-status counts. Requirement 13
anticipated this: new aggregation endpoints are needed.

## What STEP 8 keeps

`api.js`'s token handling (F14) and its single-retry refresh (F15) are already
right and are carried forward unchanged in substance. `Login.jsx`'s
non-enumerating error handling is kept. The five existing data functions keep
working; new ones are added alongside.

The `estimated_value_usd` field stays in the API response so nothing breaks,
but the dashboard stops displaying it — see `docs/DASHBOARD.md`.