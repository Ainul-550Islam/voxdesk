# VoxDesk dashboard — Next.js / TypeScript (Phase 1, strangler)

The Next.js successor to the Vite dashboard in `dashboard/`. The Vite app
remains the production dashboard until every page here has proven parity; this
app is the increment-by-increment migration, per
`docs/EXPANSION-ROADMAP.md` Phase 1.

## Why both exist

* `dashboard/` (Vite + React, JSX) — the working, tested dashboard. **Do not
  delete it.** It is retired page-by-page only after a Next.js page passes its
  parity checks.
* `dashboard-next/` (Next.js 14 + React 18 + TypeScript, App Router) — the
  target. Server-rendered routes, typed API client, strict TS.

## Run

```bash
cd dashboard-next
npm install
cp .env.example .env.local   # set NEXT_PUBLIC_API_BASE_URL
npm run dev                  # http://localhost:3000
```

The backend must be reachable at `NEXT_PUBLIC_API_BASE_URL` (default
`http://localhost:8000`).

## Auth model

* `POST /auth/login` returns a short-lived access token; it is held in
  `sessionStorage` only (never `localStorage` — the long-lived refresh token
  stays in the backend's HttpOnly cookie, unreachable by JS).
* `lib/api.ts` sends the bearer token + cookies on every request, transparently
  refreshes on 401, and redirects to `/login` only when refresh also fails.

## Migration checklist (Vite page → Next.js route)

| Vite page (`dashboard/src/pages`) | Next.js route | Status |
|---|---|---|
| Overview | `/dashboard` | ✅ done |
| Calls | `/dashboard/calls` | ✅ done |
| CallDetail | `/dashboard/calls/[id]` | ✅ done |
| Analytics | `/dashboard/analytics` | ✅ done |
| Appointments | `/dashboard/appointments` | ✅ done |
| Campaigns | `/dashboard/campaigns` | ✅ done |
| Leads | `/dashboard/leads` | ✅ done |
| Knowledge | `/dashboard/knowledge` | ✅ done |
| Integrations | `/dashboard/integrations` | ✅ done |
| Agent | `/dashboard/agent` | ✅ done |
| Team | `/dashboard/team` | ✅ done |
| Billing | `/dashboard/billing` | ✅ done |
| Audit | `/dashboard/audit` | ✅ done |
| Login | `/login` | ✅ done |

## Verify

```bash
npm run build    # next build (type-check + production bundle)
npm test         # vitest (lib/format.test.ts and future unit tests)
```
