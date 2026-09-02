# VoxDesk v0.5 — AI Voice + Text Receptionist

**যা যা মিসিং ছিল, সব যোগ করা হয়েছে।** Fiverr-এর টপ এজেন্সিগুলোর গিগে যে ফিচারগুলো লেখা থাকে, তার প্রতিটার কোড এখানে আছে।

```
১৪৫ tests passing · ৫,১০০+ লাইন Python · ২০+ API endpoint · ৭টা AI tool
```

---

## ⚡ ৬০ সেকেন্ডে চালু

```bash
cp .env.example .env      # কী-গুলো বসান
make up                   # postgres + api + scheduler + dashboard
make seed                 # ডেমো ক্লায়েন্ট বানায় (tenant id প্রিন্ট করে)

# প্রথম owner অ্যাকাউন্ট — পাসওয়ার্ড প্রম্পটে চাইবে, argv-তে যাবে না
python -m scripts.create_owner --tenant-id <উপরের uuid> --email you@example.com

open http://localhost:8000/docs
```

> **রিপোতে কোনো ডিফল্ট ইউজারনেম/পাসওয়ার্ড নেই — ইচ্ছাকৃতভাবে।** শিপ করা
> ক্রেডেনশিয়াল মানেই ব্যাকডোর। প্রথম owner আপনি নিজে বানাবেন।

`make up` নিজেই `alembic upgrade head` চালায়। আলাদা কিছু করতে হবে না।

| কমান্ড | কাজ |
|---|---|
| `make up` / `make down` | পুরো স্ট্যাক চালু / বন্ধ |
| `make migrate` | ডাটাবেস মাইগ্রেশন |
| `make seed` | ডেমো tenant |
| `make test` | ৪১১টা টেস্ট |
| `make worker` | রিমাইন্ডার + আউটবাউন্ড ওয়ার্কার |
| `make dev` | docker ছাড়া লোকাল API |

---

## 📦 কী কী আছে

### ১. ইনবাউন্ড ভয়েস (মূল পণ্য)

```
📞 Twilio  →  Silero VAD  →  Deepgram STT  →  Backchannel
                                                   ↓
    Twilio ← ElevenLabs ← TextNormalizer ← FillerInjector ← LLM
                                                   ↑
                                    ChatGPT | Claude | Gemini
```
প্রথম অডিও পর্যন্ত লক্ষ্য **~৫৫০–৭৫০ms**। আসল সংখ্যা আপনার সার্ভারের লোকেশন আর নেটওয়ার্কের উপর নির্ভর করবে — নিজে মেপে নেবেন।

### ২. আউটবাউন্ড কলিং — `app/telephony/outbound.py`

Cold calling, follow-up, lead nurture। **আইনি গার্ডরেল কোডেই বসানো:**

| গার্ডরেল | কী করে |
|---|---|
| `is_call_window_open()` | ক্লায়েন্টের **নিজের timezone-এ** ৯টা–৮টার বাইরে কল যায় না |
| `next_window_start()` | সময় শেষ হলে ড্রপ না করে পরের দিনে শিডিউল করে |
| `backoff_for()` | ১ → ৪ → ২৪ ঘণ্টা, তারপর থামে |
| `max_call_attempts` | ডিফল্ট ৩ |
| DNC ফিল্টার | `next_callable_leads()` কখনো DNC লিড ফেরত দেয় না |
| `machine_detection` | ভয়েসমেইল ধরলে সাথে সাথে কেটে দেয় |

### ৩. আসল কল ট্রান্সফার — `app/telephony/transfer.py`

আগে AI শুধু *"transferring now"* **বলত** — কিছুই হতো না। ডেমোতে ধরা পড়ার এক নম্বর জায়গা।

এখন Twilio-র live-call-update API দিয়ে সত্যিকারের `<Dial>`:
- `answer_on_bridge=True` — কলার ringback শোনে, নীরবতা না
- whisper — মানুষটা ধরার আগে শোনে "AI transfer, emergency"
- মানুষ না ধরলে **ভয়েসমেইল**, কল কেটে যায় না

### ৪. SMS + WhatsApp — `app/channels/messaging.py`

একই ৭টা tool, একই knowledge base, একই AI — শুধু চ্যানেল আলাদা।

- **একটাই webhook** দুই চ্যানেলের জন্য (`whatsapp:` prefix দেখে চেনে)
- **STOP/START/HELP LLM-এর আগে** হ্যান্ডেল হয় — আইনি বাধ্যবাধকতা
- `"please stop by at 3"` কে opt-out ধরে **না** (এই বাগটা সবার থাকে)
- SMS উত্তর ৩০০ অক্ষরে বাক্যের শেষে কাটে
- থ্রেড ৩০ মিনিট বাঁচে, তারপর নতুন কথোপকথন

### ৫. IVR / কল ফ্লো — `app/telephony/ivr.py`

Twilio Studio-র বদলে JSON। ড্যাশবোর্ড থেকে এডিট করা যায়, deploy লাগে না।

```json
{"start": "menu", "nodes": {
  "menu": {"say": "Emergency? Press 1. Otherwise just tell me what you need.",
           "gather": {"1": "emergency"}, "timeout_goto": "ai"},
  "emergency": {"say": "Connecting you.", "transfer": "{escalation_number}"},
  "ai": {"ai": true}}}
```

`validate_flow()` ভাঙা ফ্লো **ঢুকতেই দেয় না** — dangling pointer, dead end, unreachable node, আর সবচেয়ে জরুরি: **এমন মেনু যেখান থেকে কখনো মানুষ বা AI-তে পৌঁছানো যায় না।**

### ৬. ১৩টা ভাষা — `app/core/i18n.py`

আগে `Tenant.language` কলামটা কেউ পড়ত না। এখন চার জায়গায় পৌঁছায়:

| কোথায় | কী হয় |
|---|---|
| Deepgram | nova-3 যে ভাষা কভার করে না, **নিজে থেকে nova-2**-এ নামে |
| ElevenLabs | অ-ইংরেজি হলে multilingual মডেল বাধ্যতামূলক (নইলে স্প্যানিশ ইংরেজি টানে পড়বে) |
| LLM প্রম্পট | *"ONLY Español"* — কাস্টমার ইংরেজি শব্দ বললেও ভাষা বদলায় না |
| filler_words | শুধু ইংরেজি মডেলে আছে, তাই অন্য ভাষায় বন্ধ |

en-US/GB/AU · es-US/MX · fr · de · pt-BR · it · nl · hi · ar (RTL) · bn

### ৭. A2P 10DLC + কমপ্লায়েন্স — `app/core/compliance.py`

**US-এ রেজিস্ট্রেশন ছাড়া SMS নীরবে ব্লক হয়** — ক্যারিয়ার কিছু বলে না, ক্লায়েন্ট আপনাকে দোষ দেয়।

- **SHAFT ফিল্টার** — cannabis / loan / gambling / firearms / vape ধরে ফেলে
- **URL shortener ব্লক** — bit.ly = ক্যাম্পেইন রিজেক্ট
- **সেগমেন্ট কাউন্টার** — GSM-7 বনাম UCS-2। একটা ইমোজি ১৬০ অক্ষরকে ৭০ বানিয়ে দেয়, বিল তিনগুণ
- `registration_checklist()` — কোন কাজ ক্লায়েন্টের, কোনটা আপনার, কোনটা ক্যারিয়ারের

### ৮. CRM — `app/integrations/crm.py`

কল শেষ হলেই **অটো পুশ**, ব্যাকগ্রাউন্ড টাস্কে (Twilio-র webhook কখনো আটকায় না)।
**GoHighLevel** (contact + tag mapping), HubSpot, আর generic webhook → Zapier / Make / n8n।

### ৯. লিড স্কোরিং

```
immediately 40 · this_week 30 · this_month 15 · just_looking 5
+ budget জানা 20  + decision maker 15  + কন্টাক্ট 15  + booked 10
```
**৭০+ = সাথে সাথে মালিকের ফোনে "HOT LEAD" SMS।** নিয়মটা deterministic — ক্লায়েন্ট নিজে যাচাই করতে পারবে।

### ১০. রিমাইন্ডার — `app/integrations/reminders.py`

২৪ ঘণ্টা আগে *"Reply C to confirm or R to reschedule"*। **এটাই একমাত্র ফিচার যার দাম ডলারে মাপা যায়** — তাই এটা দিয়েই project price চাওয়া যায়, hourly না।

---

## ☎️ কল লাইফসাইকেল ও মানুষে ট্রান্সফার (v0.5)

আগে `escalate_to_human` শুধু `escalated = True` বসাত আর `action: "transfer"`
ফেরত দিত — **pipeline সেটা কখনো পড়তই না**, `execute_transfer`-এর কোনো caller
ছিল না। AI বলত "connecting you", তারপর কিছুই হতো না।

| জিনিস | কী করা হয়েছে |
|---|---|
| **স্টেট মেশিন** | `app/telephony/call_state.py` — একমাত্র জায়গা যেখানে `call.status` বসে |
| **টার্মিনাল সুরক্ষা** | COMPLETED/FAILED/NO_ANSWER থেকে আর কোথাও যাওয়া যায় না |
| **অজানা স্ট্যাটাস** | উপেক্ষা করা হয়, আগে চুপচাপ COMPLETED ধরে নিত |
| **আসল ট্রান্সফার** | provider redirect **গ্রহণ করার পরেই** `TRANSFERRED` বসে |
| **প্রমাণ** | `<Dial action=/telephony/transfer-status>` callback ছাড়া "connected" বলা হয় না |
| **Idempotency** | `transfer_state` = লক। দ্বিতীয় অনুরোধে মানুষের ফোন দ্বিতীয়বার বাজে না |
| **রেস** | ৪টা callback ordering কেস — অনুমান বনাম প্রমাণ আলাদা করা |
| **সিগনেচার** | `/telephony/status`-এ HMAC যোগ (আগে সম্পূর্ণ খোলা ছিল) |
| **ডাবল বিলিং** | duplicate callback আর মিনিট দুবার বিল করে না |
| **ফোন নম্বর** | E.164 normalize; country code **কখনো অনুমান করা হয় না**; লগে redacted |

পুরো বিবরণ: **[`docs/CALL-LIFECYCLE.md`](docs/CALL-LIFECYCLE.md)**

---

## 📚 নলেজ বেস ও RAG (v0.5)

আগে পুরো `knowledge_base` dict প্রতিটা টার্নে system prompt-এ ঢুকত। এখন নয়।

| জিনিস | কী করা হয়েছে |
|---|---|
| **ডকুমেন্ট** | PDF, DOCX, TXT, Markdown, CSV, JSON আপলোড → extract → chunk → embed |
| **মূল নিয়ম** | আপলোড ≠ জ্ঞান। শুধু **retrieve হওয়া chunk** LLM-এ যায় |
| **টেন্যান্ট আইসোলেশন** | tenant filter **query-র ভিতরে**, পরে ফিল্টার করে নয় |
| **স্ট্যাটাস** | UPLOADED → PROCESSING → READY → FAILED / ARCHIVED; শুধু READY খোঁজা যায় |
| **Embedding** | provider config-driven; মডেল বদলালে reindex বাধ্যতামূলক |
| **Injection** | retrieved text = UNTRUSTED DATA; instruction-শেপ লাইন neutralize করা হয় |
| **লাইভ কলে** | bounded 1.5s timeout; ব্যর্থ হলে বানায় না, escalate করে |
| **Eval** | `tests/evals/rag/` — relevance, groundedness, refusal, injection, isolation |

পুরো বিবরণ: **[`docs/KNOWLEDGE-RAG.md`](docs/KNOWLEDGE-RAG.md)**

---

## 🔌 CRM ইন্টিগ্রেশন (v0.6)

কল/লিড/অ্যাপয়েন্টমেন্ট → normalized ইভেন্ট → provider adapter → CRM API →
persisted sync result → retry/idempotency। **চারটা provider**, নতুন provider
যোগ করতে core business logic ছুঁতে হয় না।

| Provider | Contact | Note | Appointment | Tag |
|---|:--:|:--:|:--:|:--:|
| GoHighLevel | ✅ upsert | ✅ | ✅ | ✅ |
| HubSpot | ✅ upsert | ✅ | — | — |
| Jobber | ✅ query+create | ✅ | — | — |
| Generic Webhook | ✅ signed | ✅ | ✅ | ✅ |

মূল গ্যারান্টি:

| বিষয় | কীভাবে |
|---|---|
| **Credentials** | AES-256-GCM, `(tenant_id, provider)`-এ AAD-bound। API/লগ/audit — কোথাও যায় না |
| **Idempotency** | ইভেন্ট থেকে derived key + `UNIQUE (tenant_id, idempotency_key)`। ডুপ্লিকেট কল = একটাই CRM contact |
| **Retry** | Bounded exponential + full jitter। 401/422 রিট্রাই হয় না, timeout/429/5xx হয় |
| **Tenant isolation** | প্রতিটা lookup `(tenant_id, provider)`; contact hash tenant-salted; adapter-এর হাতে কোনো DB handle নেই |
| **Failure UX** | CRM ফেল করলে কলার কিছুই টের পায় না — hook কখনো raise করে না |

⚠️ **কোনো provider API আসল credentials দিয়ে live টেস্ট করা হয়নি।** কী যাচাই
হয়েছে আর কী হয়নি: `docs/CRM-INTEGRATIONS.md` §13।

প্রোডাকশনে **`CRM_ENCRYPTION_KEYS` বাধ্যতামূলক** — না দিলে অ্যাপ boot করবে না।

পুরো বিবরণ: **[`docs/CRM-INTEGRATIONS.md`](docs/CRM-INTEGRATIONS.md)** ·
অডিট: [`docs/CRM-AUDIT.md`](docs/CRM-AUDIT.md)

---

## 📅 ক্যালেন্ডার ও শিডিউলিং (v0.7)

কলার রিকোয়েস্ট → availability → business rules → slot → booking → **provider
confirmation** → persistence → CRM event → reminder। **পাঁচটা provider**।

| Provider | Availability | Book | Reschedule | Cancel |
|---|:--:|:--:|:--:|:--:|
| Google Calendar | ✅ free/busy | ✅ | ✅ | ✅ |
| Microsoft Graph | ✅ getSchedule | ✅ | ✅ | ✅ |
| Cal.com | ✅ slots | ✅ | ✅ dedicated | ✅ |
| Internal | ✅ | ✅ | ✅ | ✅ |
| Google service account (legacy) | ⚠️ | ✅ | — | — |

মূল গ্যারান্টি:

| বিষয় | কীভাবে |
|---|---|
| **কখনো মিথ্যা "booked" নয়** | `CONFIRMED` লিখতে provider-এর `external_event_id` লাগে |
| **Outage ≠ খালি ক্যালেন্ডার** | `get_busy()` raise করে; আগের কোড `[]` ফেরাত |
| **Double-booking** | `UNIQUE (tenant_id, slot_key)` — DB সিদ্ধান্ত নেয়, application logic নয় |
| **Ambiguous timeout** | provider-কে জিজ্ঞেস করে; "চেক করতে পারিনি" ≠ "নেই" |
| **Timezone** | UTC-তে সংরক্ষণ, tenant-এর zone-এ যুক্তি; DST gap/overlap ধরা পড়ে |
| **Business hours** | per-weekday intervals, lunch break, holiday, blocked period |
| **LLM booking বানাতে পারে না** | outcome একটা closed enum; message service লেখে |

⚠️ **কোনো calendar provider আসল credentials দিয়ে live টেস্ট করা হয়নি।**
বিস্তারিত: `docs/CALENDAR-INTEGRATIONS.md` §12।

পুরো বিবরণ: **[`docs/CALENDAR-INTEGRATIONS.md`](docs/CALENDAR-INTEGRATIONS.md)** ·
অডিট: [`docs/CALENDAR-AUDIT.md`](docs/CALENDAR-AUDIT.md)

---

## 💳 বিলিং, Stripe ও usage metering (v0.8)

customer → plan → subscription → usage event → metering → invoice →
entitlement → webhook reconciliation।

| প্ল্যান | মাসিক | ভয়েস মিনিট | Overage/min | Overage? |
|---|--:|--:|--:|:--:|
| Trial | $0 | 60 | — | **না** |
| Starter | $199 | 500 | ১২¢ | হ্যাঁ |
| Pro | $499 | 2,000 | ১০¢ | হ্যাঁ |
| Enterprise | $1,499 | 10,000 | ৮¢ | হ্যাঁ |

মূল গ্যারান্টি:

| বিষয় | কীভাবে |
|---|---|
| **Usage authority** | immutable append-only `UsageEvent`; `minutes_used` এখন শুধু cache |
| **Idempotency** | key কল থেকে derived + `UNIQUE (tenant_id, idempotency_key)` — ১০টা duplicate callback = ১টা চার্জ |
| **Webhook ordering** | provider timestamp তুলনা — বাসি event নতুন state ফেরত নিতে পারে না |
| **Signature** | raw bytes, rotation-এ একাধিক `v1`, replay window, constant-time |
| **Price trust** | ক্লায়েন্ট শুধু plan code পাঠায়; `CheckoutIn`-এ `price` ফিল্ডই নেই |
| **Money** | integer cents ও millicents — কোথাও float নেই |
| **Checkout ≠ active** | শুধু verified webhook subscription সক্রিয় করে |

⚠️ **কোনো Stripe API কল আসল credentials দিয়ে করা হয়নি** — test-mode-ও নয়।
বিস্তারিত: `docs/BILLING.md` §13।

দুটো আসল বাগ পাওয়া গেছে ও সারানো হয়েছে: একই ১২০-সেকেন্ডের কল webhook ক্রম
অনুযায়ী **২.০০/১.০০/০.৫০ মিনিট** বিল করত, আর `minutes_used` **কখনো রিসেট হতো
না** — মানে "৫০০ মিনিট/মাস" আসলে "৭৫০ মিনিট চিরকাল"।

পুরো বিবরণ: **[`docs/BILLING.md`](docs/BILLING.md)** ·
অডিট: [`docs/BILLING-AUDIT.md`](docs/BILLING-AUDIT.md)

---

## 🔐 অথেনটিকেশন ও টেন্যান্ট আইসোলেশন (v0.4)

আগে প্রতিটা এন্ডপয়েন্ট খোলা ছিল। এখন নয়।

| জিনিস | কী করা হয়েছে |
|---|---|
| **লগইন** | `POST /auth/login` → ১৫ মিনিটের JWT + HttpOnly রিফ্রেশ কুকি |
| **পাসওয়ার্ড** | bcrypt cost 12, ১২+ ক্যারেক্টার পলিসি, কখনো লগ/সিরিয়ালাইজ হয় না |
| **রোল** | owner / admin / manager / agent / viewer — পারমিশন এক জায়গায় ডিফাইন করা |
| **টেন্যান্ট আইসোলেশন** | URL-এর `tenant_id` **অবিশ্বস্ত ইনপুট**; আসল tenant টোকেন থেকে আসে |
| **ক্রস-টেন্যান্ট** | সবসময় `404`, কখনো `403` — ৪০৩ দিলে বোঝা যায় জিনিসটা আছে |
| **রিফ্রেশ টোকেন** | opaque, শুধু SHA-256 হ্যাশ সেভ হয়, একবার ব্যবহারযোগ্য, চুরি ধরা পড়লে সব সেশন বাতিল |
| **ইনস্ট্যান্ট রিভোক** | `token_version` বাড়ালেই ওই ইউজারের সব টোকেন সঙ্গে সঙ্গে মৃত |
| **অডিট লগ** | লগইন, লগআউট, ইউজার তৈরি, রোল পরিবর্তন, ডিঅ্যাক্টিভেশন, অথ-ডিনায়াল |
| **স্টার্টআপ গেট** | প্রোডাকশনে ডিফল্ট `JWT_SECRET` থাকলে অ্যাপ **বুটই হবে না** |
| **WebSocket** | Twilio মিডিয়া স্ট্রিম এখন সাইন করা, কল-নির্দিষ্ট, ১২০ সেকেন্ডের টোকেন চায় |

পুরো বিবরণ, থ্রেট মডেল আর প্রোডাকশন চেকলিস্ট: **[`docs/AUTH.md`](docs/AUTH.md)**

```
POST /auth/login      /auth/refresh    /auth/logout    /auth/logout-all
GET  /auth/me         /auth/roles
GET  /api/team/users  POST /api/team/users
PATCH /api/team/users/{id}/role        /api/team/users/{id}/active
GET  /api/team/audit
```

---

## 🛠 ৭টা AI Tool

| Tool | কাজ |
|---|---|
| `check_availability` | ক্যালেন্ডার দেখে **সর্বোচ্চ ৩টা** স্লট বলে (১২টা পড়লে কল মরে) |
| `book_appointment` | বুক করার আগে **আবার** চেক করে (কথা বলার ফাঁকে স্লট চলে যেতে পারে) |
| `answer_question` | knowledge_base থেকে — **না জানলে বানায় না**, message নেয় |
| `qualify_lead` | ০–১০০ স্কোর + hot lead SMS |
| `take_message` | মালিককে SMS |
| `escalate_to_human` | **আসল** `<Dial>` ট্রান্সফার |
| `mark_do_not_call` | স্থায়ী DNC |

---

## 🌐 API (২০+)

```
POST   /telephony/voice              ইনকামিং কল (IVR অথবা সরাসরি AI)
POST   /telephony/ivr                মেনু কী-প্রেস
POST   /telephony/outbound-answer    আউটবাউন্ড উঠলে (voicemail detect)
WS     /telephony/ws                 মিডিয়া স্ট্রিম
POST   /channels/message             SMS + WhatsApp (একটাই)

GET    /api/llm/presets              ChatGPT / Claude / Gemini ড্রপডাউন
GET    /api/languages                ১৩ ভাষা + কোন STT মডেল
PATCH  /api/tenants/{id}/voice       লাইভ টিউনিং
GET    /api/tenants/{id}/ivr         ফ্লো পড়া
PUT    /api/tenants/{id}/ivr         ফ্লো লেখা (ভাঙা হলে 422)
POST   /api/ivr/validate             এডিটরের লাইভ ভ্যালিডেশন
POST   /api/tenants/{id}/leads       বাল্ক ইমপোর্ট (ডুপ্লিকেট বাদ)
GET    /api/tenants/{id}/leads       স্কোর অনুযায়ী সাজানো
POST   /api/tenants/{id}/campaigns   ক্যাম্পেইন
POST   /api/.../campaigns/{id}/run   ব্যাচ ডায়াল (dry_run=true ডিফল্ট)
POST   /api/compliance/check-message পাঠানোর আগে যাচাই + সেগমেন্ট
GET    /api/compliance/a2p-checklist ক্লায়েন্টকে পাঠানোর লিস্ট
GET    /api/tenants/{id}/stats       ড্যাশবোর্ড
```

---

## 🎛️ লাইভ টিউনিং — ডিল ক্লোজ করার মুহূর্ত

```
ক্লায়েন্ট : "রোবটটা আমার কথার মাঝে কেটে দিচ্ছে"
আপনি     : PATCH /api/tenants/{id}/voice  {"vad_stop_secs": 0.60}
আপনি     : "আবার কল দিন"
ক্লায়েন্ট : "ঠিক হয়ে গেছে!"
```

| নব | রেঞ্জ | ডিফল্ট |
|---|---|---|
| `vad_stop_secs` | 0.30 দ্রুত ↔ 0.70 নিরাপদ | 0.45 |
| `temperature` | 0.2 রোবটিক ↔ 0.8 স্বাভাবিক | 0.65 |
| `speech_speed` | 0.9 ↔ 1.15 | 1.0 |
| `llm_preset` | fast / natural / cheap / smart | natural |
| `humanize` | true / false | true |

---

## 🤖 চারটা প্রিসেট

| প্রিসেট | AI | কখন |
|---|---|---|
| `fast` | ChatGPT gpt-4o-mini | tool calling নিখুঁত |
| `natural` | Claude Haiku | সবচেয়ে মানুষের মতো **← ডিফল্ট** |
| `cheap` | Gemini Flash | সস্তা, বহুভাষী |
| `smart` | Claude Sonnet | জটিল কথোপকথন |

একটা key না থাকলে নিজে থেকেই পরেরটায় যায়।

---

## 📁 গঠন

```
app/
├── agent/       llm_factory · humanize · pipeline · prompts · functions · text_agent
├── channels/    messaging (SMS + WhatsApp)
├── telephony/   twilio_handler · outbound · transfer · ivr
├── integrations/ google_calendar · crm · reminders · notifications
├── core/        config · logging · i18n · compliance
├── db/          models · session
└── api/         routes
alembic/versions/0001_baseline.py     পুরো স্কিমা
scripts/scheduler.py                  আলাদা প্রসেস (আটকে গেলেও কল ধরা বন্ধ হবে না)
tests/            ১৪৫টা
```

---

## ⚠️ সৎ সতর্কবার্তা

**যা কোডে প্রমাণিত:** ১৪৫টা টেস্ট পাস — TCPA উইন্ডো, ব্যাকঅফ, DNC, লিড স্কোর, IVR ভ্যালিডেশন, GSM-7/UCS-2 সেগমেন্ট, ভাষা fallback, opt-out কীওয়ার্ড, CRM ম্যাপিং।

**যা এখনো আসল API-র বিপরীতে যাচাই হয়নি:**
- pipecat 0.0.55-এর import path (লাইব্রেরিটা দ্রুত বদলায়)
- `AnthropicLLMService` / `GoogleLLMService`-এর `InputParams`
- মডেল নাম (`claude-haiku-4-5`, `gemini-2.0-flash`)
- Twilio Trust Hub-এর policy SID
- **প্রকৃত latency**

**সবচেয়ে জরুরি:** প্রথম আসল ফোন কলের আগে এর কোনোটাই প্রমাণিত না। `make up` চালান, নিজের নম্বরে কল দিন, যা ভাঙে ঠিক করুন।

**কোড ৩০%, টিউনিং ৭০%।**

---

## পরের ধাপ

1. Twilio অ্যাকাউন্ট + US নম্বর — ২০ মিনিট, ফ্রি
2. `.env` ভরুন → `make up` → `make seed`
3. ngrok দিয়ে `/telephony/voice` webhook বসান
4. নিজের নম্বরে কল দিন
5. যা অস্বস্তিকর লাগে লিখে রাখুন → `vad_stop_secs` / `temperature` টিউন করুন
6. স্ক্রিন রেকর্ডিং = আপনার Fiverr gig, Upwork proposal, cold email — সব
