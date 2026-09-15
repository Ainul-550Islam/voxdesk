# STEP 5 — প্রোডাকশন-গ্রেড, provider-agnostic CRM ইন্টিগ্রেশন লেয়ার

**অবস্থা: সম্পূর্ণ। ১০৪১ টেস্ট পাস, ০ ফেল।** (STEP 4 বেসলাইন ছিল ৬৯০ — **৩৫১টি নতুন**)

---

## ০. প্রথমে একটা জরুরি কথা

**কোনো provider API আসল credentials দিয়ে live টেস্ট করা হয়নি।** GoHighLevel,
HubSpot, Jobber — কারো অ্যাকাউন্ট বা টোকেন আমার কাছে নেই। §১৪-এ ঠিক কী যাচাই
হয়েছে আর কী হয়নি তার সৎ তালিকা আছে। এটা রিপোর্টের শেষে লুকিয়ে রাখিনি, শুরুতেই
বলছি, কারণ "CRM ইন্টিগ্রেশন কাজ করে" আর "CRM ইন্টিগ্রেশন লেখা হয়েছে" এক জিনিস নয়।

---

## ১. অডিট: আগে কী ছিল (কিছু বদলানোর আগে)

সম্পূর্ণ অডিট → **[`docs/CRM-AUDIT.md`](docs/CRM-AUDIT.md)** (১৭টি finding)।

আগের "CRM" ছিল **একটাই ফাইল, ১৩০ লাইন** — `app/integrations/crm.py`। সবচেয়ে
গুরুতর যা পেলাম:

| # | সমস্যা | কেন গুরুতর |
|---|---|---|
| ১ | **এটা CRM ইন্টিগ্রেশন নয়, একটা webhook POST।** সব provider-এর জন্যই tenant-এর দেওয়া URL-এ পোস্ট হতো | কোনো provider API base URL কোডে ছিলই না |
| ২ | **"GoHighLevel সাপোর্ট" কখনো GoHighLevel-কে কল করেনি** | `to_gohighlevel()` সঠিক GHL payload বানিয়ে সেটা tenant-এর webhook URL-এ পাঠাত |
| ৩ | **HubSpot-এর header ছিল, mapping ছিল না** | VoxDesk-এর internal event shape পাঠানো হতো; HubSpot reject করত |
| ৫ | **Credentials plaintext** — `Tenant.crm_api_key: String(255)` | DB read আছে এমন যে কেউ সব tenant-এর টোকেন পেত |
| ৭ | **"Retry" কোনো অপেক্ষা করত না** — `for attempt in range(3)`, কোনো sleep নেই | 429-এর সবচেয়ে খারাপ সম্ভাব্য উত্তর: মাইক্রোসেকেন্ডে তিনটা রিকোয়েস্ট |
| ৮ | **ব্যর্থতা কোথাও লেখা হতো না** | ফেল করা sync নীরবে হারিয়ে যেত। ম্যানুয়াল retry অসম্ভব |
| ৯ | **ডুপ্লিকেট গার্ড ইভেন্ট হারাত** — POST-এর *আগেই* `crm_synced = True` commit | দুইয়ের মাঝে প্রসেস মরলে কলটা চিরতরে "synced" চিহ্নিত, কখনো পাঠানো হয়নি |
| ১১ | `asyncio.create_task(...)` — reference রাখা হয়নি | CPython মাঝপথে GC করতে পারে |
| ১২ | **একটাই ইভেন্ট টাইপ** — `"call.completed"` হার্ডকোড | missed call নেই, lead নেই, appointment নেই, transfer নেই |
| ১৫ | **কোনো signing নেই** — HMAC নেই, timestamp নেই, replay protection নেই | receiver যাচাই করতে পারত না POST-টা VoxDesk থেকে এসেছে কিনা |

স্পেকের প্রশ্নগুলোর সরাসরি উত্তর:
**credentials আছে?** হ্যাঁ, plaintext। **retry আছে?** নামমাত্র। **duplicate সম্ভব?**
CRM-এ হ্যাঁ। **tenant isolation আছে?** আকস্মিকভাবে — isolate করার মতো কিছুই ছিল না।

---

## ২. ফাইল যোগ করা হয়েছে (২৩টি)

**CRM প্যাকেজ (`app/integrations/crm/`, ১৩টি মডিউল):**
`__init__.py` · `models.py` · `base.py` · `errors.py` · `crypto.py` ·
`mapping.py` · `events.py` · `service.py` · `retry.py` · `registry.py` ·
`hooks.py` · `legacy.py` · `providers/{__init__,ghl,hubspot,jobber,webhook}.py`

**API:** `app/api/integration_routes.py` · `app/api/crm_webhook_routes.py`

**Migration:** `alembic/versions/0006_crm_integrations.py`

**টেস্ট (৭টি ফাইল):** `test_crm_contract.py` · `test_crm_credentials.py` ·
`test_crm_idempotency.py` · `test_crm_mapping.py` · `test_crm_providers.py` ·
`test_crm_tenant_isolation.py` · `test_crm_wiring.py`

**ডক:** `docs/CRM-INTEGRATIONS.md` · `docs/CRM-AUDIT.md`

## ৩. ফাইল পরিবর্তিত (১০টি)

`app/db/models.py` (৫ টেবিল + ৪ enum + ৪ AuditAction) · `app/core/config.py`
(১১ সেটিং + ২ boot guard) · `app/auth/permissions.py` (`INTEGRATION_SYNC`) ·
`app/auth/rbac.py` · `app/main.py` (২ router) · `scripts/scheduler.py`
(`crm_sync_loop`) · `app/telephony/twilio_handler.py` (call + transfer wiring) ·
`app/agent/functions.py` (appointment + DNC lead) · `app/api/routes.py`
(bulk lead import) · `tests/conftest.py` · `tests/test_enum_consistency.py` ·
`tests/test_availability.py` (FakeSession) · `README.md` · `requirements.txt`

`app/integrations/crm.py` → `app/integrations/crm/legacy.py` সরানো হয়েছে,
`__init__.py` থেকে re-export করা — **`tests/test_outbound.py`-এর ৬টি লিগ্যাসি
টেস্ট অপরিবর্তিতভাবে পাস করে**।

## ৪. Migration

**`0006_crm_integrations`** (← `0005_knowledge_rag`)। ৫ টেবিল, ৪ enum,
`auditaction`-এ ৪ member, ৫ unique constraint, ১৭ index।

সম্পূর্ণ additive। **পুরনো `tenants.crm_*` কলামগুলো ইচ্ছাকৃতভাবে রাখা হয়েছে** —
সেগুলোয় গ্রাহকের দেওয়া কনফিগ আছে, আর rolling deploy-এ আগের অ্যাপ ভার্সন তখনো
সেগুলো পড়ছে। ওগুলো ফেলা আলাদা data migration-এর কাজ, এই চেঞ্জের পাদটীকা নয়।

---

## ৫. Provider ও capability

| Capability | GHL | HubSpot | Jobber | Webhook |
|---|:--:|:--:|:--:|:--:|
| upsert / create / update contact | ✅ | ✅ | ✅¹ | ✅ / ✅ / — |
| get_contact | ✅ | ✅ | ✅ | — |
| note / activity | ✅ | ✅ | ✅ | ✅ |
| appointment (create / cancel) | ✅ / — | — | — | ✅ / ✅ |
| tag | ✅ | —² | — | ✅ |
| custom fields | ✅ | ✅ | — | ✅ |
| health check | ✅ | ✅ | ✅ | ✅ |

¹ **native upsert নয়** — `clientUpsert` Jobber-এর schema থেকে ২০২৩-০৮-১৮ ভার্সনে
**সরিয়ে ফেলা হয়েছে**, তাই query-then-create/edit।
² HubSpot-এ tag নেই। Capability ঘোষণা করে নীরবে ডেটা ফেলে দেওয়ার চেয়ে না থাকাই ভালো।

### যে তিনটা আসল ফাঁদ কোডে ধরা আছে

**GoHighLevel — `Version: 2021-07-28` header।** না দিলে error message payload-কে
দোষ দেয়, missing header-কে নয়। GHL ইন্টিগ্রেশনের সবচেয়ে সাধারণ ব্যর্থতা।
`/contacts/upsert`-এর প্রাপ্যতা নিয়ে সূত্রগুলো দ্বিমত করে, তাই **404 হলে
search-then-create-এ fallback** — নইলে কিছু tenant-এর সব sync ব্যাখ্যাহীনভাবে ফেল করত।

**HubSpot — ভয়েস প্রোডাক্টে email থাকেই না।** HubSpot email দিয়ে dedupe করে; phone
unique identifier নয়। শুধু email-upsert জানা adapter **প্রতিটা কলে নতুন contact
বানাত**। তাই: email থাকলে batch upsert, না থাকলে (বা portal `non-unique property
email` বলে reject করলে) phone-এ search → PATCH বা POST। **দ্বিতীয় পথটাই এখানে
স্বাভাবিক পথ, ব্যতিক্রম নয়।**

**Jobber — তিনটা ব্যর্থতাই HTTP 200-এ আসে।** `userErrors` মানে mutation *প্রত্যাখ্যাত*;
top-level `errors` মানে throttle বা schema error; null payload মানে কিছুই হয়নি।
শুধু status code দেখা adapter প্রতিটা rejection-কে success বলে রিপোর্ট করত — আর
service layer তখন external id ছাড়াই SYNCED লিখত। Note mutation-টা
`clientCreateNote`, `clientNoteCreate` নয় (দ্বিতীয়টাও সরানো হয়েছে)।

**Jobber-এ appointment sync নেই** — Jobber কাজকে job/visit হিসেবে মডেল করে, যার
জন্য property, line item ও schedule লাগে যা এই লেয়ার দিতে পারে না। অর্ধেক job
লেখার বদলে capability ঘোষণাই করা হয়নি; appointment ইভেন্ট একবার চেষ্টায়
`unsupported` permanent failure হয়, পাঁচবার retry নয়।

---

## ৬. Credential নিরাপত্তা

**AES-256-GCM**, envelope: `v1.<key_id>.<nonce>.<ciphertext>`

| বৈশিষ্ট্য | কেন |
|---|---|
| Authenticated (GCM) | tampered row decrypt-এ ফেল করে, garbage provider-এ যায় না |
| প্রতিবার নতুন ৯৬-বিট nonce | GCM-এ nonce পুনর্ব্যবহার catastrophic |
| Envelope-এ key id | rotation = "নতুন key যোগ করো", flag day নয় |
| **AAD = `(tenant_id, provider)`** | Tenant A-র ciphertext B-র row-তে কপি করলে decrypt হয় না |

শেষ পয়েন্টটা গুরুত্বপূর্ণ: naive column encryption-এর বিরুদ্ধে row-এর মধ্যে
ciphertext সরানো একটা বাস্তব অ্যাটাক। AAD সেটা বন্ধ করে — **isolation গ্যারান্টি
cipher পর্যন্ত পৌঁছায়, শুধু `WHERE` clause-এ নয়।**

**Threat model সরাসরি বলছি:** এটা database dump-এর বিরুদ্ধে রক্ষা করে (চুরি হওয়া
backup, ভুল-কনফিগার করা replica, ORM logger)। যে attacker ইতিমধ্যে অ্যাপ্লিকেশন
প্রসেস চালাচ্ছে তার বিরুদ্ধে **করে না** — তার কাছে key আছে, সংজ্ঞা অনুযায়ী। HSM
বা remote KMS ছাড়া এটা বদলায় না, দুটোর কোনোটাই এই স্টেপের scope-এ নয়।

কোথাও যায় না: API response (allowlist model), লগ (`safe_message()` স্ক্রাব),
audit detail (শুধু ফিল্ডের *নাম*), JWT, `ProviderContext.__repr__`।
**401 response body পুরোপুরি বাদ** — টোকেন প্রতিধ্বনিত হওয়ার সবচেয়ে সম্ভাব্য জায়গা।

প্রোডাকশনে `CRM_ENCRYPTION_KEYS` **বাধ্যতামূলক** — না থাকলে বা malformed হলে অ্যাপ
boot করে না। ডেভেলপমেন্টে না দিলে চলে, কিন্তু credential store করতে গেলে **503,
কখনো plaintext fallback নয়**।

---

## ৭. ইভেন্ট আর্কিটেকচার

```
business fact  →  CrmEvent (caller-এর transaction-এ commit)
                    ↓
               CrmSync × N (প্রতি integration-এ একটা, কিছুই পাঠানো হয়নি)
                    ↓
               scheduler → service.process_sync → adapter → provider
                    ↓
               external_id + SYNCED (atomically)
```

মূল ধারণা: **রেকর্ড করা আর পাঠানো আলাদা।** ব্যবসায়িক ঘটনা একবার commit হয়, যে
জিনিসটা সেটা ঘটিয়েছে তার সাথে একই transaction-এ। "কল শেষ হলো" আর "CRM জানল"-এর
মাঝে প্রসেস মরলে **কিছুই হারায় না** — পুরনো কোডের ৯ নম্বর বাগের ঠিক উল্টো।

৭টি ইভেন্ট: `call.completed` · `call.missed` · `transfer.completed` ·
`lead.created` · `lead.updated` · `appointment.booked` · `appointment.cancelled`

`transfer.completed` আলাদা করে emit হয় কারণ "transfer করা হয়েছিল" আর "transfer
connect হয়েছে" দুটো ভিন্ন ঘটনা — যে CRM এদুটো মিলিয়ে ফেলে সে ব্যবসাকে বলে কেউ
গ্রাহকের সাথে কথা বলেছে, যখন কেউ বলেনি।

---

## ৮. Idempotency

Key **derived, generated নয়**: `call.completed:3f2a1b4c-...`

Deterministic (একই ঘটনা = চিরকাল একই key) এবং legible (লগে বা support query-তে
মানে বোঝা যায়)। Random uuid হলে প্রতিটা ডুপ্লিকেট নতুন ইভেন্ট হতো — ঠিক যে
ব্যর্থতাটা স্পেক বর্ণনা করেছে।

গ্যারান্টি হলো `UNIQUE (tenant_id, idempotency_key)`; আগের `SELECT`-টা শুধু
optimization, আর `IntegrityError` পথটা race সামলায়।

| ডুপ্লিকেটের উৎস | প্রতিরোধ |
|---|---|
| ডুপ্লিকেট provider webhook | `CrmWebhookReceipt` unique on `(tenant, provider, event_id)` |
| রিট্রাই করা Twilio callback | derived key → দ্বিতীয় `emit()` no-op |
| Worker restart মাঝপথে | reaper: `PROCESSING` → `FAILED`; খরচ হওয়া attempt গোনা থাকে |
| **Provider timeout, কিন্তু সে accept করে ফেলেছে** | upsert semantics + `CrmContactLink` external id মনে রাখে → retry update করে, create নয় |

শেষেরটা সবচেয়ে কঠিন কেসটা, এবং এর জন্য আলাদা টেস্ট আছে।

---

## ৯. Retry

Bounded exponential + **full jitter**: attempt *n* অপেক্ষা করে
`[0, min(base·2ⁿ⁻¹, cap)]`-এ uniform random সময়।

Full jitter কেন, "exponential + একটু noise" কেন নয়: প্রথমটা আসলেই thundering
herd ভাঙে। jitter ছাড়া provider outage-এ একশো sync একসাথে ফেল করে ঠিক t+2s-এ
একশো retry পাঠায় — প্রতিটা ঢেউ overload-টা আবার তৈরি করে।

| রিট্রাই হয় | হয় না |
|---|---|
| timeout, connection reset, 429, 5xx | 401/403, malformed payload, unsupported, authorization denial |

**401 এক চেষ্টার পরেই permanent, পাঁচবার নয়** — revoked টোকেন নিজে নিজে ফিরে আসে না,
আর মৃত credential দিয়ে auth endpoint পেটালে অ্যাকাউন্ট lock হয়।

Provider-এর `Retry-After` আমাদের backoff-এর চেয়ে বড় হলে জেতে (rate limiter-এর
সাথে তর্ক করা মানে টোকেন suspend), ৩০০ সেকেন্ডে cap, unparseable হলে নিজের
schedule-এ fallback।

**Per-tenant token bucket** আলাদা: locally throttle হওয়া ব্যর্থতা নয় — sync
`PENDING`-এ ফিরে যায় এবং **attempt খরচ করে না**। উদ্দেশ্য ন্যায্যতা: একটা ভাঙা
tenant যেন পুরো worker pass গিলে না ফেলে।

---

## ১০. Tenant isolation

| প্রক্রিয়া | কোথায় |
|---|---|
| `UNIQUE (tenant_id, provider)` | জোড়াটাকে key বানায়, filter নয় |
| প্রতিটা lookup `(tenant_id, provider)` | `service.get_integration()` একমাত্র অনুমোদিত পথ |
| অন্যের row-তে 404, কখনো 403 | 403 নিশ্চিত করে দিত row-টা আছে |
| tenant আসে verified JWT থেকে | কোনো route body/query/header থেকে tenant পড়ে না |
| identity hash **tenant-salted** | দুই tenant-এর একই গ্রাহক ভিন্ন hash — `WHERE` ভুলে গেলেও join হয় না |
| AES-GCM AAD | credentials row-এর মধ্যে সরানো যায় না |
| **Adapter-এর হাতে কোনো DB handle নেই** | একটা `ProviderContext`, ব্যস — adapter অন্য tenant-এ পৌঁছাতে *পারে না*, গঠনগতভাবে |

শেষেরটা structurally assert করা: adapter-এর instance dict-এ ঠিক `{"context"}`
থাকতে হবে। এটা discipline নয়, construction।

---

## ১১. Call / Lead / Appointment wiring

| ট্রিগার | কোথায় | ইভেন্ট |
|---|---|---|
| কল terminal + COMPLETED | `twilio_handler` status callback | `call.completed` |
| কল terminal + NO_ANSWER/FAILED | একই callback | `call.missed` |
| মানুষ transfer ধরল | `/transfer-status` | `transfer.completed` |
| Appointment বুক | `functions.book_appointment` | `appointment.booked` |
| DNC অনুরোধ | `functions` | `lead.created` + `lead.updated` |
| Bulk lead import | `POST /tenants/{id}/leads` | `lead.created` × N |

সব hook: **কখনো raise করে না, কখনো পাঠায় না, কখনো commit করে না।**

পুরনো কোডে ছিল: commit → `crm_synced = True` → commit → unawaited
`create_task`। এখন ইভেন্টটা কলের চূড়ান্ত state-এর **একই commit-এ** — হয় দুটোই
থাকে, নয় কোনোটাই না।

**Transcript:** payload-এ যায় একটা *reference*
(`voxdesk:call:<id>:turns`), কখনো transcript নয়। কল transcript এই প্রোডাক্টের
সবচেয়ে সংবেদনশীল জিনিস — কার্ড নম্বর, রোগনির্ণয়, ঠিকানা। শুধু
`share_transcripts: true` থাকা integration-এ reference-টা পৌঁছায়।

---

## ১২. টেস্ট (৩৫১টি নতুন)

| ফাইল | সংখ্যা | কী কভার করে |
|---|--:|---|
| `test_crm_contract.py` | ১০০ | **registry-র উপর parameterized** — নতুন provider যোগ করলেই সাথে সাথে টেস্ট হয় |
| `test_crm_providers.py` | ৬৮ | চার adapter-এর payload mapping ও vendor quirk |
| `test_crm_wiring.py` | ৫১ | call/lead/appointment wiring, API, health, inbound webhook, structured logging |
| `test_crm_mapping.py` | ৪১ | field mapping, custom field validation, identity hashing |
| `test_crm_idempotency.py` | ৩৩ | idempotency, retry, rate limiting |
| `test_crm_credentials.py` | ২৬ | এনক্রিপশন, rotation, AAD binding, secret leakage |
| `test_crm_tenant_isolation.py` | ২০ | cross-tenant read/write/sync, injection, audit/log leakage |
| `test_enum_consistency.py` | +১২ | migration↔model parity (৩৭ → ৪৯) |

**Contract টেস্টগুলো registry-র উপর parameterized, হাতে লেখা তালিকার উপর নয়।**
আগামীকাল Salesforce adapter যোগ করলে register করা মাত্রই এই ১০০টা টেস্ট তাকে
tenant scoping, error normalization, timeout, idempotency ও secret handling-এর
জন্য পরীক্ষা করবে। ডকুমেন্টে চেকলিস্ট deadline-এর সাথে টেকে না; এটা টেকে।

---

## ১৩. যে ৫টা আসল বাগ টেস্ট লিখতে গিয়ে ধরা পড়ল

1. **`safe_message()` JSON body-তে ফেল করত।** Regex ছিল `\bkey\b\s*[:=]` — কিন্তু
   JSON-এ key quoted, তাই টেক্সট `"access_token":"v"`, নামের পরে একটা `"` আছে।
   ফলে scrubber shell-style `api_key=v` ধরত আর **প্রতিটা আসল provider response
   মিস করত** — যে কেসটার জন্যই সে বিদ্যমান। Contract টেস্ট ধরেছে।

2. **`Authorization: Bearer <token>`-এ "Bearer" রিড্যাক্ট হতো, টোকেন টিকে যেত।**
   regex non-overlapping, তাই "Bearer"-কে value হিসেবে খেয়ে ফেলায় আলাদা
   `bearer <token>` নিয়মটা সুযোগই পেত না।

3. **`sk_live_...` ধরা পড়ত না** — `sk_[A-Za-z0-9]{8,}` আন্ডারস্কোরে থেমে যেত,
   `live` মাত্র ৪ অক্ষর, length floor-এর নিচে।

4. **Rate limiter-এর প্রথম কল সবসময় `True` দিত**, burst যাই হোক। `burst=0`-তেও
   একটা টোকেন দিয়ে `tokens=-1` রেখে যেত — অর্থাৎ "কিছুই allow করো না" সেটিং
   (tenant, provider) প্রতি ঠিক একটা রিকোয়েস্ট allow করত।

5. **Inbound webhook-এর replay path 500 দিত, 200 নয়।** `except IntegrityError`
   ব্লকের ভিতরে `integration.tenant_id` পড়ছিলাম — `rollback()` সব object expire
   করে, তাই lazy refresh → `MissingGreenlet`, আর pending failed INSERT autoflush-এ
   আবার চলে গিয়ে IntegrityError দ্বিতীয়বার raise হতো, এবার uncaught।

চারটাই security বা correctness বাগ, এবং চারটাই টেস্ট থেকে এসেছে, review থেকে নয়।

---

## ১৪. যা live যাচাই করা হয়নি

**একটাও provider API আসল credentials দিয়ে ছোঁয়া হয়নি।** প্রতিটা adapter published
API contract অনুযায়ী লেখা এবং একটা scripted HTTP transport-এর বিরুদ্ধে চালানো
যেটা `httpx`-এ intercept করে — তাই header assembly, payload shape, status
classification, timeout handling সবই আসল কোড পথ, কিন্তু একটা byte-ও মেশিন ছাড়েনি।

- **GoHighLevel** — অ্যাকাউন্ট নেই, location id নেই, টোকেন নেই। বিশেষ করে
  `/contacts/upsert` প্রাপ্যতার প্রশ্নটা **অমীমাংসিত**; fallback-টা আছেই কারণ
  টেস্ট করে নিষ্পত্তি করা যায়নি।
- **HubSpot** — portal নেই। non-unique-email rejection ডকুমেন্টেশন ও community
  রিপোর্ট থেকে সামলানো, observed response থেকে নয়।
- **Jobber** — developer account নেই, OAuth app নেই। `userErrors` shape,
  throttle code, mutation নাম — সব published schema ও changelog থেকে।
- **Generic webhook** — কোনো live receiver নেই। Signature scheme নিজের reference
  implementation-এর বিরুদ্ধে যাচাই, যা internal consistency প্রমাণ করে,
  third-party interoperability নয়।
- **যেকোনো provider থেকে inbound webhook।**
- **PostgreSQL** — সব টেস্ট SQLite-এ। `alembic upgrade head` চালানো হয়নি;
  migration↔model parity static AST parsing-এ যাচাই।
- **OAuth token refresh** — Jobber-এর `refresh_token` field আছে, refresh flow নেই।

যা যাচাই *হয়েছে*: চার provider-এর serialization, error mapping, retry behaviour,
idempotency (timeout-after-acceptance সহ), tenant isolation, credential
encryption, পুরো configuration API।

---

## ১৫. বাকি সীমাবদ্ধতা

1. **Inbound webhook শুধু `webhook` provider-এ verify হয়।** GHL Ed25519 ব্যবহার
   করে, Jobber ও HubSpot-এর নিজস্ব scheme — কোনোটার key material এই স্টেপে
   provision করা হয়নি। তাই ওরা **fail closed** (401)। unverified inbound endpoint
   না থাকার চেয়েও খারাপ: ওটা একটা public, tenant-addressable write path।
2. **Inbound এখনো VoxDesk ডেটা বদলায় না** — verify + record করে। Bidirectional
   sync আলাদা ফিচার।
3. **Rate limiter process-local।** একটা scheduler প্রসেসের জন্য সঠিক; দ্বিতীয়টা
   যোগ করলে Redis লাগবে। ইন্টারফেস ছোট, backing store বদলালে caller ছুঁতে হয় না।
4. **Jobber-এ appointment sync নেই** (§৫)।
5. **HubSpot-এ tag নেই** — tenant field mapping দিয়ে property ব্যবহার করতে পারে।
6. **পুরনো `tenants.crm_*` কলাম এখনো আছে।** নতুন লেয়ার ওগুলো পড়ে না, কিন্তু
   বিদ্যমান tenant-দের `crm_integrations`-এ সরানোর data migration লেখা হয়নি।
7. **Key rotation ম্যানুয়াল** — `credentials_key_id` কলাম আছে তাই "কোন row এখনো
   পুরনো key-তে" একটা SQL প্রশ্ন, কিন্তু re-encrypt job লেখা হয়নি।

---

## ১৬. চূড়ান্ত যাচাই

```
python3 -m compileall app scripts alembic tests   → OK
python3 -m ruff check <সব নতুন ফাইল>              → All checks passed
python3 -m pytest tests/ -q                       → 1041 passed, 0 failed (৩৯s)
১৯টি নতুন মডিউল import                            → clean
migration↔model parity (৪ enum, ৫ টেবিল, ৬৬ কলাম) → PARITY CLEAN
production boot guard (৬ পরিস্থিতি)                → সবগুলো সঠিক
```

Ruff-এ ৮টা pre-existing warning আছে `routes.py`/`vectorstore.py`/`outbound.py`-তে —
ওগুলো আমার নয়, "unrelated ফাইল rewrite কোরো না" নির্দেশ মেনে ছুঁইনি। আমার তৈরি
একটা (`twilio_handler.py`-তে dead `asyncio` import) সরানো হয়েছে।

**STEP 1–4-এর কিছুই ভাঙেনি।** ৬৯০ পূর্ববর্তী টেস্ট অপরিবর্তিতভাবে পাস করে।