# STEP 3 — Call Lifecycle & Real Human Transfer
### VoxDesk v0.4 → v0.5 · সম্পূর্ণ রিপোর্ট

---

## এক লাইনে

`transfer.py` **কোথাও থেকে কল হতো না**। `escalate_to_human` শুধু একটা flag বসিয়ে
`{"action": "transfer"}` ফেরত দিত, আর `functions.py`-র কমেন্ট দাবি করত pipeline
সেটা দেখে — **pipeline কখনো দেখেনি**। AI বলত "connecting you", কাস্টমার অপেক্ষা
করত, কিছুই হতো না। এখন tool → service → provider → callback → DB পুরো পথ জোড়া,
আর `TRANSFERRED` **শুধু provider redirect গ্রহণ করার পরেই** বসে।

**টেস্ট: ৪১৪ passed / ০ failed** (আগে ২৫৯, নতুন ১৫৫)।

---

## ১. ফাইল যোগ হয়েছে (৫টা কোড + ৭টা টেস্ট/ডক)

| ফাইল | কাজ |
|---|---|
| `app/telephony/call_state.py` | কল-স্ট্যাটাসের **একমাত্র** উৎস। transition গ্রাফ, terminal সুরক্ষা, idempotency, provider-status ম্যাপিং |
| `app/telephony/transfer_service.py` | ট্রান্সফার অর্কেস্ট্রেশন — validation, idempotency লক, state, system event, callback |
| `app/telephony/provider.py` | `TelephonyProvider` protocol + `TwilioProvider` + `FakeTelephonyProvider` |
| `app/telephony/phone.py` | E.164 normalize/validate/redact |
| `alembic/versions/0004_call_transfer_lifecycle.py` | **মাইগ্রেশন** |
| `docs/CALL-LIFECYCLE.md` | লাইফসাইকেল, transition টেবিল, ট্রান্সফার আচরণ, রেস, টেস্টিং |
| `tests/test_call_state.py` (৪৫) · `test_phone.py` (২৩) · `test_transfer.py` (২৬) · `test_transfer_pipeline.py` (১৪) · `test_call_callbacks.py` (১৯) · `test_stream_lifecycle.py` (১৫) · `test_transfer_security.py` (৯) | |

## ২. ফাইল বদলেছে (৭টা)

| ফাইল | কী বদলাল |
|---|---|
| `app/db/models.py` | `TransferState` enum + `TRANSFER_IN_FLIGHT`; `Call`-এ ১০টা কলাম |
| `app/telephony/transfer.py` | এখন **শুধু TwiML builder**; `<Dial action=...>` callback যোগ; `execute_transfer` low-level escape hatch হিসেবে ডকুমেন্টেড |
| `app/telephony/twilio_handler.py` | `/status` সিগনেচার + `call_state`; নতুন `/transfer-status`; `/outbound-answer` সিগনেচার; WS teardown আর transferred কলকে FAILED বলে না |
| `app/agent/functions.py` | `escalate_to_human` → `transfer_service`; `available_tools()`; injectable provider |
| `app/agent/pipeline.py` | per-call tool list; `TRANSFER_STARTED`-এ পরিচ্ছন্নভাবে stream বন্ধ |
| `app/api/routes.py` | `GET /api/calls/{id}/transfer`; কল লিস্টে `transfer_state` |
| `README.md`, `tests/test_enum_consistency.py` | v0.5 সেকশন; TransferState parity + migration chain টেস্ট |

**হাত দেওয়া হয়নি:** `0001`/`0002`/`0003`, auth প্যাকেজ, RBAC, dashboard, integrations, compliance, i18n।

---

## ৩. মাইগ্রেশন

**`0004_call_transfer_lifecycle`** ← `0003_auth_rbac`

চেইন যাচাই করা (টেস্টে enforced): `0001_baseline → 0002_enum_consistency → 0003_auth_rbac → 0004_call_transfer_lifecycle`, একটাই root, প্রতিটা parent বিদ্যমান।

সম্পূর্ণ **additive** — কোনো কলাম বদলায়/মোছে না, কোনো row ছোঁয় না। সব নতুন কলাম nullable বা server-default, তাই rolling deploy ভাঙে না। `downgrade()` শুধু নিজের যোগ করাগুলো ড্রপ করে — কল-হিস্ট্রি অক্ষত।

PG enum `transferstate` **NAME**-এ ডিক্লেয়ার করা (`NONE`/`REQUESTED`/…) — STEP 1-এর শিক্ষা, আর একটা AST টেস্ট এখন model↔migration parity জোর করে ধরে রাখে।

---

## ৪. বৈধ transition টেবিল

| From | To | কখন |
|---|---|---|
| `RINGING` | `IN_PROGRESS` | উত্তর দিল |
| `RINGING` | `NO_ANSWER` | বাজতে বাজতে শেষ |
| `RINGING` | `FAILED` | busy / failed / canceled |
| `RINGING` | `COMPLETED` | ringback-এর সময় কেটে দিল |
| `IN_PROGRESS` | `TRANSFERRED` | provider redirect নিল |
| `IN_PROGRESS` | `COMPLETED` | স্বাভাবিক শেষ |
| `IN_PROGRESS` | `FAILED` | media/provider error |
| `TRANSFERRED` | `COMPLETED` | মানুষ রেখে দিল |
| `TRANSFERRED` | `FAILED` | transfer leg মরল |
| `COMPLETED` / `FAILED` / `NO_ANSWER` | — | **terminal, বেরোনোর পথ নেই** |

বাকি সব — `COMPLETED→RINGING`, `FAILED→IN_PROGRESS`, `TRANSFERRED→IN_PROGRESS` — প্রত্যাখ্যাত, লগড, row অপরিবর্তিত। **কোনো retry মেকানিজম শেষ হওয়া কল খোলে না**; retry মানে নতুন SID-সহ নতুন Call row।

`TRANSFERRED` ইচ্ছাকৃতভাবে terminal **নয়** — কল তখনো চালু, শুধু মানুষ ধরেছে।

Provider ম্যাপিং: `queued/initiated/ringing→RINGING`, `in-progress/answered→IN_PROGRESS`, `completed→COMPLETED`, `no-answer→NO_ANSWER`, `busy/failed/canceled/cancelled→FAILED` (+`failure_reason`)। **অজানা স্ট্রিং উপেক্ষিত** — আগে চুপচাপ `COMPLETED` ধরে নিত, অর্থাৎ একটা টাইপো লাইভ কল কেটে দিত।

---

## ৫. আসল ট্রান্সফার কীভাবে হয়

```
escalate_to_human(reason)                    functions.py
  → transfer_service.request_transfer()
      0. tenant scoping   call.tenant_id == tenant.id, নইলে refuse
      1. idempotency      in-flight? বিদ্যমান state ফেরত, provider ছোঁয়া হয় না
      2. transferable?    terminal হলে refuse
      3. destination      tenant.escalation_number → E.164 normalize
      4. REQUESTED        + SYSTEM turn, COMMIT (provider ছোঁয়ার আগে)
      5. provider.redirect_call(call_sid, twiml)
           ├─ ব্যর্থ  → transfer_state=FAILED, status অপরিবর্তিত
           └─ গৃহীত → transfer_state=DIALING, status=TRANSFERRED  ◀── শুধু এখানে
  → Twilio মানুষকে ডায়াল করে, <Dial action=/telephony/transfer-status>
  → POST /telephony/transfer-status
       answered/completed → CONNECTED + "Human transfer connected."
       busy/no-answer/…   → FAILED    + "Human transfer failed."
```

**AI-র অনুরোধ = request, outcome নয়।** Twilio reject করলে (সাধারণত `20404` — কল আগেই শেষ) কল `IN_PROGRESS` থাকে, ট্রান্সফার `FAILED` লেখা হয়, agent পায় `TRANSFER_FAILED`।

Internal কোড: `TRANSFER_STARTED` / `TRANSFER_COMPLETED` / `TRANSFER_FAILED` / `ALREADY_TRANSFERRED`। কাস্টমার যা **শোনে** সেটা আলাদা, generic বাক্য — raw provider error বা ফোন নম্বর কাস্টমারের কাছেও যায় না, LLM-এর কাছেও না (টেস্টে assert করা)।

---

## ৬. ডুপ্লিকেট ঠেকানো

`Call.transfer_state` **নিজেই লক**। `REQUESTED`/`DIALING`/`CONNECTED` অবস্থায় যেকোনো নতুন অনুরোধ বিদ্যমান state ফেরত দেয়, provider **একবারও** ডাকা হয় না:

```
১ম অনুরোধ → TRANSFER_STARTED      provider.call_count == 1
২য় অনুরোধ → ALREADY_TRANSFERRED   provider.call_count == 1
৩য় অনুরোধ → ALREADY_TRANSFERRED   provider.call_count == 1
```

LLM tool call retry করে — এখানে ডুপ্লিকেট মানে কারো ডেস্কে সত্যিকারের দ্বিতীয় ফোন বাজা। `FAILED` in-flight নয়, তাই আসল retry অনুমোদিত এবং `transfer_attempts` বাড়ে।

Transcript event exact-text দিয়ে de-duplicated — retried webhook তিনবার "connected" লিখতে পারে না।

---

## ৭. Callback রেস

| কেস | ক্রম | ফল |
|---|---|---|
| **A** | transfer → stream বন্ধ → `completed` → dial callback | `COMPLETED` + `CONNECTED` |
| **B** | transfer → dial callback → `completed` | `COMPLETED` + `CONNECTED` |
| **C** | `completed` তারপর বাসি `no-answer` | `COMPLETED` থাকে, বাসিটা উপেক্ষিত |
| **D** | duplicate `completed` × N | `COMPLETED`, একবার বিল, এক সেট event |

**অনুমান বনাম প্রমাণ।** Case A লিখতে গিয়ে একটা আসল ডিজাইন ত্রুটি বেরিয়েছিল: কল শেষ হওয়ার সময় ট্রান্সফার `DIALING` থাকলে আমরা failure *অনুমান* করতাম, তারপর আসল `<Dial>` callback ("মানুষ ধরেছে") এলে সেটা ব্লক হয়ে যেত। এখন অনুমান-করা failure `INFERRED_PREFIX` দিয়ে চিহ্নিত এবং প্রামাণিক callback সেটা শুধরাতে পারে। Provider-এর নিজের রিপোর্ট করা failure (`busy`, `no-answer`) prefix ছাড়া — **কখনো override হয় না** (এই সংকীর্ণতার আলাদা টেস্ট আছে)।

**বিলিং** শুধু *applied* terminal transition-এ, এবং শুধু আগের রেকর্ডের উপরের delta-টুকু — N-টা duplicate একবারই বিল করে। Lead grading আর CRM push-ও `result.applied`-এ gated, CRM অতিরিক্তভাবে `crm_synced` দেখে।

---

## ৮. ডাটাবেস ফিল্ড

`calls`-এ যোগ: `failure_reason`, `transfer_state` (enum, default `NONE`), `transfer_destination`, `transfer_reason`, `transfer_attempts` (default `0`), `transfer_error`, `transfer_requested_at`, `transfer_started_at`, `transfer_completed_at`, `transfer_failed_at`। ইনডেক্স `ix_calls_tenant_transfer_state (tenant_id, transfer_state)`।

বিদ্যমান `escalated` boolean **রাখা এবং এখনো maintained** — dashboard আর CRM payload ওটা পড়ে। কিছু ডুপ্লিকেট করা হয়নি।

---

## ৯. টেস্ট (নতুন ১৫৫টা)

| ফাইল | সংখ্যা | কী প্রমাণ করে |
|---|---|---|
| `test_call_state.py` | ৪৫ | ৮টা বৈধ transition, ৮টা অবৈধ প্রত্যাখ্যাত, terminal সুরক্ষা, duplicate, duration কখনো কমে না, unknown status কল কাটে না |
| `test_transfer.py` | ২৬ | valid escalation সত্যিই ডায়াল করে, missing/invalid destination, provider failure, idempotency, callback, system event, tenant mismatch |
| `test_transfer_pipeline.py` | ১৪ | tool registered, tool→service→provider পুরো পথ, unavailable হলে tool লুকায়, failure সাফল্য দাবি করে না, injection |
| `test_call_callbacks.py` | ১৯ | সিগনেচার, ৪টা রেস কেস, ডাবল বিলিং, lead re-grading |
| `test_stream_lifecycle.py` | ১৫ | token call-specific/expiring/tamper-proof, transfer teardown FAILED বলে না |
| `test_transfer_security.py` | ৯ | tenant A→B ট্রান্সফার/মেটাডেটা/destination সব ব্লকড, লগে correlation আছে গোপন কিছু নেই |
| `test_phone.py` | ২৩ | E.164, country code **কখনো অনুমান নয়**, redact |
| `test_enum_consistency.py` | +৪ | TransferState parity, migration কলাম parity, chain linearity |
| পুরনো ২৫৯ | ২৫৯ | **একটাও ভাঙেনি** |

সব transfer টেস্ট আসল service + আসল session + আসল row-তে চলে; শুধু outbound HTTP call `FakeTelephonyProvider`-এ বদলানো, যেটা **আমাদের জেনারেট করা আসল TwiML** রেকর্ড করে এবং fail করতে বলা যায়। কোনো টেস্ট "function কল হয়েছে" assert করে থামে না — প্রতিটা DB state যাচাই করে।

---

## ১০. যাচাইয়ের ফল

```
পুরো suite      : 414 passed, 0 failed   (৫ বার পরপর একই)
compileall      : OK (app, scripts, alembic, tests)
import check    : OK
lint (STEP 3)   : All checks passed  (F, I001, SIM, C4, UP)
route sweep     : ৪১টা route, UNGUARDED: NONE
migration chain : 0001 → 0002 → 0003 → 0004, একটাই root
```

একবার random-এ একটা stream-token টেস্ট ফেল করেছিল — খুঁজে দেখা গেল ওটা আমার নিজের শেল কমান্ডে `ruff --fix` আর `pytest` একসাথে চলার রেস, কোডের flake নয়; ৫ বার পরপর ক্লিন।

---

## ১১. পথে যে বাগগুলো ধরা পড়ল (পরিকল্পনার বাইরে)

1. **`/telephony/status`-এ সিগনেচার ছিল না** — যে কেউ যেকোনো কল শেষ করে দিতে ও tenant-এর বিল বাড়িয়ে দিতে পারত। ✅
2. **`/telephony/outbound-answer`-এও সিগনেচার ছিল না** — শেষ sweep-এ ধরা; যে কেউ ইচ্ছেমতো call SID-র জন্য media-stream URL বানাতে পারত। ✅
3. **Whisper prompt injection** — LLM-জেনারেটেড `reason` সরাসরি `<Say>`-তে যাচ্ছিল। কাস্টমার "ওদের বলো ৫৫৫... নম্বরে ফোন করতে" বললে ব্যবসার নিজের রিসেপশনিস্ট স্টাফকে আক্রমণকারীর নম্বর পড়ে শোনাত। এখন digit-run `[number removed]` হয়, markup ক্যারেক্টার বাদ যায়; পূর্ণ reason DB-তে থাকে। ✅
4. **ডাবল বিলিং** — duplicate callback প্রতিবার `minutes_used += duration/60` করত। ✅
5. **Lead re-grading** — retried webhook `DNC` status মুছে দিতে পারত। ✅
6. **Transferred কল FAILED লেখা হতো** — ট্রান্সফার ইচ্ছাকৃতভাবে stream মারে, আর handler নিঃশর্তে `status = FAILED` বসাত। ✅
7. **`ended_at` কখনো সেট হতো না।** ✅

---

## ১২. যা এই পরিবেশে যাচাই করা যায়নি (সৎভাবে)

1. **আসল Twilio-তে কিছু চালানো হয়নি।** `TwilioProvider.redirect_call`-এর সঠিকতা SDK-র API shape-এর উপর নির্ভরশীল; টেস্টগুলো `FakeTelephonyProvider`-এ চলেছে। প্রথম staging কলে `calls(sid).update(twiml=...)` আর `<Dial action>` callback নিজে চোখে দেখে নেবেন।
2. **PostgreSQL-এ মাইগ্রেশন চালানো হয়নি** — এই স্যান্ডবক্সে `alembic` আর Postgres নেই। টেস্ট SQLite-এ চলে। সিনট্যাক্স, enum NAME parity আর কলাম parity টেস্টে enforced, কিন্তু `make migrate` আপনাকে একবার চালাতে হবে।
3. **pipecat ইনস্টল নেই**, তাই `pipeline.py`-র `task.stop_when_done()` কলটা সত্যিকারের pipecat টাস্কে চলিয়ে দেখা হয়নি — tool→service→provider পথটা `FunctionHandlers.dispatch` দিয়ে সরাসরি প্রমাণ করা হয়েছে, যেটা pipeline যে ফাংশনটাই ডাকে।
4. **Recording system তৈরি করিনি** — নির্দেশ অনুযায়ী। `recording_url` কলাম আছে কিন্তু কোনো recording callback route ছিল না এবং নেই; ট্রান্সফার TwiML `tenant.record_calls` মানে, ব্যস।
5. `<Dial action>` callback-এ Twilio ঠিক কোন `DialCallStatus` মান পাঠায় সেটা ডকুমেন্টেশন থেকে নেওয়া, লাইভ যাচাই করা নয়।

---

## ১৩. আপনার পরের কমান্ড

```bash
cd voxdesk && sh .devsetup.sh
make migrate          # 0004 প্রয়োগ করবে
make test             # 414 passed
```

Twilio কনসোলে নম্বরের **status callback** → `/telephony/status`। ট্রান্সফারের action URL কোড নিজেই বানায় (`PUBLIC_BASE_URL` থেকে)।

প্রতিটা tenant-এ `escalation_number` **E.164**-এ (`+8801712345678`) বসাতে হবে — নইলে ট্রান্সফার tool AI-কে দেওয়াই হবে না, যেটা ইচ্ছাকৃত।

**STEP 3 এখানেই শেষ। STEP 4 শুরু করিনি।**