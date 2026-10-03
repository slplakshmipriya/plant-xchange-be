# Jira Backlog — Garden Swap App

**Source:** `PRD.md` (v1, 2026-09-27) + Garden Swap App UI Design board
**Status:** Draft — no Jira board exists; ticket keys are proposed and stable.

## Assumptions (locked)

1. **Middleware:** Google Cloud Run + Neon Postgres (same pattern as the
   trade-agent-guard oversight proxy: Dockerfile respects `$PORT`, DB-backed
   state for scale-to-zero correctness, no in-memory session state).
2. **Tracking:** Firebase — Analytics (event taxonomy below), Crashlytics,
   Cloud Messaging (push), Auth (phone auth).
3. **Client:** Android only in current scope. Gradle config mirrors BareLabel
   (`~/workspace/true-food`): AGP 8.5.2, google-services 4.4.2, compileSdk 35,
   targetSdk 35, minSdk 23, firebase-bom 33.7.0, JUnit 4 JVM regression suite
   wired to `assembleDebug` (`testDebugUnitTest` fails the build), secrets via
   git-ignored `local.properties` + `buildConfigField`, release signing from
   `local.properties`, `minifyEnabled`/`shrinkResources` on release.
4. **Payments:** Stripe Connect (platform + connected accounts for sitters /
   paid pick slots). Webhooks verified by signature.

## Conventions

- Epics: `EPIC-MVP-n`, `EPIC-P2-n`, `EPIC-P3-n`
- Tickets: `AND-xxx` (Android), `API-xxx` (Cloud Run backend), `SEC-xxx`
  (security/vulnerability), `ANL-xxx` (analytics/tracking)
- Every ticket: Priority (P0–P3), Story points (Fibonacci), Acceptance criteria.
- P0 = blocks launch. P1 = MVP but sequenced after P0. P2 = phase enhancements.
- "Done" for any ticket includes: unit tests where logic lives, Firebase
  Analytics events per the taxonomy (§12), no new Crashlytics issues, PRD
  section referenced.

---

# PHASE: MVP

## EPIC-MVP-1 — Foundations: Android scaffold, Cloud Run + Neon, Firebase

**Objective:** Standing skeleton both sides can build on. Nothing user-facing.

### Backend
- **API-001** [P0, 5] Provision Cloud Run service + Neon Postgres
  - AC: Service deploys from `main` via Cloud Build; `$PORT` respected; Neon
    branch per env (dev/staging/prod); health endpoint `/healthz` returns 200;
    DB migrations run on deploy (no manual SQL).
- **API-002** [P0, 5] API authN/authZ framework
  - AC: Firebase Auth ID-token verification middleware on all routes except
    `/healthz`; per-user scoping on every query (no cross-user reads); 401 on
    missing/invalid token; 403 on scope violation. Integration test covers both.
- **API-003** [P0, 3] Shared middleware: logging, rate limiting, error envelope
  - AC: Structured JSON logs with request id; per-IP + per-user rate limits
    (tunable env vars); uniform error envelope `{code, message, request_id}`;
    429 on limit breach. No stack traces in prod responses.

### Frontend
- **AND-001** [P0, 5] Android scaffold from BareLabel Gradle template
  - AC: `build.gradle` mirrors BareLabel (AGP 8.5.2, SDK 35/35/23,
    firebase-bom 33.7.0, `local.properties` secrets, release signing block,
    `assembleDebug` → `testDebugUnitTest` gate); package
    `com.gardenswap.app`; debug + release variants build; app launches to
    placeholder home.
- **AND-002** [P0, 3] Firebase wiring: Auth, Analytics, Crashlytics, FCM
  - AC: `google-services.json` per variant (git-ignored, documented setup);
    Analytics + Crashlytics initialize on launch; FCM token registered to
    backend on login; test push delivers to a debug device.

### Security
- **SEC-001** [P0, 3] Secrets management
  - AC: No secrets in repo (gitleaks/CI check); Stripe keys, Neon URLs via
    Secret Manager → env; `local.properties` git-ignored; documented rotation
    runbook.
- **SEC-002** [P0, 2] Dependency vulnerability scanning in CI
  - AC: Gradle dependency check + `npm`/pip audit (backend) run on every PR;
    build fails on CRITICAL; weekly scheduled scan files issues.

---

## EPIC-MVP-2 — Identity, profiles, verification

**Objective:** Know who users are; gate trust-sensitive actions on verification.
(PRD §9)

### Backend
- **API-010** [P0, 5] User profile service
  - AC: `users` table (id, phone hash, display name, avatar URL, home zip,
    created_at); CRUD with owner-only write; PII fields never in logs.
- **API-011** [P0, 5] Phone verification via Firebase Auth
  - AC: Signup requires verified phone; one account per phone number;
    device fingerprint stored; duplicate-phone signup rejected with safe error.
- **API-012** [P0, 8] Government-ID verification flow (sitters, solo-pickup)
  - AC: Integration with IDV provider (e.g. Stripe Identity / Didit);
    webhook-verified results stored as enum
    (unverified/pending/verified/failed); `verified` badge exposed on profile;
    ID images never stored on our infra (provider-hosted only).

### Frontend
- **AND-010** [P0, 5] Onboarding: phone auth → profile → home zip
  - AC: Firebase phone-auth UI; profile form (name, avatar via camera/gallery);
    home zip required (drives radius logic); matches design board onboarding.
- **AND-011** [P0, 5] ID verification UI + verification badges
  - AC: IDV provider SDK flow; badge renders on profile/sitter cards;
    graceful failure states; re-try path.

### Security
- **SEC-010** [P0, 3] PII minimization & access control
  - AC: Exact address/phone never returned by list endpoints; audit log on
    profile reads by non-owners (support tooling); data-deletion endpoint
    (GDPR/CCPA) purges user + anonymizes ledger references.

---

## EPIC-MVP-3 — Listings platform (shared engine)

**Objective:** One listing engine serving seedling, harvest, and tree listings.
(PRD §4, §7; design: listing detail + create flow)

### Backend
- **API-020** [P0, 8] Listing CRUD + lifecycle state machine
  - AC: `listings` table (type: seedling|harvest|tree, photos[], variety,
    quantity, credit_cost 1–3, pickup window, expiry_at, geo fuzzed point,
    spray_disclosure, status: draft|live|claimed|completed|expired|cancelled);
    state transitions validated server-side (no live→completed skip);
    expiry job archives listings (Cloud Scheduler, idempotent).
- **API-021** [P0, 5] Freshness-ranked feed
  - AC: `GET /feed` ranks by time-remaining within radius (default 5 mi,
    tunable); pagination cursor-based; fuzzed location (~0.5 mi) until
    exchange confirmed; response time p95 < 400 ms at 10k listings.
- **API-022** [P0, 3] Photo upload pipeline
  - AC: Signed-URL upload to Cloud Storage; server-side validation
    (type, size ≤ 8 MB, strip EXIF GPS); thumbnail generation; photo required
    enforced on publish.

### Frontend
- **AND-020** [P0, 8] Create-listing flow (per design board)
  - AC: Photo (camera/gallery, required) → variety (autosuggest) → quantity →
    credit cost (1–3 stepper) → pickup window → spray disclosure (required) →
    review → publish; draft autosave; matches design board screens.
- **AND-021** [P0, 5] Listing detail + claim flow
  - AC: Detail per design (photo, freshness countdown, credit cost, giver stats);
    Claim button → confirm sheet → chat opens; cancel path; expired state UI.

### Analytics
- **ANL-020** [P0, 2] Listing funnel events
  - AC: `listing_create_start/complete`, `listing_claim`, `listing_expire`,
    `listing_view` fire with listing_id/type/credit_cost; funnel visible in
    Firebase console.

---

## EPIC-MVP-4 — Seedling swap + want-list matching

**Objective:** The proactive matching loop — the core differentiator. (PRD §4)

### Backend
- **API-030** [P0, 8] Want-list service + match engine
  - AC: `want_lists` (user, variety, radius, season); on listing publish, match
    job fans out to matching want-lists (variety synonym map, e.g.
    "tomato" ↔ "tomatoes"); match = push-eligible event; dedupe (one alert per
    listing per user).
- **API-031** [P0, 3] No-show tracking + enforcement
  - AC: `no_shows` counter; 2 in 90 days → 30-day claim suspension (job
    enforced, appeal path via support); surfaced on profile.

### Frontend
- **AND-030** [P0, 5] Want-list UI (per design board)
  - AC: Add/remove wants with variety autosuggest; season auto-set from zone;
    matched listings section; push opt-in per category.
- **AND-031** [P1, 3] Expiry nudge surfaces
  - AC: "Expires in 48h/12h" banners on own listings; one-tap extend (max 14d).

### Analytics
- **ANL-030** [P0, 2] Matching effectiveness
  - AC: `wantlist_match_sent`, `wantlist_match_claimed` events; dashboard:
    match→claim conversion; target > 15% in MVP review.

---

## EPIC-MVP-5 — Harvest swap

**Objective:** Surplus-produce exchange with perishability tiers. (PRD §7)
Mostly reuses EPIC-MVP-3 engine; delta tickets only.

### Backend
- **API-040** [P0, 3] Harvest listing rules
  - AC: type=harvest defaults: 48h expiry (max 5d); partial claims supported
    (quantity decremented, listing stays live); `is_free` flag bypasses credit
    charge but still requires both-side completion for reputation.

### Frontend
- **AND-040** [P0, 3] Harvest-specific UI deltas
  - AC: Quantity stepper with units (lbs/bags/each); "Free" toggle prominent;
    partial-claim sheet ("take 5 of 20 lbs").

---

## EPIC-MVP-6 — Pick-your-own + harvest alerts

**Objective:** Zero-effort supply: tree listings, ripe-window alerts, slot claiming.
(PRD §6; design: tree detail)

### Backend
- **API-050** [P0, 8] Tree listings + ripe-window alert engine
  - AC: `trees` table (variety, yield estimate, ripe_start/ripe_end, per-picker
    limit, pickup rules JSON, spray_disclosure required); scheduler flips
    `is_ripe` on window start → alert fan-out to opted-in users in radius;
    owner can shift window (re-alerts once).
- **API-051** [P0, 5] Slot claiming
  - AC: `slots` (tree_id, start, end, max_pickers); atomic claim (no
    double-book under concurrency — DB constraint + test); claim charges
    credits or cash per owner setting; cancellation releases slot.

### Frontend
- **AND-050** [P0, 5] Tree detail + slot picker (per design board)
  - AC: Spray disclosure badge prominent; ripe countdown; slot grid;
    first-timer gating messaging ("owner present required for your first
    3 visits"); post-visit lbs-taken confirmation.

### Security
- **SEC-050** [P0, 3] Solo-pickup gating enforcement
  - AC: Server enforces: solo slots claimable only if picker has ≥3 completed
    exchanges AND ID verified; bypass attempts logged + rejected.

---

## EPIC-MVP-7 — Credit ledger economy

**Objective:** Fungible, non-monetary credits across all pillars. (PRD §8;
design: wallet)

### Backend
- **API-060** [P0, 8] Append-only ledger + balances
  - AC: `ledger` table (user_id, delta, reason, ref_exchange_id, expires_at);
    `balances` materialized (recomputed, never hand-edited); all mutations in
    DB transactions; double-spend impossible under concurrency (tested).
- **API-061** [P0, 5] Issuance, expiry, caps
  - AC: Mint on both-side completion only; 3 starter credits on signup;
    seasonal expiry job (2 seasons/yr, warnings at 30d/7d via push);
    10 credits/week earning cap; new accounts (<14d): 5 claims/week cap
    [REMOVED 2026-10-02 — see PRD].
- **API-062** [P0, 3] Dispute-driven reversal
  - AC: Support tool reverses a credit movement with audit trail; reversal
    itself is a ledger entry (never delete).

### Frontend
- **AND-060** [P0, 5] Wallet UI (per design board)
  - AC: Balance hero, ledger list (earned/spent/expired with refs), expiry
    countdown banner, "how credits work" explainer sheet.

### Analytics
- **ANL-060** [P0, 3] Economy health dashboard
  - AC: Events `credit_issued/spent/expired`; BigQuery/Firebase dashboard:
    listings/WAU, credit velocity (issue→spend median days), % expired unused
    (alert if > 25%), supply alert if listings/WAU < 0.3.

### Security
- **SEC-060** [P0, 3] Ledger integrity
  - AC: No client-side balance writes (server-authoritative); tamper-evident
    log (hash-chained rows or signed checkpoints); anomaly alert on earning
    velocity outliers.

---

## EPIC-MVP-8 — Plant sitting marketplace + reviews + payments

**Objective:** Paid services layer = the revenue line. (PRD §5; design: sitter
profile)

### Backend
- **API-070** [P0, 8] Sitter profiles + availability + booking
  - AC: `sitters` (services[], per-visit pricing, zip coverage, calendar);
    booking state machine (requested→confirmed→in_progress→completed|
    cancelled); payment hold on confirm (Stripe), capture on completion;
    platform fee 15–20% (configurable) split at capture.
- **API-071** [P0, 5] Stripe Connect integration
  - AC: Express accounts for sitters; webhook signature verification (SEC);
    idempotent webhook handling; payout schedule surfaced; test-mode E2E.
- **API-072** [P0, 5] Two-sided verified reviews
  - AC: Reviews writable only post-completion, once per side; 1–5 + tags;
    aggregated score with Bayesian smoothing (no 5.0-from-1-review);
    review text profanity/basic-moderation filter.

### Frontend
- **AND-070** [P0, 8] Sitter discovery + profile + booking (per design board)
  - AC: Search by zip/service; profile (badges, swap-history-derived trust
    line, reviews); booking sheet (dates, services, price math incl. fee);
    care-instructions form (per-plant notes + photos).
- **AND-071** [P0, 3] Visit check-ins + photo updates
  - AC: Sitter check-in with photo; owner push on each update; completion
    confirm triggers review prompts both sides.

### Security
- **SEC-070** [P0, 5] Payments + PII hardening
  - AC: Stripe webhook signatures verified (reject unsigned); no card data
    touches our servers (Stripe Elements/SDK only); exact address shared only
    post-booking (API enforces); PCI scope documented as SAQ-A.

---

## EPIC-MVP-9 — Messaging & push notifications

**Objective:** Coordination + the re-engagement engine. (PRD §10)

### Backend
- **API-080** [P0, 5] In-app messaging
  - AC: Thread per exchange/booking; text + photos; phone numbers masked until
    mutual share; basic abuse filter (spam/links) with flagging.
- **API-081** [P0, 5] Notification service
  - AC: FCM fan-out for: harvest alerts, want-list matches, expiry nudges
    (48h/12h), credit expiry (30d/7d), booking reminders; per-category opt-in
    stored server-side; quiet hours (21:00–08:00 user tz) except booking
    day-of; dedupe + rate cap (max 5/day/user, tunable).

### Frontend
- **AND-080** [P0, 5] Chat UI + notification prefs
  - AC: Thread list, exchange-context header (listing summary + status),
    photo share, system messages (claim confirmed, slot booked);
    settings screen with per-category toggles.

---

## EPIC-MVP-10 — Launch readiness: hardening, privacy, analytics

**Objective:** Ship criteria. No new features.

### Security / Vulnerability
- **SEC-100** [P0, 5] Pre-launch security review
  - AC: Threat model doc (spoofing/tampering/repudiation/info-disclosure/
    DoS/elevation per pillar); pen-test or structured self-test of auth,
    payments, IDV; all P0 findings fixed.
- **SEC-101** [P0, 3] Abuse & fraud controls
  - AC: Signup velocity limits; fake-listing heuristics (photo required +
    report flow); coordinated-farming detection (graph of claim pairs,
    alert threshold); support ban tool with audit trail.
- **SEC-102** [P0, 2] Data retention & deletion
  - AC: Retention policy (messages 1yr, photos per listing lifecycle);
    user deletion cascades per SEC-010; verified via test.

### Analytics / Tracking
- **ANL-100** [P0, 3] Event taxonomy freeze + dashboards
  - AC: Single `analytics_events.md` spec; every MVP event implemented;
    launch dashboard: north-star (completed exchanges/WAU/week), acquisition
    (install→verified→first listing), economy health (§ANL-060), trust
    (dispute rate < 2%, review rate > 60%).
- **ANL-101** [P1, 2] Crash-free + performance gates
  - AC: Crashlytics: ≥ 99.5% crash-free users; feed p95 < 400 ms;
    cold start < 2.5 s on mid-tier device (moto g stylus class).

### Frontend
- **AND-100** [P0, 3] Release pipeline
  - AC: Internal → closed → production tracks; staged rollout 10→50→100%;
    rollback runbook; release checklist doc.

---

# PHASE 2 — Enhancement (post-launch)

## EPIC-P2-1 — Referral & growth loops
- **API-110** [P2, 5] Invite graph + dual-sided credit rewards; fraud-resistant
  (inviter reward only after invitee completes first exchange).
- **AND-110** [P2, 3] Share sheet, invite screen, reward status UI.
- **ANL-110** [P2, 2] Viral coefficient (k-factor) tracking; invite→activated
  conversion funnel.

## EPIC-P2-2 — Sitter background checks & insurance badges
- **API-111** [P2, 5] Background-check provider integration; badge states;
  required for boarding tier.
- **AND-111** [P2, 3] Sitter onboarding upgrade flow; badge display; owner-side
  filter "background-checked only".
- **SEC-111** [P2, 3] Background-check data handling: provider-only storage,
  consent records, adverse-action flow compliance notes.

## EPIC-P2-3 — Advanced matching & personalization
- **API-112** [P2, 8] Variety synonym/ontology expansion; seasonal planting-window
  awareness per zip (zone data); match ranking by distance + freshness +
  giver reputation.
- **AND-112** [P2, 3] "For you" feed section; match-quality feedback
  ("not relevant" → tunes ranking).
- **ANL-112** [P2, 2] Match→claim conversion by rank position; A/B harness.

## EPIC-P2-4 — Disputes & moderation tooling
- **API-113** [P2, 5] Support console: exchange timeline view, credit
  reversal (via API-062), strike application, ban with audit trail; SLA
  tracking (48h).
- **AND-113** [P2, 2] In-app report flow with categories + photo evidence.
- **SEC-113** [P2, 3] Moderator access controls (role-based, MFA required,
  all actions logged).

---

# PHASE 3 — Scale & monetization expansion

## EPIC-P3-1 — Nursery B2B dashboard (paid)
- **API-120** [P3, 8] Org accounts; demand-signal API (anonymized, zip-level
  want-list aggregates); sponsored placement inventory.
- **AND-120** [P3, 5] (Web-view or responsive) dashboard: demand charts,
  swap-day campaign tool.
- **SEC-120** [P3, 3] Aggregation privacy: k-anonymity thresholds (no
  zip-level data under N users); org data-access audit.

## EPIC-P3-2 — Affiliate supply links
- **API-121** [P3, 3] Curated product-link service with disclosure flags
  (pattern: BareLabel `affiliate_mappings.json` + signature verification).
- **AND-121** [P3, 3] "Supplies for this swap" module; FTC disclosure UI.
- **SEC-121** [P3, 2] Signed mapping feed (detached signature, fail-closed) —
  reuse BareLabel signing pattern.

## EPIC-P3-3 — Multi-metro expansion ops
- **API-122** [P3, 5] Geo-fencing by metro; per-metro season configs;
  launch playbook automation (seed content, alert thresholds per metro).
- **ANL-122** [P3, 2] Per-metro liquidity dashboards; expansion gating rules
  (no new metro until existing hits liquidity thresholds).

## EPIC-P3-4 — iOS app [PARKED]
- Out of current scope (Android-only). Placeholder epic: shared backend already
  platform-agnostic; estimate on demand.

---

## 12. Firebase Analytics event taxonomy (all phases)

`app_open`, `onboarding_complete`, `listing_create_start/complete`,
`listing_view`, `listing_claim`, `listing_expire`, `wantlist_add`,
`wantlist_match_sent/claimed`, `slot_claim`, `harvest_alert_sent/opened`,
`credit_issued/spent/expired`, `booking_requested/confirmed/completed`,
`payment_succeeded/failed`, `review_submitted`, `chat_message_sent`,
`invite_sent/accepted`, `push_opt_out`, `dispute_opened`.
All events carry: `user_id` (hashed), `geo_zip3`, `pillar`, `app_version`.

## 13. Security baseline (applies to all tickets)

- Auth: Firebase ID tokens, short-lived, verified server-side on every call.
- Transport: TLS everywhere; HSTS on Cloud Run.
- Data: Neon encryption at rest; PII fields encrypted at column level where
  feasible; backups tested quarterly.
- CI gates: vulnerability scan (SEC-002), lint, unit tests
  (`testDebugUnitTest` on Android; backend suite on API), no-secrets check.
- Principle of least privilege on Cloud IAM + Neon roles; staging/prod
  separation.

---

# PHASE: PRD parity (audit 2026-09-28)

**Source:** PRD-vs-`ui-feature` deviation audit (corrected: `ui-feature` is the
real implementation branch; mock is fallback only). Keys continue the existing
sequence: **AND-122+** (Android), **API-123+** (backend).

## Dependency map

**Critical path — contract first:**
`API-123` (feed endpoint) → `AND-122` (feed consumption) → `AND-123`
(freshness sort). The Android contract additions below (`getFeed`, `listTrees`,
claim-with-quantity, `cancelClaim`, `reportContent`, `Review.reviewerRole` /
`verifiedBooking`, `ListingInput` new fields) land before the UI tracks.

**Parallel tracks (after contract):**
- **T1 FEED:** AND-122, AND-123, AND-149 (needs API-135)
- **T2 CREATE:** AND-124, AND-125, AND-126, AND-127, AND-128 (form-only)
- **T3 CLAIM:** AND-132, AND-133 (needs API-126); AND-130/AND-131 blocked on
  API-124/API-125
- **T4 SITTER:** AND-137, AND-138, AND-139, AND-140, AND-141, AND-146, AND-148
  (partial); AND-135/136/142/144/145/147 blocked on backend
- **T5 PYO:** AND-149, AND-150 (needs API-136), AND-151 (needs API-137);
  AND-153/154/155/156/157 blocked on backend
- **T6 TRUST:** AND-158 (form now; intake blocked on API-143)
- **T7 NOTIFY:** AND-152; AND-161/AND-162 blocked on API-146
- **T8 WALLET:** AND-163 blocked on API-150

## EPIC-PAR-1 — Feed & discovery

- **API-123** [P0, 8] Ranked feed endpoint `GET /feed`: freshness-first
  ranking (time remaining), paginated, filterable by way
  (seedling/harvest/pick/sitting). AC: OpenAPI regenerated; ranked results.
- **AND-122** [P0, 5] Consume the feed endpoint in Explore: replace the
  mock-only `instanceof MockGardenSwapApi` bail in `loadFeed()` with
  `GardenSwapApi.getFeed()`; proper empty state when the backend has no
  listings. AC: Explore shows backend listings via `HttpGardenSwapApi`; no
  `instanceof Mock` in production code.
- **AND-123** [P1, 3] Client-side freshness sort fallback (by time remaining,
  nulls last) in `ExploreLogic`. AC: unit-tested comparator. Blocked by
  API-123 for server ranking; client sort ships regardless.
- **API-135** [P1, 5] `GET /trees` list endpoint (id, variety, approx location,
  ripe window).
- **AND-149** [P1, 5] Tree list screen; wire the Explore "Pick" way-card to it
  (today it only sets a filter chip); tapping a tree opens `TreeDetailActivity`.
  AC: `TreeDetailActivity` reachable from Explore with a sane back stack.
  Blocked by API-135.

## EPIC-PAR-2 — Listings: create & claim

- **AND-124** [P1, 3] Variety autosuggest in the create-listing form (reuse the
  want-list autosuggest source). AC: suggestions after 2 chars; free text
  still allowed.
- **AND-125** [P1, 2] Pot size + plant age fields in the create-listing form
  (seedlings); passed through `ListingInput`.
- **AND-126** [P1, 2] Pickup window defaults to 4 days in the create form when
  left blank.
- **AND-127** [P1, 3] Expiry: default 7 days, hard cap 14 days, enforced
  client-side with inline error.
- **AND-128** [P1, 2] Harvest perishability: default 48h, max 5 days, enforced
  client-side.
- **AND-129** [P2, 3] Zone-aware planting window on want-list items (derive
  from ZIP + variety).
- **API-124** [P0, 8] Claim lifecycle on the backend: `POST
  /listings/{id}/claims` → PENDING; giver accept/decline endpoints; claim
  states in OpenAPI.
- **AND-130** [P1, 5] Giver claim confirmation UI: pending-claim state,
  accept/decline from listing detail. Blocked by API-124.
- **API-125** [P2, 5] Auto-create listing-scoped chat thread when a claim is
  confirmed.
- **AND-131** [P2, 3] Surface the auto-created pickup thread after claim
  confirm. Blocked by API-125.
- **AND-132** [P1, 3] Claimer-side claim cancellation from listing detail / My
  Swaps via `cancelClaim`.
- **API-126** [P1, 5] Claim quantity support: `quantity` on the claim endpoint;
  decrements listing quantity; partial claims.
- **AND-133** [P1, 3] Wire the claim sheet's quantity stepper into
  `claimListing(listingId, ClaimRequest)`; show remaining quantity. Blocked by
  API-126.
- **API-127** [P2, 8] No-show tracking + 30-day suspension enforcement;
  moderation tooling.
- **AND-134** [P2, 3] No-show / suspension notice surface (banner + profile
  flag). Blocked by API-127.
- **API-136** [P2, 3] Spray as a required enum on the tree model; reject free
  text.
- **AND-150** [P2, 3] Spray disclosure as a required 4-option selector
  (none/organic/synthetic/unknown) on tree create/edit; prominent display on
  detail. Blocked by API-136.
- **API-137** [P2, 3] `PATCH /trees/{id}` ripe-window update.
- **AND-151** [P2, 2] Ripe-window editing on tree detail. Blocked by API-137.

## EPIC-PAR-3 — Sitting & reviews

- **API-128** [P1, 8] Sitter profile fields on the backend: photo (GCS), bio,
  service-area ZIPs, availability calendar model.
- **AND-135** [P1, 5] Sitter profile: photo upload, bio, ZIP service areas,
  availability calendar UI. Blocked by API-128.
- **API-129** [P2, 5] Trust-history endpoint: completed swaps/picks per user
  for sitter profiles.
- **AND-136** [P2, 3] Trust graph: show completed swap/pick history on sitter
  profiles. Blocked by API-129.
- **AND-137** [P2, 2] Verified-booking badge on review cards (render from
  `Review.verifiedBooking`).
- **AND-138** [P2, 3] Two-sided reviews: `reviewerRole` field + sitter→owner
  review entry point.
- **AND-139** [P1, 2] Review tags UI (chips: on time, as described, great
  comms…); submit the selected tags (today the form sends an empty array).
- **AND-140** [P1, 3] Real date picker for booking requests (replace "days
  from today").
- **AND-141** [P1, 3] Per-service picker in the booking sheet (multi-select;
  stop sending all services wholesale).
- **API-130** [P0, 13] Stripe Connect: connected accounts for sitters, payment
  hold on booking, capture/release, webhooks verified by signature.
- **AND-142** [P1, 3] Payment-hold UI: render hold state on the booking.
  Blocked by API-130.
- **AND-143** [P2, 2] Care form: setup-photo attachment.
- **API-131** [P2, 8] Visit check-in model + photo upload endpoints.
- **AND-144** [P2, 5] Visit check-ins with photos. Blocked by API-131.
- **API-132** [P1, 8] `POST /bookings/{id}/complete` + payment release;
  completion state machine.
- **AND-145** [P1, 5] Booking completion / payment-release action in the
  client. Blocked by API-132.
- **AND-146** [P0, 3] Gate the first paid booking on IDV: check
  `getIdvStatus()` before `requestBooking`; route unverified users to
  `IdvActivity`.
- **API-133** [P1, 5] Post-booking details endpoints (service address,
  emergency contact, cancellation policy).
- **AND-147** [P1, 5] Post-booking details UI: service address, emergency
  contact, cancellation policy. Blocked by API-133.
- **API-134** [P1, 5] Booking history endpoint so the reviewer can pick a real
  completed booking.
- **AND-148** [P0, 3] Drop the hardcoded `mock-booking-1` review booking ID;
  resolve the real booking from history; degrade gracefully when history is
  unavailable. Blocked by API-134.

## EPIC-PAR-4 — Pick-your-own

- **API-138** [P1, 8] Harvest alert pipeline: category prefs, ripe-event
  fan-out, alert stream endpoint.
- **AND-153** [P1, 5] Harvest/pick alert category opt-in + alert inbox.
  Blocked by API-138.
- **API-139** [P2, 8] Slot model + claiming endpoints for pick-your-own.
- **AND-154** [P2, 5] Slot claiming UI (day/time/max pickers, credit or cash).
  Blocked by API-139.
- **API-140** [P2, 3] Visit counting per picker/tree for first-timer
  enforcement.
- **AND-155** [P2, 2] First-timer gating surface (explain + enforce on slot
  claim). Blocked by API-140.
- **API-141** [P2, 5] Post-visit confirm endpoint (lbs, credit transfer,
  review prompts).
- **AND-156** [P2, 3] Post-visit confirm UI (lbs picked, credit move,
  both-side review prompt). Blocked by API-141.
- **API-142** [P2, 5] Complaint intake + strike counting.
- **AND-157** [P2, 3] Damage/abuse complaint filing UI. Blocked by API-142.
- **API-148** [P2, 5] Cash pricing support on PYO slots and produce listings.
- **AND-165** [P2, 3] Optional cash price on PYO slots / produce listings.
  Blocked by API-148.

## EPIC-PAR-5 — Trust, safety, notifications, credits

- **AND-152** [P0, 5] Push receive path: `FirebaseMessagingService` subclass,
  manifest entry, notification channels, tap → deep link. (Token registration
  already exists; today nothing can display.)
- **API-143** [P1, 8] Report/dispute intake + SLA workflow + moderator
  tooling.
- **AND-158** [P1, 3] In-app report/dispute form (listing, user, booking) with
  categories; calls `reportContent`. Intake blocked by API-143; form ships
  now.
- **API-144** [P2, 8] Chat attachment upload + storage.
- **AND-159** [P2, 5] Chat photo sharing. Blocked by API-144.
- **API-145** [P2, 5] Proxy/masked calling or number-share consent state.
- **AND-160** [P2, 3] Phone masking / share-consent UI in chat. Blocked by
  API-145.
- **API-146** [P1, 8] Push event pipeline: booking reminders, credit-expiry
  warnings, quiet-hours respect.
- **AND-161** [P2, 3] Booking reminders + quiet-hours client surface. Blocked
  by API-146.
- **AND-162** [P2, 2] Credit-expiry push client surface. Blocked by API-146.
- **API-147** [P2, 5] New-account claim caps enforced server-side.
  [REMOVED 2026-10-02 — rule dropped, see PRD]
- **AND-164** [P2, 2] New-account claim-cap notice (<14d: 5 claims/week).
  [VOID 2026-10-02 — rule dropped]
  Blocked by API-147.
- **API-149** [P0, 13] Stripe Connect platform integration (see also API-130).
- **API-150** [P1, 5] Seasonal credit-expiry batch job + 30-day warning
  events.
- **AND-163** [P2, 3] 30-day credit-expiry warning + season-boundary label in
  Wallet. Blocked by API-150.
