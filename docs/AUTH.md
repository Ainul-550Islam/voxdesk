# VoxDesk — Authentication, Authorization & Tenant Isolation

Everything in this document describes code that exists in the repository. There
are **no default accounts and no example credentials anywhere in VoxDesk** — a
shipped username/password pair is a backdoor, not a convenience.

---

## 1. Threat model

VoxDesk is multi-tenant SaaS. Every tenant's rows — calls, transcripts, leads,
appointments, campaigns, recordings, analytics — sit in shared tables separated
only by a `tenant_id` column. The single most damaging failure mode is therefore
**one customer reading another customer's phone transcripts**. Transcripts
contain names, phone numbers, addresses, appointment reasons and, for medical
and legal clients, information that carries statutory penalties if disclosed.

The design assumption is that an attacker is an ordinary authenticated
customer who reads the API docs and edits ids in requests. Consequently:

- The `tenant_id` in a URL is treated as **untrusted input**, never as an
  instruction.
- Authorization is enforced **server-side on every route**. The dashboard hiding
  a button is a usability feature, not a control.

---

## 2. Authentication flow

```
POST /auth/login          { email, password }
   ├── email normalized (trim, NFKC, lowercase)
   ├── user looked up; bcrypt verify (or verify_dummy on miss — constant work)
   ├── checks: is_active, not locked out, tenant active
   ├── audit LOGIN_SUCCESS / LOGIN_FAILURE
   └── 200 { access_token, expires_in, user }
       + Set-Cookie: voxdesk_refresh=…  HttpOnly; SameSite=Lax; Path=/auth

GET  /api/...             Authorization: Bearer <access_token>
POST /auth/refresh        (cookie) → new access token, refresh token rotated
POST /auth/logout         revokes the current refresh token
POST /auth/logout-all     revokes every session and bumps token_version
GET  /auth/me             current user + tenant + effective permissions
GET  /auth/roles          the permission matrix (for building UI)
```

### Access token — JWT, 15 minutes

| claim | meaning |
|-------|---------|
| `sub` | user id |
| `tid` | tenant id |
| `role`| role at issue time |
| `tv`  | `token_version` at issue time |
| `typ` | `"access"` |
| `iss` / `aud` | validated on every request |
| `iat` / `exp` / `jti` | required |

`decode_access_token` pins `algorithms=["HS256"]` and requires
`exp, iat, sub, iss, aud`. `alg: none` and `alg: RS256` key-confusion attacks
are both rejected; there are tests for each.

`tid` is **not** trusted on its own. On every request `get_current_user`
re-reads the user row and compares the stored `tenant_id` to the claim; a
mismatch is a 401. A token cannot outlive a tenant reassignment.

### Refresh token — opaque, 14 days, rotating

- 48 bytes from `secrets.token_urlsafe`; **never a JWT**.
- Stored as a SHA-256 digest. The database never holds a usable token. (No salt
  is needed: the input is high-entropy CSPRNG output, not a guessable password,
  and a plain digest keeps lookup O(1) on a unique index.)
- **Single use.** Each refresh marks the old row used and links `replaced_by`.
- **Reuse detection.** Presenting an already-used token means it was stolen
  (or the chain forked), so every session for that user is revoked and
  `token_version` is bumped. Both the thief and the victim are logged out; the
  victim signs in again, the thief cannot.

### Why the refresh token is a cookie and the access token is not

`localStorage` is readable by any script on the page, so one XSS equals a
permanent account takeover. The refresh token is `HttpOnly` (invisible to JS)
and `Path=/auth` (never transmitted to `/api` or `/telephony`). The access token
lives in a JavaScript variable that dies on refresh — a stolen one expires in
15 minutes. `secure=True` is set automatically when `APP_ENV=production`.

### Instant revocation: `token_version`

Stateless JWTs cannot normally be recalled. `User.token_version` is embedded in
each token and compared on every request. Bumping it invalidates every
outstanding token for that user immediately. It is bumped on deactivation, role
change, logout-all, and refresh-token reuse detection.

### Passwords

- **bcrypt, cost 12**, used directly (not through passlib, which is unmaintained
  and broke against bcrypt 4.x/5.x).
- Policy: ≥ 12 characters, ≤ 72 bytes (bcrypt's hard limit — over-length input
  is **rejected**, never silently truncated), at least 3 of 4 character classes,
  not on the common-password list, and must not contain the email local part.
- NFKC normalization so a password typed on a different keyboard still matches.
- Transparent rehash on login when the cost factor is raised later.
- Never logged, never serialized, never returned. `UserOut` is a hand-written
  allow-list, deliberately **not** `from_attributes`, so adding a sensitive
  column to `User` in future cannot silently leak it through the API.

### No user enumeration

Every failure — unknown email, wrong password, inactive user, locked account,
suspended tenant — returns exactly `401 {"detail": "Invalid email or password."}`.
Failure paths also spend equal CPU (`verify_dummy()` runs a real bcrypt
comparison against a dummy hash when no user is found), so response timing does
not reveal which emails are registered.

### Lockout

After `MAX_FAILED_LOGINS` (default 8) consecutive failures the account locks for
`LOCKOUT_MINUTES` (default 15). A successful login resets the counter.

---

## 3. Roles and permissions

Five roles, ordered. Every permission is declared in **one place**
(`app/auth/permissions.py` + `app/auth/rbac.py`); there is not a single
`if user.role == "admin"` string comparison anywhere in a route.

| Role | Level | Intended for |
|------|-------|--------------|
| `owner`   | 100 | The business owner. Everything, including billing and owner management. |
| `admin`   | 80  | Operations lead. Everything except billing and owner management. |
| `manager` | 60  | Runs campaigns and the team's day-to-day work. No user management. |
| `agent`   | 40  | Handles calls, leads and appointments. Read-only on configuration. |
| `viewer`  | 20  | Read-only. Cannot mutate anything. |

Permissions are `resource:verb` strings — `call:read`, `lead:create`,
`campaign:run`, `user:manage`, `analytics:read`, `audit:read`, and so on.

Enforcement helpers (`app/auth/dependencies.py`):

| Dependency | Use |
|------------|-----|
| `get_current_user` | Valid token → live, active `User`. |
| `get_current_tenant` | The tenant read from the user row. |
| `get_context` | `TenantContext(user, tenant, tenant_id, role, permissions)`. |
| `require_permission(*perms)` | Permission check with no path tenant. |
| `require_role(...)` / `require_min_role(...)` | Rare cases where rank matters. |
| **`scoped_permission(*perms)`** | **The standard for `/tenants/{tenant_id}/…`**: asserts path tenant == token tenant, then checks the permission. |
| `get_owned(session, Model, id, ctx)` | Loads a row **and** verifies `tenant_id`. 404 for both missing and foreign. |

### Escalation guards

- `can_assign_role` permits assigning only roles **strictly below** the actor's
  own level. An admin cannot create another admin, and certainly not an owner.
- Only an owner can manage another owner.
- `USER_CREATE` / `user:manage` are held by owner and admin only.
- The **last active owner** of a tenant cannot be demoted or deactivated. Both
  paths re-count active owners inside the same transaction.
- Users are **deactivated, never deleted**, so transcripts keep a valid author.

### Platform-only permissions — known limitation

`tenant:create` and `tenant:delete` are in `_PLATFORM_ONLY`. **No role holds
them, not even owner**, and `get_platform_admin` currently denies everyone. A
tenant therefore cannot create or delete tenants through the API at all, which
is the correct default for a hosted product but means provisioning is an
operator task:

```bash
python -m scripts.seed_demo_tenant          # create the tenant row
python -m scripts.create_owner --tenant-id <uuid> --email owner@business.com
```

A proper platform-admin plane is deliberately out of scope for this step.

---

## 4. Tenant isolation

**Rule: the tenant is derived from the principal, never from the request.**

The URL shape `/api/tenants/{tenant_id}/…` was kept for backward compatibility,
but the path id is only ever an assertion to be checked:

```python
@router.get("/tenants/{tenant_id}/calls")
async def list_calls(
    tenant_id: uuid.UUID,
    ctx: TenantContext = Depends(scoped_permission(Permission.CALL_READ)),
    session: AsyncSession = Depends(get_session),
):
    rows = await session.execute(
        select(Call).where(Call.tenant_id == ctx.tenant_id)   # ctx, never the path
    )
```

Three layers:

1. **Path guard** — `scoped_permission` 404s when the path tenant differs from
   the token tenant.
2. **Query filter** — every query filters on `ctx.tenant_id`. Even if a guard
   were removed, the query still cannot see foreign rows.
3. **Ownership check** — resources addressed by their own id
   (`/calls/{call_id}/transcript`) go through `get_owned`, which verifies the
   row's `tenant_id` after loading it.

**Cross-tenant access returns 404, never 403.** A 403 confirms the resource
exists, which is itself an information leak: an attacker could enumerate valid
call ids by status code alone.

Request bodies never carry `tenant_id` or `owner_id`. Where a client sends one
anyway it is ignored — objects are always constructed with `ctx.tenant_id`, and
there is a test asserting an injected `tenant_id` in a body does not take effect.

---

## 5. Machine authentication is separate from human authentication

| Surface | Mechanism | Why |
|---------|-----------|-----|
| `/telephony/voice`, `/telephony/status`, … | Twilio `X-Twilio-Signature` HMAC | Twilio cannot hold a user JWT. |
| `/telephony/ws` (Media Stream) | Short-lived signed stream token in the URL | Twilio cannot sign a WebSocket upgrade. |
| `/api/*`, `/auth/*` | User JWT | Humans. |

The WebSocket previously accepted **any** connection. It now requires a token
minted by `app/telephony/stream_auth.py`, HMAC-SHA256 over `call_sid:issued_at`,
valid for 120 seconds and **bound to that specific call SID** — a token
captured from one call cannot be replayed to listen to another. Only a caller
who already received our TwiML holds a valid token.

Note that `_verify_twilio()` is bypassed when `APP_ENV=development`, so that
webhooks can be exercised with curl. Production must set `APP_ENV=production`.

---

## 6. Startup security gate

`Settings.validate_security()` runs in the FastAPI lifespan.

In **production** (`APP_ENV=production`) the app **refuses to boot** if:

- `JWT_SECRET` is still the default, or shorter than 32 characters
- `SECRET_KEY` is still `change-me`
- `CORS_ORIGINS` contains `*`

In development the same problems are logged as warnings so nothing blocks local
work. This makes "we forgot to set the secret in prod" a loud crash at deploy
time rather than a silent forgery vulnerability.

CORS is restricted to `settings.cors_origin_list` with
`allow_credentials=True` and explicit method and header lists. A wildcard origin
is incompatible with credentialed requests and is rejected outright.

---

## 7. Audit log

`audit_logs` records: `LOGIN_SUCCESS`, `LOGIN_FAILURE`, `LOGOUT`,
`TOKEN_REFRESH`, `USER_CREATED`, `USER_DEACTIVATED`, `USER_REACTIVATED`,
`ROLE_CHANGED`, `PASSWORD_CHANGED`, `AUTHZ_DENIED` — with actor, target,
tenant, IP, user agent and a small JSON detail blob.

**Never logged:** passwords (right or wrong), password hashes, raw JWTs,
refresh tokens, API keys. The `detail` blob is written by hand at each call
site, never dumped from a request body.

Readable at `GET /api/team/audit` by holders of `audit:read` (owner, admin),
scoped to their own tenant.

---

## 8. Environment variables

| Variable | Default | Notes |
|----------|---------|-------|
| `JWT_SECRET` | `insecure-development-only-change-me` | **Required in production.** `openssl rand -hex 32`. |
| `JWT_ISSUER` | `voxdesk` | Validated on decode. |
| `JWT_AUDIENCE` | `voxdesk-api` | Validated on decode. |
| `ACCESS_TOKEN_MINUTES` | `15` | Keep short. |
| `REFRESH_TOKEN_DAYS` | `14` | Rotating and revocable. |
| `MAX_FAILED_LOGINS` | `8` | Then lockout. |
| `LOCKOUT_MINUTES` | `15` | |
| `CORS_ORIGINS` | `http://localhost:5173` | Comma-separated. No `*`. |
| `SECRET_KEY` | `change-me` | **Must** be changed in production. |
| `APP_ENV` | `development` | `production` enables the gate, `secure` cookies and Twilio signature checks. |

Rotating `JWT_SECRET` invalidates all access tokens immediately; refresh tokens
survive because they are opaque database rows, so users are transparently
re-issued tokens on their next refresh.

---

## 9. Local development

```bash
cp .env.example .env
make up
make migrate                     # includes 0003_auth_rbac
python -m scripts.seed_demo_tenant
python -m scripts.create_owner --tenant-id <uuid printed above> \
                               --email you@example.com
# password is prompted for, never passed on the command line
make dev                         # API  → http://localhost:8000/docs
cd dashboard && npm run dev      # UI   → http://localhost:5173
```

## 10. Production checklist

- [ ] `APP_ENV=production`
- [ ] `JWT_SECRET` from `openssl rand -hex 32`, stored in a secret manager
- [ ] `SECRET_KEY` changed
- [ ] `CORS_ORIGINS` set to your real dashboard origin only
- [ ] HTTPS everywhere (the refresh cookie is `Secure` in production and will
      not be sent over plain HTTP)
- [x] Application-level per-IP rate limiting in front of `/auth/login`
      (`RATE_LIMIT_ENABLED`, `app/core/rate_limit.py`); a WAF / reverse-proxy
      limiter is still recommended for defence-in-depth against distributed
      sprays
- [ ] `alembic upgrade head`
- [ ] At least one owner created per tenant, via `scripts.create_owner`
- [ ] Database backups cover `users`, `refresh_tokens` and `audit_logs`

---

## 11. Known limitations

1. **No self-service tenant signup.** Provisioning is an operator script. A
   platform-admin plane is future work.
2. **No password reset flow.** Requires email delivery, which VoxDesk does not
   yet have. An owner can currently only re-create an account.
3. **No MFA.** The schema (`token_version`, audit log) is ready for it.
4. **Login rate limiting is per-IP at the application level** (off by default;
   `RATE_LIMIT_ENABLED=true` and `RATE_LIMIT_LOGIN_PER_MINUTE`). A WAF or
   reverse-proxy limiter in front is still recommended for a distributed
   spray across many source IPs.
5. **The Twilio signature check is disabled when `APP_ENV=development`** — a
   pre-existing convenience, unchanged by this step, and a reason production
   must never run with the development flag.