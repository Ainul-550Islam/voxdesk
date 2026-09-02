# STEP 7 — প্রোডাকশন বিলিং, Stripe ও usage metering

**অবস্থা: সম্পূর্ণ। ১৭৬২ টেস্ট পাস, ০ ফেল।** (STEP 6 বেসলাইন ১৪৮১ — **২৮১টি নতুন**)

---

## ০. আগে যা বলা দরকার

**একটাও Stripe API কল আসল credentials দিয়ে করা হয়নি — test-mode-ও নয়।**
§১৯-এ সৎ তালিকা।

---

## ১. অডিট: দুটো বাগ যা টাকা হারাচ্ছিল ও অ্যাকাউন্ট নষ্ট করছিল

সম্পূর্ণ অডিট → **[`docs/BILLING-AUDIT.md`](docs/BILLING-AUDIT.md)** (১১টি finding)।
প্রতিটি দাবি **চালিয়ে** যাচাই করা, পড়ে অনুমান নয়।

### F1 — একই কল webhook ক্রম অনুযায়ী ভিন্ন পরিমাণ বিল করত

পুরনো কোড ছিল `minutes_used += duration - previous_duration` — আগের কোনো
callback যা stamp করেছে তা বিয়োগ করত। মধ্যবর্তী callback stamp হতো কিন্তু বিল
হতো না, তাই তার সেকেন্ডগুলো উবে যেত। আসল `call_state` মডিউলের বিরুদ্ধে চালিয়ে,
একটা ১২০-সেকেন্ডের কল:

```
completed(120s) মাত্র                  -> 2.00 min  ✅
in-progress(0s), completed(120s)       -> 2.00 min  ✅
in-progress(60s), completed(120s)      -> 1.00 min  ❌  ৫০% কম
answered(90s), completed(120s)         -> 0.50 min  ❌  ৭৫% কম
completed(120s) x10                    -> 2.00 min  ✅
```

Duplicate সুরক্ষা কাজ করত; **ক্রম** করত না। Twilio সত্যিই `in-progress` ও
`answered` পাঠায়, আর transferred কলে দুটো leg এলোমেলো ক্রমে রিপোর্ট করে।
ভুলের দিক ছিল **কম বিল করা**, তাই কেউ অভিযোগ করেনি এবং এটা কখনো ধরা পড়েনি।

### F2 — `minutes_used` কখনো রিসেট হতো না

```
grep -rn "minutes_used" app scripts  ->  ৪টি hit:
  models.py:212          কলাম
  routes.py:69           response field
  twilio_handler.py:69   cap চেক
  twilio_handler.py:313  increment
```

কোনো scheduler job নেই, কোনো period rollover নেই। এটা একটা **আজীবন** কাউন্টার
যা একটা **মাসিক** ভাতার সাথে তুলনা করা হতো। ৫০০ মিনিটের tenant চিরকালের জন্য
৭৫০ মিনিট পেত, তারপর প্রতিটা inbound কলে *"This account has reached its usage
limit"* — স্থায়ীভাবে, ম্যানুয়াল `UPDATE` ছাড়া ফেরার পথ নেই।

বাকিগুলো: `minutes_used` display counter হয়েও authority (F3), billing period
নেই (F4), plan catalogue নেই (F5), শুধু voice মিটার হয় (F6), `×1.5` cap-ই
একমাত্র entitlement (F7), `BILLING_*` permission ঘোষিত কিন্তু অব্যবহৃত (F8),
enforcement voice loop-এর ভিতরে (F9), **inbound কল ব্লক করত** (F10)।

---

## ২. ফাইল যোগ (১৭টি)

**`app/billing/` (১৩ মডিউল):** `__init__.py` · `errors.py` · `periods.py` ·
`plans.py` · `metering.py` · `entitlements.py` · `base.py` · `registry.py` ·
`service.py` · `webhooks.py` · `reconciliation.py` · `hooks.py` ·
`providers/{__init__,stripe,manual}.py`

**API:** `app/api/billing_routes.py` · **Migration:** `alembic/versions/0008_billing.py`

**টেস্ট (৪):** `test_billing_metering.py` · `test_billing_voice.py` ·
`test_billing_subscriptions.py` · `test_billing_api.py`

**ডক:** `docs/BILLING.md` · `docs/BILLING-AUDIT.md`

## ৩. ফাইল পরিবর্তন (৮টি)

`app/db/models.py` (৬ টেবিল + ৬ enum + ৯ AuditAction) · `app/core/config.py`
(৯ সেটিং + ৪ boot guard) · `app/auth/rbac.py` (admin → `BILLING_READ`) ·
`app/main.py` (router + plan seeding + config check) · `scripts/scheduler.py`
(reconciliation loop) · `app/telephony/twilio_handler.py` (**F1 ও F2 ফিক্স**) ·
`tests/conftest.py` · `tests/test_enum_consistency.py` (+১৫) · `README.md`

## ৪. Migration

**`0008_billing`** (← `0007_calendar_scheduling`)। ৬ টেবিল, ৬ enum,
`auditaction`-এ ৯ member, **৭টি unique constraint**, ১৮ index।

সম্পূর্ণ additive। `tenants`-এর চারটে পুরনো কলাম **ইচ্ছাকৃতভাবে রাখা** —
`minutes_used` authority থেকে **cache**-এ নামানো হয়েছে (dashboard পড়ে), আর
`tenants.plan` সক্রিয়ভাবে ব্যবহৃত: এটাই সেই fallback যা STEP 7-এর আগের
`plan="pro"` tenant-কে তার কেনা Pro entitlement ধরে রাখতে দেয় (requirement 25)।

**কোনো plan row migration-এ insert করা হয়নি** — দাম ব্যবসায়িক ডেটা যা বদলায়;
repricing চলমান সিস্টেমে operator-এর কাজ, schema change + deploy নয়।

---

## ৫. Plan ও entitlement মডেল

| প্ল্যান | মাসিক | ভয়েস | SMS | Overage/min | Overage? |
|---|--:|--:|--:|--:|:--:|
| Trial | $0 | 60 | 50 | — | **না** |
| Starter | $199 | 500 | 500 | ১২¢ | হ্যাঁ |
| Pro | $499 | 2,000 | 2,500 | ১০¢ | হ্যাঁ |
| Enterprise | $1,499 | 10,000 | 10,000 | ৮¢ | হ্যাঁ |

রেট STEP 4-এর market research থেকে: infrastructure-এ all-in ভয়েস মিনিট
$0.10–0.30, তাই এর নিচে বিক্রি মানে লোকসান।

Non-metered entitlement JSON-এ, write-time allowlist validation সহ। `-1` মানে
unlimited। **কোথাও `if tenant.plan == "pro"` নেই।**

### তিনটে ইচ্ছাকৃত নরম দিক

**`PAST_DUE`-তেও সেবা চলে।** Stripe এখনো কার্ড retry করছে; প্রথম ব্যর্থ
চার্জেই ফোন লাইন কেটে দেওয়া কয়েক দিনের সেবার চেয়ে অনেক বেশি সদিচ্ছা নষ্ট করে।

**Subscription না থাকলেও ব্লক নয়।** STEP 7-এর আগের প্রতিটা tenant এই
অবস্থায়। Fallback: `subscription.plan → tenant.plan → default plan`।

**Catalogue না থাকলে degrade-open।** কোনো plan resolve না হলে **allow** করে
আর `billing.no_plan_resolved` error-level লগ করে। এটা *আমাদের* ব্যর্থতা;
এর শাস্তি গ্রাহককে দেওয়া ভুল।

### Inbound কল কখনো ব্লক হয় না

F10-এর ফিক্স। যে গ্রাহককে কেটে দেওয়া হচ্ছে সে ঋণী গ্রাহক নয় — ডেন্টিস্টের
রোগীর ফোন কেটে দিলে শাস্তি পায় সেই পক্ষ যার সমাধানের কোনো উপায় নেই।
Entitlement সেখানেই প্রয়োগ হয় যেখানে খরচ শুরু হয়: outbound কল ও
account-level feature। `tenant.is_active` ইচ্ছাকৃত off switch হিসেবে রইল।

---

## ৬. Stripe আর্কিটেকচার

REST-এর বিপরীতে লেখা, SDK নয়: `stripe` প্যাকেজ synchronous আর এই codebase
পুরোটা async; আর requirement 5 বলে core logic যেন Stripe SDK class-এর উপর
নির্ভর না করে — যা importable না হলে নিশ্চিত করা অনেক সহজ।

**সব Stripe-আকৃতির জিনিস ওই ফাইলেই থামে।** Status string, form encoding,
`data` envelope — কিছুই বাইরে যায় না; যা যায় তা `RemoteSubscription` ইত্যাদি,
enum member সহ।

### দুটো version-drift প্রতিরক্ষা

**Period bounds সরে গেছে** — Stripe ২০২৫-এর এক API version-এ
`current_period_start/_end` subscription থেকে item-এ নিয়ে গেছে। Adapter root
পড়ে, না পেলে প্রথম item-এ যায়; API version per-account, আমরা নিয়ন্ত্রণ করি না।

**অচেনা status → `INCOMPLETE`** — সেবা দেয় না, কাউকে cancel-ও করে না।

`cancel_at_period_end` + `active` → আমাদের **`CANCELING`**। Stripe এটা
boolean-এ প্রকাশ করে, যা প্রতিটা UI-এর দরকারি পার্থক্য হারায়।

---

## ৭. Customer ও subscription lifecycle

**Customer** — প্রতি tenant একটাই। ক্রম: (১) locally linked? ফেরত দাও,
provider কল-ই নেই। (২) provider-কে জিজ্ঞেস করো এই tenant-এর customer আছে কিনা।
(৩) তবেই তৈরি করো। **Timeout-এ retry নয়, আবার জিজ্ঞেস** — timeout মানে
হয়তো সফল হয়েছে।

**Checkout** — শুধু plan code ও interval। **Session তৈরি কিছুই প্রতিষ্ঠা করে
না**; local row `INCOMPLETE` থাকে। এমনকি `checkout.session.completed`-ও session
body থেকে `ACTIVE` সেট করে না: session বলে payment page শেষ হয়েছে,
*subscription* object বলে সে কোন অবস্থায় — 3DS বা ব্যর্থ প্রথম চার্জে দুটো ভিন্ন।

**Plan change** — **upgrade তাৎক্ষণিক** (`create_prorations`), **downgrade
period-end-এ scheduled** (proration নেই, refund নেই)। তাৎক্ষণিক downgrade
এমন গ্রাহককে আটকে দিত যে ইতিমধ্যে ছোট প্ল্যানের চেয়ে বেশি ব্যবহার করেছেন।

**Cancel** — ডিফল্ট period-end। গ্রাহক মাসের টাকা দিয়েছেন; ক্লিকের মুহূর্তে
কেটে দেওয়া মানে না-দেওয়া সেবার টাকা নেওয়া। Idempotent।

---

## ৮. Usage event ডিজাইন

**Immutable, append-only, idempotency-keyed।** সংশোধন = নতুন row, negative
quantity — requirement 35, এবং disputed invoice-এর একমাত্র উত্তরযোগ্য রূপ।

**smallest unit-এ integer** — voice সেকেন্ডে। কয়েক হাজার float যোগ করলে মোট
তার অংশগুলোর যোগফলের সমান থাকে না, আর যে invoice নিজের line item-এর সাথে মেলে
না তা রক্ষা করা যায় না।

```
voice_minute:3f2a1b4c-...
llm_token:3f2a1b4c-...:turn:7
```

Derived, generated নয়। `UNIQUE (tenant_id, idempotency_key)` গ্যারান্টি;
আগের `SELECT` শুধু optimization।

## ৯. Voice-minute billing নীতি

**এক কল = এক event, call id-তে keyed, finalization-এ একবার, চূড়ান্ত পরম
duration থেকে।** ক্রম-নির্ভরতা সম্পূর্ণ দূর।

**Transfer: গ্রাহক-দৃশ্যমান মোট duration, provider leg নয়।** Transferred কল
গ্রাহকের দৃষ্টিতে একটাই কল, invoice-এ একটাই লাইন। Leg আলাদা বিল করলে প্রতিটা
escalated কল প্রায় দ্বিগুণ হতো।

## ১০. Overage হিসাব

```
included 500 min, used 620 min -> overage 120 min x ১০¢ = $12.00
```

**Rounding নীতি:** duration প্রতি কলে **floor** হয় (উপরে round করলে হাজারো
কলে পদ্ধতিগত অতিরিক্ত চার্জ), আর overage **period-এর মোটে একবার** উপরে round
হয়। দুইশো ২০-সেকেন্ডের কল = **৬৭ বিলযোগ্য মিনিট, ২০০ নয়** — per-call
rounding টেলিফোনি বিলিং বিরোধের সবচেয়ে সাধারণ কারণ।

---

## ১১. Webhook নিরাপত্তা

**Raw bytes-এ verify, সবার আগে।** Route `await request.body()` পড়ে, কখনো
`request.json()` নয় — re-serialize করলে byte বদলায় এবং প্রতিটা signature check
অর্থহীন হয়ে যায়। Source-এর বিপরীতে টেস্ট আছে।

* signed payload `{t}.{raw_body}`, HMAC-SHA256 hex, `whsec_` key
* **প্রতিটা `v1` চেক করা হয়** — rotation-এ Stripe একাধিক পাঠায়; শুধু প্রথমটা
  নিলে পুরো rotation window-এর বৈধ event হারাতাম
* অচেনা scheme (`v0`) উপেক্ষা, ব্যর্থতা নয়
* timestamp signed payload-এর ভিতরে, ৩০০s-এর বাইরে reject — replay বন্ধ
* `compare_digest`, কারণ `==` timing দিয়ে prefix ফাঁস করে

**400 ফেরে, 401 নয়:** Stripe 4xx-কে স্থায়ী ধরে retry বন্ধ করে, যা কখনো
verify হবে না এমন request-এর জন্য সঠিক।

## ১২. Idempotency কৌশল

| স্তর | প্রক্রিয়া |
|---|---|
| Usage | derived key + `UNIQUE (tenant_id, idempotency_key)` |
| Webhook | `UNIQUE (provider, provider_event_id)` |
| Stripe-এর দিকে | প্রতিটা mutating কলে `Idempotency-Key` |
| Customer | timeout-এ reconcile, retry নয় |
| Subscription | `list_subscriptions` দিয়ে হারানো subscription দত্তক নেওয়া |
| Invoice | external id-তে idempotent mirroring |
| Ordering | provider timestamp তুলনা; বাসি event প্রত্যাখ্যাত |

## ১৩. Reconciliation

`reconcile_tenant()` **রিপোর্ট করে, ঠিক করে না**। একমাত্র mutation
`rebuild_summaries` — যা immutable event থেকে একটা **cache** পুনর্গণনা করে।

পাঁচ শ্রেণির discrepancy: `summary_drift` · `negative_usage` (financial —
দুইবার credit) · `orphan_usage` · `cross_tenant_usage` (financial — একজনের কল
আরেকজনকে চার্জ) · `period_mismatch`।

**Finalized period রিপোর্ট হয় কিন্তু কখনো rebuild হয় না** — ওই সংখ্যাগুলোই
invoice করা হয়েছে; নীরবে বদলালে আমাদের আর গ্রাহকের হিসাব বিনা চিহ্নে আলাদা হয়ে যায়।

## ১৪. Tenant isolation ও ১৫. Permissions

| প্রক্রিয়া | কোথায় |
|---|---|
| প্রতিটা query verified JWT-র `tenant_id`-তে | কোনো route body থেকে tenant পড়ে না |
| `UNIQUE (tenant_id, provider)` | "tenant-এর subscription" সুসংজ্ঞায়িত |
| `UNIQUE (provider, external_subscription_id)` | webhook ভুল row-তে ম্যাচ করতে পারে না |
| `UNIQUE (tenant_id, idempotency_key)` | key namespace tenant-scoped |
| Portal-এ কোনো body নেই | অন্যের customer id দেওয়ার parameter-ই নেই |

`BILLING_READ` → owner, admin। `BILLING_WRITE` → **owner only**। Admin-এর
জানা দরকার কেন limit hit হলো; কোম্পানি কত চার্জ হবে তা শুধু owner বদলাতে পারে।

## ১৬. API

```
GET  /api/billing            GET  /api/billing/plans     GET  /api/billing/usage
GET  /api/billing/invoices   POST /api/billing/checkout   POST /api/billing/portal
POST /api/billing/cancel     POST /api/billing/change-plan
POST /api/billing/reconcile  POST /api/billing/webhook/{provider}
```

---

## ১৭. টেস্ট (২৮১টি নতুন)

| ফাইল | সংখ্যা | কভারেজ |
|---|--:|---|
| `test_billing_subscriptions.py` | ১১৭ | plan catalogue, customer, lifecycle, ordering, Stripe mapping, signature, webhook, **contract (registry-parameterized)** |
| `test_billing_metering.py` | ৫৬ | period arithmetic, recording, idempotency, **concurrency**, overage, summaries, adjustments |
| `test_billing_api.py` | ৫০ | API, permissions, **tenant isolation**, secret handling, webhook route, reconciliation |
| `test_billing_voice.py` | ৪৪ | **F1 regression**, transfer policy, entitlement, threshold, enforcement |
| `test_enum_consistency.py` | +১৫ | migration↔model parity (৫৯ → ৭৪) |

উল্লেখযোগ্য: F1 regression suite ছয়টা আসল callback ক্রম replay করে এবং প্রতিবার
১২০ সেকেন্ড দাবি করে; concurrency টেস্ট আলাদা DB connection-এ পাঁচটা genuine
concurrent worker চালিয়ে ঠিক একটা billable row দাবি করে।

## ১৮. যে ৫টা আসল বাগ ধরা পড়ল

1. **`session.add()` savepoint-এর বাইরে থাকলে race হারা worker-এর session
   অব্যবহারযোগ্য হয়ে যেত।** Rollback অবজেক্টটা expunge করে না, সেটা pending
   থেকে পরের statement-এ `PendingRollbackError` ঘটায় — মানে হারা worker কে
   জিতল তা পড়তেও পারত না। মেপে দেখা: তিনটার দুটো worker আটকে যেত।
   `metering.record_usage` ও `service._ensure_row` দুই জায়গাতেই ছিল।
2. **`_apply_remote` ভুল field নাম পড়ত** — `remote.external_subscription_id`,
   কিন্তু dataclass-এ আছে `external_id`। প্রতিটা state application-এ
   `AttributeError`; ১২টা টেস্ট ধরেছে।
3. **Overage দুইবার ভাগ হতো** — `compute_overage` ইতিমধ্যেই মিনিট ফেরায়,
   তারপর `display()` আবার ৬০ দিয়ে ভাগ করত। ১০০ মিনিটের overage API-তে
   **১.৬৭** দেখাত।
4. **OAuth-এর মতোই: `find_customer_by_tenant` না থাকলে timeout recovery
   অসম্ভব** — contract টেস্ট প্রতিটা provider-কে বাধ্য করে।
5. **Autouse fixture পুরো suite ৬৮s → ১৫৬s করে দিয়েছিল** — প্রতিটা pure-unit
   টেস্টকে DB engine বানাতে বাধ্য করছিল। এটা প্রোডাকশন বাগ নয়, কিন্তু এর ফিক্স
   একটা প্রোডাকশন প্রশ্ন তুলেছিল: catalogue না থাকলে entitlement কী করবে?
   উত্তর: **degrade-open**, কারণ সেটা আমাদের ব্যর্থতা।

## ১৯. যা live যাচাই করা হয়নি

**একটাও Stripe API কল করা হয়নি, কোনো credential দিয়েই।** Adapter published
REST contract-এর বিপরীতে লেখা এবং `httpx`-এ intercept করা scripted transport-এ
চালানো — header assembly, form encoding, idempotency key, status
classification, timeout handling সবই আসল কোড পথ, কিন্তু একটা byte-ও মেশিন
ছাড়েনি।

যাচাই হয়নি: **প্রতিটা Stripe endpoint** (`/customers`, `/customers/search`,
`/subscriptions` create/update/cancel/list, `/invoices`, `/checkout/sessions`,
`/billing_portal/sessions`, `/prices`) · **`Idempotency-Key`-এর আসল আচরণ**
বাস্তব retry-তে · **আসল webhook delivery**, retry ও out-of-order আগমন ·
**Proration** একটা live upgrade-এ · **3DS / `default_incomplete`** flow ·
**PostgreSQL** (সব টেস্ট SQLite-এ; `alembic upgrade head` চালানো হয়নি)।

যা যাচাই *হয়েছে*: দুই provider-এর payload construction, response
normalization, status mapping (`cancel_at_period_end` special case ও ২০২৫-এর
period-field স্থানান্তর সহ), error classification, webhook signature
(valid/wrong secret/tampered/re-serialized/stale/future/rotation/unknown
scheme/malformed), webhook dedupe ও out-of-order, **আলাদা DB connection-এ
genuine concurrency**, overage arithmetic, period boundary, entitlement,
tenant isolation, secret containment, পুরো API।

## ২০. বাকি সীমাবদ্ধতা

1. **Stripe metered-billing integration নেই** — overage locally গণনা ও API-তে
   দেখানো হয়, Stripe-এ usage record হিসেবে push হয় না।
2. **নিজস্ব proration arithmetic নেই** — upgrade Stripe-এর উপর ছাড়া, downgrade
   scheduled। ইচ্ছাকৃত: requirement 18 অস্পষ্ট proration নিষেধ করে।
3. **Provider-এর বাইরে dunning নেই** — `PAST_DUE` রেকর্ড হয়, সেবা চলে; কোনো
   escalation schedule বা suspension নেই।
4. **Tax handling নেই।**
5. **Refund হলো reference, operation নয়** — `record_adjustment` *usage* সংশোধন
   করে; আসল refund Stripe dashboard-এ।
6. **Period finalization observed renewal-এ ট্রিগার হয়** — webhook বন্ধ হলে
   period খোলা থাকে reconcile না চলা পর্যন্ত।
7. **`Tenant.minutes_used`/`included_minutes` এখনো আছে** cache ও legacy
   fallback হিসেবে। সরানো আলাদা ঘোষিত পরিবর্তন।
8. **Reconciliation নিজের রেকর্ড নিজের সাথে মেলায়** — Stripe-এর মোটের সাথে নয়;
   সেটার জন্য (১) দরকার।

## ২১. চূড়ান্ত যাচাই

```
python3 -m compileall app scripts alembic tests   → OK
python3 -m ruff check <সব নতুন ফাইল>              → All checks passed
python3 -m pytest tests/ -q                       → 1762 passed, 30 skipped, 0 failed (84s)
নতুন মডিউল import                                  → 10/10 clean
migration↔model parity (6 enum, 6 টেবিল, 93 কলাম)  → PARITY CLEAN
production boot guard (৮ পরিস্থিতি)                → সবগুলো সঠিক
```

Ruff-এ ৮টা pre-existing warning `routes.py`/`vectorstore.py`/`outbound.py`-তে —
STEP 5 ও 6-এও ছিল, "unrelated ফাইল rewrite কোরো না" মেনে ছুঁইনি।

**STEP 1–6-এর কিছুই ভাঙেনি।** ১৪৮১ পূর্ববর্তী টেস্ট অপরিবর্তিতভাবে পাস করে।

