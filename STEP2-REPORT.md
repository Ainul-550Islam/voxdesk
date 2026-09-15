# STEP 2 — Authentication + RBAC + Tenant Isolation
### VoxDesk v0.3 → v0.4 · সম্পূর্ণ রিপোর্ট

---

## এক লাইনে

VoxDesk-এর **প্রতিটা** endpoint আগে খোলা ছিল — যে কেউ URL-এ `tenant_id` বদলে
অন্য ক্লায়েন্টের কল ট্রান্সক্রিপ্ট পড়তে পারত, এমনকি তাদের নামে আসল বিল-হওয়া
ফোন কলও করাতে পারত। এখন **৩৯টা route-এর একটাও unguarded নেই**, এবং টেন্যান্ট
কখনোই request থেকে আসে না — টোকেন থেকে আসে।

**টেস্ট: ২৫৯ passed / ০ failed** (আগে ১৭০ ছিল, নতুন ৮৯টা)।

---

## ১. যে ফাইলগুলো যোগ হয়েছে (১৩টা)

| ফাইল | কাজ |
|---|---|
| `app/auth/__init__.py` | প্যাকেজ |
| `app/auth/permissions.py` | `Permission` str-enum — `call:read`, `campaign:run` ইত্যাদি + ৩টা বান্ডল |
| `app/auth/rbac.py` | রোল→পারমিশন ম্যাট্রিক্স, escalation guard। DB/FastAPI-মুক্ত বিশুদ্ধ ডেটা |
| `app/auth/password.py` | bcrypt cost 12, পলিসি, timing equalizer, rehash |
| `app/auth/jwt.py` | HS256 pinned, `TokenClaims`, opaque refresh token |
| `app/auth/service.py` | `authenticate`, `issue_tokens`, `rotate_refresh_token`, `create_user`, `change_role`, `set_active`, `record_audit` |
| `app/auth/dependencies.py` | `get_current_user`, `scoped_permission`, `get_owned`, `require_permission/role` |
| `app/api/auth_routes.py` | `/auth/login|refresh|logout|logout-all|me|roles` |
| `app/api/team_routes.py` | `/api/team/users` + `/api/team/audit` |
| `app/telephony/stream_auth.py` | সাইন করা WebSocket stream token + শেয়ার্ড Twilio HMAC ভেরিফায়ার |
| `alembic/versions/0003_auth_rbac.py` | **মাইগ্রেশন** |
| `scripts/create_owner.py` | প্রথম owner বানানোর স্ক্রিপ্ট (পাসওয়ার্ড প্রম্পটে, argv-তে নয়) |
| `docs/AUTH.md` | থ্রেট মডেল, রোল টেবিল, env var, প্রোডাকশন চেকলিস্ট |

**টেস্ট ফাইল (৫টা):** `tests/conftest.py`, `test_auth_login.py`, `test_rbac.py`,
`test_tenant_isolation.py`, `test_team_management.py`

**ফ্রন্টএন্ড:** `dashboard/src/components/Login.jsx`

---

## ২. যে ফাইলগুলো বদলেছে (৯টা)

| ফাইল | কী বদলাল |
|---|---|
| `app/db/models.py` | `User`, `RefreshToken`, `AuditLog` + `UserRole`, `AuditAction` enum |
| `app/core/config.py` | JWT সেটিংস, `is_production`, `cors_origin_list`, `validate_security()` |
| `app/main.py` | স্টার্টআপ সিকিউরিটি গেট, দুটো নতুন router, CORS লক, v0.4.0 |
| `app/api/routes.py` | **২০টা endpoint-এর প্রতিটায়** guard + `ctx.tenant_id` |
| `app/telephony/twilio_handler.py` | WebSocket-এ stream token বাধ্যতামূলক; pipecat import lazy; HMAC শেয়ার্ড |
| `app/channels/messaging.py` | **HMAC verification যোগ** (আগে সম্পূর্ণ খোলা ছিল) |
| `dashboard/src/lib/api.js` | টোকেন হ্যান্ডলিং, auto-refresh, 401→লগইন |
| `dashboard/src/App.jsx` | protected route, current user, logout, পারমিশন-ভিত্তিক UI |
| `dashboard/vite.config.js` | `/auth` proxy (কুকি same-origin রাখতে) |
| `.env.example`, `requirements.txt`, `README.md` | auth সেকশন, ৩টা ডিপেন্ডেন্সি |

**হাত দেওয়া হয়নি:** `0001_baseline.py`, `0002_enum_consistency.py`, agent
pipeline, integrations, compliance — অপ্রাসঙ্গিক ফাইল রিরাইট করিনি।

> একবার ভুল করে ৫টা অপ্রাসঙ্গিক ফাইলে `datetime.utcnow()` মাস-এডিট করে
> ফেলেছিলাম। ধরা পড়ায় **সঙ্গে সঙ্গে রিভার্ট** করেছি — ওই ফাইলগুলো এখন আগের মতোই।

---

## ৩. মাইগ্রেশন

**`0003_auth_rbac`** ← `0002_enum_consistency`

- সম্পূর্ণ **additive** — কোনো পুরনো টেবিল বদলায় না, কোনো row মুছে না।
  বিদ্যমান tenant, call, transcript, lead অক্ষত।
- ৩টা টেবিল: `users`, `refresh_tokens`, `audit_logs`
- ২টা PG enum: `userrole`, `auditaction` — **সদস্যের নাম UPPERCASE**, কারণ
  SQLAlchemy `Enum()` value নয়, NAME পার্সিস্ট করে (STEP 1-এর শিক্ষা)
- ইনডেক্স: `uq_users_email` (global unique), `ix_users_tenant_role`,
  `ix_refresh_tokens_token_hash` (unique), `ix_refresh_active`,
  `ix_audit_tenant_time`
- `downgrade()` শুধু এই ৩টা টেবিল ড্রপ করে — ব্যবসায়িক ডেটা হারায় না
- **কোনো ডিফল্ট অ্যাকাউন্ট তৈরি করে না।** শিপ করা ক্রেডেনশিয়াল = ব্যাকডোর।

---

## ৪. Auth ফ্লো

```
POST /auth/login {email, password}
  → email normalize (trim, NFKC, lowercase)
  → bcrypt verify  (ইউজার না থাকলেও verify_dummy চলে — সমান CPU)
  → is_active + lockout + tenant active চেক
  → audit LOGIN_SUCCESS / LOGIN_FAILURE
  → 200 { access_token (15 min), expires_in, user }
    + Set-Cookie: voxdesk_refresh   HttpOnly; SameSite=Lax; Path=/auth
```

**Access token (JWT, ১৫ মিনিট):** `sub`(user) `tid`(tenant) `role` `tv`(token_version)
`iss` `aud` `exp` `iat` `jti`। ডিকোডে `algorithms=["HS256"]` পিন করা —
`alg:none` আর key-confusion দুটোরই টেস্ট আছে।

**`tid`-কেও একা বিশ্বাস করা হয় না** — প্রতি রিকোয়েস্টে user row আবার পড়া হয়
এবং সেখানকার `tenant_id`-র সঙ্গে claim না মিললে 401।

**Refresh token (১৪ দিন):** JWT নয় — `secrets.token_urlsafe(48)`। DB-তে শুধু
SHA-256 হ্যাশ। **একবার ব্যবহারযোগ্য**; পুরনোটা আবার এলে বুঝতে হবে চুরি গেছে →
ওই ইউজারের **সব সেশন বাতিল** + `token_version` বৃদ্ধি। চোর আর ভিকটিম দুজনেই
লগআউট; ভিকটিম আবার লগইন করে, চোর পারে না।

**কেন কুকিতে?** `localStorage` যেকোনো স্ক্রিপ্ট পড়তে পারে — একটা XSS মানেই
চিরস্থায়ী দখল। রিফ্রেশ টোকেন `HttpOnly` (JS দেখতে পায় না) আর `Path=/auth`
(`/api`-তে কখনো যায় না)। access token শুধু একটা JS ভেরিয়েবলে — রিফ্রেশে মরে যায়।

**ইনস্ট্যান্ট রিভোকেশন:** stateless JWT সাধারণত ফেরানো যায় না। `token_version`
টোকেনে বসানো এবং প্রতি রিকোয়েস্টে মেলানো হয় — বাড়ালেই সব টোকেন তৎক্ষণাৎ মৃত।
deactivate / role change / logout-all / refresh reuse — চারটাতেই বাড়ে।

---

## ৫. রোল ও পারমিশন

| রোল | লেভেল | পায় |
|---|---|---|
| `owner` | 100 | সব — বিলিং, owner ম্যানেজমেন্ট সহ |
| `admin` | 80 | সব, বিলিং আর owner ম্যানেজমেন্ট বাদে |
| `manager` | 60 | ক্যাম্পেইন চালানো, দৈনন্দিন কাজ। ইউজার ম্যানেজমেন্ট নেই |
| `agent` | 40 | কল, লিড, অ্যাপয়েন্টমেন্ট। কনফিগে read-only |
| `viewer` | 20 | শুধু পড়া। কিচ্ছু বদলাতে পারে না |

পারমিশন **একটাই জায়গায়** (`permissions.py` + `rbac.py`)। কোনো route-এ একটাও
`if user.role == "admin"` স্ট্রিং তুলনা নেই — টেস্টেও যাচাই করা।

**Escalation গার্ড:**
- নিজের লেভেলের **কড়াভাবে নিচের** রোলই দেওয়া যায় → admin আরেকজন admin বানাতে পারে না
- owner-কে শুধু owner ম্যানেজ করতে পারে
- **শেষ active owner** demote বা deactivate করা যায় না (একই ট্রানজ্যাকশনে গোনা হয়)
- ইউজার **ডিলিট নয়, ডিঅ্যাক্টিভেট** — ট্রান্সক্রিপ্টের author বৈধ থাকে

---

## ৬. টেন্যান্ট আইসোলেশন

**নিয়ম: টেন্যান্ট principal থেকে আসে, request থেকে নয়।**

`/api/tenants/{tenant_id}/…` URL শেপ রাখা হয়েছে (backward compatible), কিন্তু
path-এর id শুধুই একটা **দাবি, যা যাচাই করতে হয়**:

```python
@router.get("/tenants/{tenant_id}/calls")
async def list_calls(
    tenant_id: uuid.UUID,
    ctx: TenantContext = Depends(scoped_permission(Permission.CALL_READ)),
    session: AsyncSession = Depends(get_session),
):
    select(Call).where(Call.tenant_id == ctx.tenant_id)   # ctx — path নয়
```

তিন স্তর:
1. **Path guard** — `scoped_permission` path≠token হলে 404
2. **Query filter** — প্রতিটা কোয়েরি `ctx.tenant_id`-তে ফিল্টার। গার্ড উঠে
   গেলেও কোয়েরি বাইরের row দেখতে পাবে না
3. **Ownership check** — নিজের id দিয়ে ডাকা রিসোর্স (`/calls/{id}/transcript`)
   `get_owned()` দিয়ে যায়, যা লোড করার পর `tenant_id` মেলায়

**ক্রস-টেন্যান্ট = সবসময় 404, কখনো 403।** 403 দিলে বোঝা যায় জিনিসটা আছে —
স্ট্যাটাস কোড দেখেই বৈধ call id খুঁজে বের করা যেত।

---

## ৭. সুরক্ষিত endpoint — ৩৯টার মধ্যে ৩৯টা

স্বয়ংক্রিয় sweep চালিয়ে প্রতিটা route আর তার guard মিলিয়ে দেখা হয়েছে:

```
ok      × 32   JWT + পারমিশন
public  ×  3   /auth/login, /auth/refresh, /health   (উদ্দেশ্যমূলকভাবে খোলা)
hmac    ×  4   Twilio webhooks — সিগনেচার দিয়ে অথেনটিকেট
────────────────────────────────────────────────
UNGUARDED HUMAN ROUTES: NONE
```

---

## ৮. যে নিরাপত্তা সমস্যাগুলো পাওয়া গেছে এবং সারানো হয়েছে

| # | সমস্যা | তীব্রতা | অবস্থা |
|---|---|---|---|
| 1 | **প্রতিটা** endpoint সম্পূর্ণ unauthenticated | 🔴 | ✅ ৩৯/৩৯ সুরক্ষিত |
| 2 | `GET /api/tenants` **সব** টেন্যান্ট লিস্ট করত | 🔴 | ✅ শুধু নিজেরটা |
| 3 | `POST /api/tenants` খোলা — যে কেউ টেন্যান্ট বানাত | 🔴 | ✅ platform-only, সবাইকে deny |
| 4 | ~১২টা endpoint path-এর `tenant_id` বিশ্বাস করত | 🔴 | ✅ `scoped_permission` + `ctx.tenant_id` |
| 5 | `GET /api/calls/{id}/transcript`-এ **কোনো** টেন্যান্ট চেক ছিল না — রিপোর সবচেয়ে স্পষ্ট IDOR | 🔴 | ✅ `get_owned()` |
| 6 | ভয়েস/IVR কনফিগ unauthenticated লেখা যেত | 🔴 | ✅ `TENANT_UPDATE` |
| 7 | `POST /campaigns/{id}/run` — **অজ্ঞাত কেউ আসল বিল-হওয়া কল করাতে পারত** | 🔴 | ✅ আলাদা `CAMPAIGN_RUN` পারমিশন |
| 8 | `/telephony/ws` মিডিয়া স্ট্রিম সম্পূর্ণ খোলা — call SID জানলেই লাইভ অডিও | 🔴 | ✅ সাইন করা, কল-নির্দিষ্ট, ১২০s টোকেন |
| 9 | **`/channels/message` + `/channels/status`-এ HMAC ছিল না** — ভুয়া SMS ঢুকিয়ে ক্লায়েন্টের LLM খরচ করানো যেত | 🔴 | ✅ *(sweep চালানোর সময় নতুন ধরা পড়ে)* |
| 10 | CORS wildcard | 🟠 | ✅ allowlist + credentials |
| 11 | `secret_key="change-me"` প্রোডাকশনেও বুট করত | 🟠 | ✅ স্টার্টআপ গেট |
| 12 | body-তে `tenant_id`/`owner_id` পাঠিয়ে রেকর্ড অন্য টেন্যান্টে ঠেলা | 🟠 | ✅ সবসময় `ctx.tenant_id`, টেস্ট আছে |

**#৯ পরিকল্পনায় ছিল না** — শেষ ধাপের repo-wide sweep-এ বেরিয়েছে।

---

## ৯. স্টার্টআপ সিকিউরিটি গেট

`APP_ENV=production`-এ অ্যাপ **বুটই হবে না** যদি:
- `JWT_SECRET` ডিফল্ট বা ৩২ ক্যারেক্টারের কম
- `SECRET_KEY` এখনো `change-me`
- `CORS_ORIGINS`-এ `*`
- `PUBLIC_BASE_URL` https নয়, বা `TWILIO_AUTH_TOKEN` নেই

যাচাই করা:
```
default secrets → ['JWT_SECRET is still the built-in default',
                   'SECRET_KEY is still the built-in default',
                   'PUBLIC_BASE_URL must use https in production',
                   'TWILIO_AUTH_TOKEN is required to verify webhooks']
```
ডেভেলপমেন্টে একই সমস্যাগুলো শুধু warning — লোকাল কাজ আটকায় না।

---

## ১০. টেস্ট

```
259 passed, 0 failed
```

| ফাইল | সংখ্যা | কী প্রমাণ করে |
|---|---|---|
| `test_auth_login.py` | ৩৪ | হ্যাশিং, পলিসি, JWT (`alg:none`, ভুল কী, ভুল audience, expiry), লকআউট, no-enumeration, রিফ্রেশ রোটেশন, reuse detection, অডিট |
| `test_rbac.py` | ২৩ | পারমিশন ম্যাট্রিক্স, রোল ordering, escalation guard, route-এ প্রয়োগ, denial অডিট, stream token, HMAC |
| `test_tenant_isolation.py` | ১৫ | নিচের ৯টা attack + list bleed |
| `test_team_management.py` | ১৭ | তৈরি/রোল/ডিঅ্যাক্টিভেট, owner protection, সিরিয়ালাইজেশন |
| পুরনো | ১৭০ | **একটাও ভাঙেনি** |

সব integration test — আসল FastAPI app, আসল router stack, আসল DB, আসল bcrypt,
আসল JWT। **authorization লেয়ার কোথাও mock করা হয়নি** — করলে আইসোলেশন টেস্ট
কিছুই প্রমাণ করত না।

### ৯টা attack scenario — সবগুলোই নিরাপদে ব্যর্থ ✅

| # | আক্রমণ | ফল |
|---|---|---|
| 1 | URL-এ `tenant_id` বদলে দেওয়া (৬টা endpoint) | ৪০৪, ডেটা লিক নেই |
| 2 | অন্য টেন্যান্টের আসল call id দিয়ে ট্রান্সক্রিপ্ট | ৪০৪, `"secret medical detail"` অনুপস্থিত |
| 3 | body-তে `tenant_id`/`owner_id` ইনজেক্ট | উপেক্ষিত; row নিজের টেন্যান্টেই |
| 4 | নিজের রোল owner বানানো / admin→owner প্রমোশন | ৪০৩, DB-তে রোল অপরিবর্তিত |
| 5 | অন্য টেন্যান্টের কনফিগ PATCH | ৪০৪, তাদের `llm_preset` অটুট |
| 6 | viewer লিড যোগ / DNC / IVR লেখা | তিনটাই ৪০৩ |
| 7 | agent টিম লিস্ট/তৈরি/ডিঅ্যাক্টিভেট/অডিট | চারটাই ৪০৩ |
| 8 | tenant A-র admin, tenant B-র ইউজার ম্যানেজ | ৪০৪, ভিকটিম অপরিবর্তিত |
| 9 | ১৯টা endpoint-এ anonymous access | সবগুলোই ৪০১/৪০৩ |

### Lint

নতুন ফাইলগুলোয় auto-fixable সব ঠিক করা। বাকি ৪১টা `B008`
(`Depends()` ডিফল্ট আর্গুমেন্টে) — FastAPI-র নিজস্ব প্যাটার্ন, false positive।
পুরনো ৯৩টা repo-wide finding-এ হাত দিইনি।

---

## ১১. ফ্রন্টএন্ড

- `Login.jsx` — email/password, লোডিং স্টেট, সার্ভারের **generic** এরর মেসেজই
  দেখায় (UI-তে enumeration ফিরিয়ে আনি না)
- `api.js` — টোকেন শুধু module-level ভেরিয়েবলে। **`localStorage` নয়,
  `sessionStorage` নয়, পড়া যায় এমন কুকিও নয়।** 401 পেলে একবার auto-refresh
  করে রিকোয়েস্ট রিপ্লে করে; না পারলে লগইনে ফেরত
- `App.jsx` — protected route, current user + রোল হেডারে, Sign out,
  পারমিশন অনুযায়ী সেকশন লুকানো (শুধু UX; আসল প্রয়োগ সার্ভারে)
- পেজ রিফ্রেশে HttpOnly কুকি দিয়ে সেশন ফিরে আসে

---

## ১২. যা এখনো সীমাবদ্ধতা (সৎভাবে)

1. **সেলফ-সার্ভিস টেন্যান্ট সাইনআপ নেই।** `tenant:create`/`tenant:delete`
   কোনো রোলই পায় না, owner-ও না। প্রোভিশনিং অপারেটরের স্ক্রিপ্ট
   (`seed_demo_tenant` + `create_owner`)। প্ল্যাটফর্ম-অ্যাডমিন প্লেন
   ইচ্ছাকৃতভাবে এই ধাপের বাইরে।
2. **পাসওয়ার্ড রিসেট ফ্লো নেই** — ইমেইল ডেলিভারি লাগবে, VoxDesk-এ এখনো নেই।
3. **MFA নেই।** স্কিমা (`token_version`, অডিট লগ) প্রস্তুত।
4. **লগইন রেট-লিমিট per-account, per-IP নয়** — অনেক অ্যাকাউন্টে ছড়ানো
   distributed spray আটকাবে না। প্রোডাকশনে সামনে WAF/proxy limiter দরকার।
5. **`APP_ENV=development`-এ Twilio সিগনেচার চেক বাইপাস হয়** — পুরনো সুবিধা,
   এই ধাপে বদলাইনি। প্রোডাকশন কখনো dev ফ্ল্যাগে চলবে না।
6. `pipecat`/`asyncpg`/`alembic` এই স্যান্ডবক্সে ইনস্টল নেই, তাই মাইগ্রেশনটা
   **আসল Postgres-এ চালিয়ে দেখা হয়নি** — সিনট্যাক্স আর মডেল-প্যারিটি যাচাই
   করা হয়েছে, কিন্তু আপনাকে একবার `make migrate` চালিয়ে নিশ্চিত হতে হবে।

---

## ১৩. আপনার পরের কমান্ড

```bash
cd voxdesk
sh .devsetup.sh
make migrate
python -m scripts.seed_demo_tenant
python -m scripts.create_owner --tenant-id <uuid> --email you@example.com
make test          # 259 passed
make dev
```

পরের ধাপের জন্য বাকি: `transfer.py` → `pipeline.py` ওয়্যারিং,
`seed_demo_tenant.py`-তে owner তৈরি যোগ, ড্যাশবোর্ডে AI-switch ড্রপডাউন,
Fiverr gig + Upwork প্রোফাইল কপি, আর **আপনার CV রিরাইট** (এখনো বাকি আছে)।