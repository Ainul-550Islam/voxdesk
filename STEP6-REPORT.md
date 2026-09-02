# STEP 6 — প্রোডাকশন-রেডি, provider-agnostic শিডিউলিং লেয়ার

**অবস্থা: সম্পূর্ণ। ১৪৮১ টেস্ট পাস, ০ ফেল।** (STEP 5 বেসলাইন ১০৪১ — **৪৪০টি নতুন**)

---

## ০. আগে যা বলা দরকার

**কোনো calendar provider আসল credentials দিয়ে live টেস্ট করা হয়নি।** Google,
Microsoft, Cal.com — কারো অ্যাকাউন্ট নেই। §১৭-এ সৎ তালিকা।

---

## ১. অডিট: দুটো বাগ যা প্রোডাক্টকে কলারের সাথে মিথ্যা বলাত

সম্পূর্ণ অডিট → **[`docs/CALENDAR-AUDIT.md`](docs/CALENDAR-AUDIT.md)** (১৮টি finding)।
নিচের প্রতিটি দাবি আমি **চালিয়ে যাচাই করেছি**, পড়ে নয়।

**F1 — ক্যালেন্ডার প্রত্যাখ্যান করলেও কলারকে "booked" বলা হতো।**
`create_event()` সব exception গিলে `None` ফেরায়। `book_appointment` রিটার্ন
ভ্যালু চেক করত না: `google_event_id=None` দিয়ে row লিখে বলত *"Booked for
Tuesday at 3."* চালিয়ে দেখানো:

```
create_event when the provider is unreachable -> None
=> কলারকে বলা হয় বুকিং হয়েছে; ক্যালেন্ডারে কিছুই নেই।
```

**F2 — ক্যালেন্ডার outage নীরবে "সব খালি"-তে রূপান্তরিত হতো।**
`list_busy()` যেকোনো exception-এ `[]` ফেরায়, আর সব caller `[]`-কে "কোনো
conflict নেই" ধরে। ব্যর্থতা আর শূন্যতা একই মান।

**F3 — আগামী ৪–৫ ঘণ্টার সব reminder নীরবে ফেলে দেওয়া হতো।** `reminders.py:49`
naive UTC ঘড়িকে appointment-এর timezone লেবেল দেয়। America/New_York-এ মাপা:
কোড ভাবে "now" বাস্তবের চেয়ে **৩:৫৯:৫৯ এগিয়ে**।

বাকিগুলো: VoxDesk-এর নিজের appointment-এর সাথে conflict চেক **নেই** (F4),
TOCTOU (F5), `AppointmentStatus` **নেই** — cancel/reschedule অসম্ভব (F6),
appointment-এর timezone সংরক্ষিত হয় না (F7), **LLM সব date arithmetic করে**
(F8), zero minimum notice (F9), duration = slot step (F10), সপ্তাহের সাত দিনই
একই open/close (F11), একটাই provider service-account দিয়ে (F13), error
normalization নেই (F16), retry নেই (F17), webhook নেই (F18)।

---

## ২. ফাইল যোগ (২১টি)

**`app/integrations/calendar/` (১৪ মডিউল):** `__init__.py` · `timezones.py` ·
`models.py` · `errors.py` · `base.py` · `policy.py` · `nlp.py` · `registry.py` ·
`service.py` · `tools.py` · `providers/{__init__,google,microsoft,calcom,internal}.py`

**API:** `app/api/appointment_routes.py` · `app/api/calendar_webhook_routes.py`

**Migration:** `alembic/versions/0007_calendar_scheduling.py`

**টেস্ট (৫):** `test_calendar_timezones.py` · `test_calendar_booking.py` ·
`test_calendar_contract.py` · `test_calendar_providers.py` · `test_calendar_api.py`

**ডক:** `docs/CALENDAR-INTEGRATIONS.md` · `docs/CALENDAR-AUDIT.md`

## ৩. ফাইল পরিবর্তন (৯টি)

`app/db/models.py` (Appointment-এ ১৮ কলাম + ২ enum + ৩ টেবিল + ৪ AuditAction) ·
`app/core/config.py` (৪ সেটিং) · `app/main.py` (৩ router) ·
`app/agent/functions.py` (dispatch রাউটিং + legacy arg alias) ·
`tests/conftest.py` (৩ fixture) · `tests/test_enum_consistency.py` (+১০) ·
`tests/test_availability.py` (১টা টেস্ট, নিচে ব্যাখ্যা) ·
`tests/test_rbac.py` (flaky টেস্ট ফিক্স) · `README.md`

`app/integrations/google_calendar.py` **অপরিবর্তিত** — `providers/internal.py`
সেটাকে wrap করে, তাই পুরনো tenant চলতে থাকে।

## ৪. Migration

**`0007_calendar_scheduling`** (← `0006_crm_integrations`)। Appointment-এ ১৮
কলাম, ৩ টেবিল, ২ enum, `auditaction`-এ ৪ member, ৫ unique constraint।

**কোনো ডেটা নষ্ট হয় না**, এবং দুটো backfill করা হয়েছে ভুল রেখে দেওয়ার বদলে:
`status` → বিদ্যমান সব row `CONFIRMED` (পুরনো কোড শুধু বিশ্বাসযোগ্য row-ই
লিখত; `PENDING` করলে গোটা বইটাই unconfirmed দেখাত), আর `timezone` → owning
tenant থেকে কপি (কলামের ডিফল্ট `UTC` রাখলে প্রতিটা ঐতিহাসিক appointment
নীরবে ৫ ঘণ্টা সরে যেত)।

---

## ৫. Provider ও capability

| Capability | Google | Microsoft | Cal.com | Internal | GSA |
|---|:--:|:--:|:--:|:--:|:--:|
| free_busy | ✅ | ✅ | —¹ | —² | ⚠️ |
| create / update / cancel / get | ✅ | ✅ | ✅/—³/✅/✅ | ✅ | ✅/—/—/— |
| reschedule | ✅ | ✅ | ✅ dedicated | ✅ | — |

¹ Cal.com bookable *slot* দেয়, busy block নয় — declare করা মানে অপারেশন আবিষ্কার করা।
² বাইরের ক্যালেন্ডার নেই; service আলাদাভাবে VoxDesk-এর নিজের appointment চেক করে।
³ Cal.com-এর PATCH বুকিং-এর সময় সরায় না।

### তিনটা vendor ফাঁদ কোডে ধরা

**Microsoft — timezone।** Graph ঐতিহাসিকভাবে *Windows* নাম বলে ("Pacific
Standard Time")। IANA↔Windows ম্যাপিং টেবিল shipping করা মানে DST-র দ্বিতীয়
সত্যের উৎস। তাই adapter **UTC পাঠায়** `timeZone: "UTC"` সহ; সব local rendering
`timezones.py`-তেই থাকে।

**Cal.com — HTTP 200-এ প্রত্যাখ্যান।** `status != "success"` মানে reject।
আর taken slot **400 আসে, 409 নয়** — সেটা conflict হিসেবে পুনর্শ্রেণিবদ্ধ করা হয়।

**Google — 403 আসলে throttle হতে পারে** (`rateLimitExceeded`), permission নয়।

### তিনটা idempotency primitive

Google-এর **client-supplied event id** (409 = আমাদেরই আগের চেষ্টা), Graph-এর
**`transactionId`**, Cal.com-এর **`metadata.voxdesk_key`**।

---

## ৬. Timezone কৌশল

**UTC-তে সংরক্ষণ। tenant-এর zone-এ যুক্তি। দুটো কখনো মেশানো নয়।**

`Appointment.timezone` আলাদা করে রাখা হয় কারণ tenant পরে জায়গা বদলালে বা ভুল
timezone শোধরালে বইয়ের প্রতিটা appointment নীরবে অন্য অর্থ পেয়ে যেত।

**Nonexistent time** — `zoneinfo` raise করে না, নীরবে ভিন্ন wall clock-এ
round-trip করে, যা error-এর চেয়েও খারাপ। round-trip করে ধরা হয়, আর আসল
transition boundary binary search-এ বের করা হয় যাতে প্রশ্নটা "২টা থেকে ৩টায়
লাফ দিই" বলতে পারে। Lord Howe-র **৩০ মিনিটের** shift-ও ধরা পড়ে।

**Ambiguous time** — দুটো candidate-ই অফার করা হয়, একটা নীরবে বাছা হয় না।

**Date arithmetic** — `+ timedelta(days=1)` ২৪ ঘণ্টা *অতিবাহিত সময়* যোগ করে,
spring-forward পেরোলে wall clock এক ঘণ্টা সরে যায়। টেস্টে প্রমাণিত: naive
পদ্ধতি ১৬:০০ দেয়, সঠিকটা ১৫:০০।

---

## ৭. Availability ও business hours

Business policy **provider availability নয়** — ডেন্টিস্টের Google ক্যালেন্ডার
রাত ৩টায় খালি থাকলেই রাত ৩টা bookable হয় না। দুটোই চেক হয়, সস্তা local
filter আগে।

Per-weekday **interval-এর তালিকা**, একটা open/close জোড়া নয় — **লাঞ্চ ব্রেক
হলো সবচেয়ে সাধারণ কারণ যাতে বুকিং এমন সময়ে পড়ে যখন কেউ নেই।** Appointment
**পুরোপুরি একটা interval-এর ভিতরে** ফিট করতে হবে।

Slot half-open, তাই ০৯:০০–০৯:৩০ আর ০৯:৩০–১০:০০ সংঘর্ষ করে না — closed
comparison প্রতিটা পরপর slot-কে সংঘর্ষ দেখাত এবং ক্ষমতা অর্ধেক করে দিত।

---

## ৮. Double-booking প্রতিরক্ষা

`service.book()`-এর ক্রমটাই ডিজাইন:

```
1. idempotency lookup   -- retry নীতিমালা পর্যন্ত পৌঁছায়ই না
2. policy               -- local, network নেই
3. slot reserve         -- PENDING row, unique slot_key = তালা
4. provider call        -- শুধু বিজয়ী এখানে আসে
5. confirm              -- external id + CONFIRMED একসাথে
```

৩ ও ৪-এর ক্রম গুরুত্বপূর্ণ। provider call-এর *পরে* reserve করলে দুই কলারই
event বানাত আর একজন persist-এ ব্যর্থ হয়ে ব্যবসার ক্যালেন্ডারে অনাথ entry
রেখে যেত।

Reservation **SAVEPOINT** ব্যবহার করে: রেস হারা মানে *reservation* ফেরানো,
caller-এর গোটা transaction নয়।

**টেস্টে প্রমাণিত:** আলাদা connection-এ ৫টা genuine concurrent session, ঠিক
একটা `CONFIRMED`, হারানোরা exception নয় — safe `CONFLICT` পায়।

## ৯. Idempotency

```
slot_key        = sha256(tenant | start_utc | end_utc)[:48]
idempotency_key = sha256(tenant | phone/email/request_id | start_utc)[:48]
```

দুটোই derived, generated নয়। প্রতি retry-তে নতুন UUID **duplicate protection
নয়, তার উল্টো** — তখন প্রতিটা retry নতুন বুকিং মনে হয়।

**Ambiguous timeout:**
```
timeout → find_event_by_key
          ├─ পাওয়া গেল  → সফল, ওর id রেকর্ড
          ├─ নেই        → ব্যর্থ
          └─ চেক নিজেই ব্যর্থ → মূল timeout re-raise
```
শেষ শাখাটাই গুরুত্বপূর্ণ। **"চেক করতে পারিনি" অনুপস্থিতির প্রমাণ নয়।**

---

## ১০. Reschedule ও cancel

**Reschedule** — provider গ্রহণ না করা পর্যন্ত row ছোঁয়া হয় না। ব্যর্থ হলে
appointment হুবহু আগের মতো থাকে এবং কলারকে বলা হয় মূল সময়টাই বহাল
(§17-এর শর্ত)। Cal.com-এর dedicated endpoint cancel-then-rebook-এর চেয়ে ভালো,
যেখানে দুটোর মাঝে ব্যর্থতা গ্রাহককে কিছুই না দিয়ে ছাড়ত।

**Cancel** — idempotent। দ্বিতীয় cancel Google-এ 404, Cal.com-এ 400; সেগুলো
দেখানো মানে নিরীহ double-click ভাঙা দেখানো। Provider cancel ব্যর্থ হলেও local
state `CANCELLED` হয় — গ্রাহক cancel বলেছেন; vendor API ধীর বলে সেটা রেকর্ড না
করা মানে slot আটকে রেখে পরের কলারকে ফিরিয়ে দেওয়া।

## ১১. Voice tool wiring

`check_availability` · `book_appointment` · `reschedule_appointment` ·
`cancel_appointment` · `confirm_appointment`

`when` **free text** — কলারের নিজের কথা। Model-এর কাছে ISO তারিখ চাওয়াই
পুরনো কোডের ভুল ছিল।

LLM যেন booking success বানাতে না পারে, তিনটা **গঠনগত** ব্যবস্থা:
(১) outcome একটা **closed enum** — "booked" মানে এমন কোনো মান model পেতেই পারে
না যদি না service provider-এর স্বীকৃতির পরে লিখে থাকে; (২) message **service
লেখে**; (৩) `as_tool_payload()` একটা **allowlist** — provider নাম নেই, status
code নেই, error string নেই।

`FunctionHandlers.dispatch` এই পাঁচটা নাম নতুন লেয়ারে রাউট করে, আর পুরনো
argument নাম (`date`, `starts_at`, `time`) alias হিসেবে গৃহীত — rollout-এর
সময় in-flight LLM turn বা cached schema ভাঙে না।

## ১২. CRM wiring

Calendar layer **internal business event emit করে**; CRM service consume করে।
`appointment.booked` / `.rescheduled` / `.cancelled` — সব STEP 5-এর
`crm_hooks` দিয়ে, একই transaction-এ।

**গঠনগতভাবে assert করা:** কোনো calendar মডিউল `providers.ghl` /
`.hubspot` / `.jobber` import করে না — source-এর বিপরীতে টেস্ট, যাতে পরে
দুর্ঘটনাক্রমে যোগ না হয়।

## ১৩. Tenant isolation

| প্রক্রিয়া | কোথায় |
|---|---|
| `UNIQUE (tenant_id, provider)` | জোড়াটা key, filter নয় |
| প্রতিটা lookup `(tenant_id, provider)` | `service.get_integration()` |
| অন্যের row-তে 404, কখনো 403 | 403 নিশ্চিত করত row-টা আছে |
| tenant আসে verified JWT থেকে | কোনো route body/query থেকে পড়ে না |
| `slot_key` tenant-salted | দুই ব্যবসা একই ঘণ্টা ধরতে পারে |
| AES-GCM AAD `(tenant, provider)` | credential row-এর মধ্যে সরানো যায় না |
| **Adapter-এর হাতে কোনো DB handle নেই** | শুধু `CalendarContext` — গঠনগতভাবে পৌঁছাতে *পারে না* |
| Webhook cancel `tenant_id` + `external_event_id`-এ scoped | event id একা cross-tenant write primitive হতো |

## ১৪. Credential নিরাপত্তা

STEP 5-এর **একই cipher, key ring, rotation** — AES-256-GCM, AAD
`(tenant_id, provider)`। API/লগ/audit/JWT/repr — কোথাও যায় না। 401 body
error message থেকে **সম্পূর্ণ বাদ**। Key না থাকলে **503, কখনো plaintext নয়**।

---

## ১৫. টেস্ট (৪৪০টি নতুন)

| ফাইল | সংখ্যা | কভারেজ |
|---|--:|---|
| `test_calendar_contract.py` | ১৭০ | **registry-র উপর parameterized** — নতুন provider সাথে সাথে টেস্ট হয় |
| `test_calendar_timezones.py` | ৯৩ | UTC, DST start/end, ambiguous, nonexistent, tenant zone, NLP |
| `test_calendar_api.py` | ৬৯ | API, tenant isolation, voice tools, webhook, perf |
| `test_calendar_providers.py` | ৬৮ | চার adapter-এর payload ও vendor quirk |
| `test_calendar_booking.py` | ৫৯ | business hours, availability, booking, **concurrency**, reschedule, cancel, reminder |
| `test_enum_consistency.py` | +১০ | migration↔model parity (৪৯ → ৫৯) |

### পরিমাপ (internal provider — শুধু VoxDesk-এর নিজস্ব overhead)

```
availability lookup            3.28 ms
availability empty → 6 booked  3.19 → 3.21 ms  (x1.0 — quadratic নয়)
booking                        9.22 ms
reschedule / cancel            7.39 / 6.15 ms
hung provider, 200 ms বাজেট    201.19 ms-এ ফেরত
```

শেষেরটা guard rail নয়, product requirement: lookup চিরকাল ঝুলিয়ে রাখলেও tool
ফেরত আসে।

---

## ১৬. যে ৭টা আসল বাগ টেস্ট লিখতে গিয়ে ধরা পড়ল

1. **`"day after tomorrow"` একদিন কম দিত** — `\btomorrow\b` আগে ম্যাচ করত।
   গ্রাহক ২৪ ঘণ্টা আগে হাজির হতেন।
2. **ISO তারিখ থেকে সময় দূষিত হতো** — `"2026-12-24 at 9am"`-এ মাসের `12` তুলে
   "সকাল না সন্ধ্যা?" জিজ্ঞেস করত। আর `"in 3 days"` বিকেল ৩টা বুক করত।
3. **OAuth refresh মৃত টোকেন দিয়ে `Authorization` পাঠাত** — অথচ refresh-ই সেই
   মুহূর্ত যখন টোকেন কাজ করে না, তাই expired tenant কখনো refresh করতে পারত না।
4. **Token endpoint-এ JSON পাঠানো হচ্ছিল**, form-encoded নয় — Microsoft
   সরাসরি reject করে।
5. **`_schedule_reminder` ইনজেক্ট করা ঘড়ি উপেক্ষা করত** — service-এর দুটো
   ভিন্ন "now" ছিল, তাই ভবিষ্যতের বুকিং-এর reminder অতীত হিসেবে বাদ পড়ত।
6. **`internal` provider undeclared capability-তে `[]` ফেরাত, raise নয়** —
   ঠিক সেই F2 প্যাটার্ন। Contract টেস্ট ধরেছে।
7. **হারা race-এর `session.rollback()` জেতা session-এর transaction ধ্বংস করত** —
   SAVEPOINT দিয়ে সমাধান।

এছাড়া একটা **pre-existing flaky টেস্ট** (~৫.৯% ফেল হার) ধরা পড়ে ফিক্স করেছি:
`test_rbac.py`-তে `token[:-1] + "0"` — digest ৬% ক্ষেত্রে `"0"`-তে শেষ হয়, তখন
"tampered" টোকেন আসলটার সমান হয়ে যেত এবং assertion কিছুই পরীক্ষা করত না।

### একটা টেস্ট বদলেছি, এবং কেন

`test_availability.py::test_bad_date_does_not_crash` দাবি করত `"next tuesday"`
**ব্যর্থ** হবে — কারণ পুরনো handler literal `YYYY-MM-DD` ছাড়া কিছু বুঝত না।
টেস্টটা একটা **সীমাবদ্ধতা encode করেছিল**, যা STEP 6 দূর করেছে। মূল উদ্দেশ্য
("crash করে না, structured উত্তর দেয়") রেখে সত্যিকারের অপাঠ্য input-এ বাড়িয়েছি।

---

## ১৭. যা live যাচাই করা হয়নি

**একটাও calendar provider আসল credentials দিয়ে ছোঁয়া হয়নি।** প্রতিটা adapter
published contract অনুযায়ী লেখা এবং `httpx`-এ intercept করা scripted transport-এর
বিপরীতে চালানো — header assembly, payload shape, status classification,
timeout handling সবই আসল কোড পথ, কিন্তু একটা byte-ও মেশিন ছাড়েনি।

- **Google** — client-supplied-id 409 আচরণ, `freeBusy`-র per-calendar `errors`
  shape, refresh-token semantics — সব ডকুমেন্টেশন থেকে।
- **Microsoft** — `getSchedule` shape, `availabilityView` encoding,
  `transactionId` dedupe, `/cancel` vs `DELETE` — ডকুমেন্টেশন থেকে।
- **Cal.com** — v2 envelope, 400-মানে-conflict, একাধিক slot shape — ডকুমেন্টেশন
  ও community রিপোর্ট থেকে।
- **যেকোনো provider থেকে inbound webhook।**
- **সম্পূর্ণ OAuth authorization flow** — token *refresh* বাস্তবায়িত ও
  unit-tested; প্রাথমিক authorization-code exchange ও callback route **নেই**।
- **PostgreSQL** — সব টেস্ট SQLite-এ। `alembic upgrade head` চালানো হয়নি;
  parity static AST parsing-এ যাচাই।

যা যাচাই *হয়েছে*: পাঁচ provider-এর request construction ও payload mapping,
response normalization, error mapping, retry classification, idempotency
(timeout-after-acceptance সহ), **আলাদা DB connection-এ genuine concurrent
booking**, চার zone-এ DST arithmetic, tenant isolation, credential encryption,
পুরো API।

---

## ১৮. বাকি সীমাবদ্ধতা

1. **OAuth authorization-code flow নেই।** Refresh আছে; প্রথম token pair পাওয়া
   নেই। এখন API দিয়ে credential দিতে হয় — self-hosted operator-এর জন্য চলে,
   গ্রাহকমুখী "Connect Google" বোতাম নয়।
2. **স্বয়ংক্রিয় token-refresh sweep নেই।** `refresh_access_token()` আছে ও
   tested, কিন্তু কোনো scheduler এখনো ডাকে না; `token_expires_at` প্রস্তুত।
3. **Google push notification-এ payload নেই** — শুধু "কিছু বদলেছে" বলে। Sync
   query ছাড়া কাজ করা মানে অনুমান, তাই রেকর্ড হয়, কিছু বদলায় না।
4. **Inbound একমুখী** — verify, dedupe, cancel। Bidirectional sync scope-এর বাইরে।
5. **`google_service_account` cancel/reschedule পারে না** এবং outage-কে খালি
   ক্যালেন্ডার থেকে আলাদা করতে পারে না (উত্তরাধিকারসূত্রে পাওয়া, বাইরে থেকে
   অপরিবর্তনীয়)।
6. **Reminder সারানো হয়েছে, নতুন করে ডিজাইন হয়নি** — timezone বাগ, dedupe ও
   cancellation যোগ; dispatch channel আগের SMS পথই।
7. **Recurring appointment নেই।**
8. **Availability প্রতি request-এ গণনা**, cache নেই।

---

## ১৯. চূড়ান্ত যাচাই

```
python3 -m compileall app scripts alembic tests   → OK
python3 -m ruff check <সব নতুন ফাইল>              → All checks passed
python3 -m pytest tests/ -q                       → 1481 passed, 29 skipped, 0 failed (61s)
নতুন মডিউল import                                  → 14/14 clean
migration↔model parity (2 enum, 4 টেবিল, 18 কলাম)  → PARITY CLEAN
```

Ruff-এ ৮টা pre-existing warning `routes.py`/`vectorstore.py`/`outbound.py`-তে —
STEP 5-এও ছিল, "unrelated ফাইল rewrite কোরো না" মেনে ছুঁইনি।

**STEP 1–5-এর কিছুই ভাঙেনি।** ১০৪১ পূর্ববর্তী টেস্টের ১০৪০টি অপরিবর্তিত পাস;
একটি (`test_bad_date_does_not_crash`) ইচ্ছাকৃত আচরণ-পরিবর্তনের কারণে হালনাগাদ,
§১৬-এ ব্যাখ্যা করা।
