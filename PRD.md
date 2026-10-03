# Product Requirements Document — Garden Swap App (working title)

**Status:** Draft v1 — 2026-09-27
**Author:** Pepper (for Adithya)
**Note:** Exploration-stage PRD. No build commitment. MVP scope is marked per section.

---

## 1. Product overview

A hyperlocal, credit-based exchange network for home gardeners covering the full
lifecycle of growing: **swap seedlings, share harvests, pick from neighbors' trees,
and get plant care while away.** One fungible credit system ties all four together
so value flows across seasons and between strangers.

**The thesis:** existing apps are dumb listing boards. This one is a *matching +
trust + credit* system where supply finds demand proactively, freshness is
first-class, and every completed exchange builds a reputation graph that powers
the paid services layer later.

## 2. Goals and non-goals

**Goals**
- Achieve liquidity in ONE geography first (single metro, e.g. Phoenix East Valley)
  before expanding. Density > reach.
- Make the marginal cost of listing ~30 seconds (photo + a few taps).
- Credits make multilateral exchange possible; no user should need a direct
  barter partner.
- Every pillar feeds the others: pick-your-own earns credits spent on seedlings,
  seedling swaps build the trust graph that enables plant sitting.

**Non-goals (v1)**
- No shipping of plants (regulatory headache; hyperlocal only).
- No in-app ads.
- No dynamic pricing / auction mechanics for credits.
- No desktop web app; mobile-first (iOS + Android). Mobile web for browsing only.

## 3. Personas

- **Maya, the seed starter** — starts 200 tomato seedlings, needs 20. Lists the
  surplus. Wants them gone within days (they die on her shelf).
- **Dev, the fruit-tree owner** — has a Meyer lemon tree producing 60 lbs he can't
  use. Will not pick/pack/deliver anything; will let neighbors pick if it's
  zero effort.
- **Rosa, the traveler** — 40 houseplants, travels 3 weeks in summer. Needs a
  trusted sitter, will pay real money.
- **Sam, the new gardener** — just started, has nothing to give yet. Needs a
  low-friction on-ramp.

## 4. Pillar 1 — Seedling swap

**User stories**
- As Maya, I list 30 extra seedlings in under a minute so they get claimed before
  they outgrow their pots.
- As Sam, I set what I want to grow this season and get notified when a neighbor
  lists it, instead of browsing.

**Requirements**
- **Listing flow:** photo (required, min 1), plant variety (free text + autosuggest
  from common list), quantity, pot size / age, pickup window (default 4 days),
  location fuzzed to ~0.5 mi until a swap is agreed.
- **Freshness-first ranking:** listings ranked by *time remaining*, not recency.
  A seedling with 1 day left outranks one listed an hour ago with 6 days left.
- **Viability window:** every listing has an expiry (default 7 days, max 14).
  Expired listings auto-archive; giver gets a nudge at 48h and 12h remaining.
- **Want-list matching:** users maintain a seasonal want-list (variety + zone-aware
  planting window). When a matching listing appears within their radius, push
  notification. Matching is the primary discovery mechanism; browse is secondary.
- **Claim flow:** claim → giver confirms → pickup arranged in chat → both confirm
  completion → credits issued. Either side can cancel before completion; no credits
  move on cancel.
- **No-show handling:** 2 no-shows = 30-day claim suspension. Tracked per user.

## 5. Pillar 2 — Hyperlocal plant sitting (with reviews)

**User stories**
- As Rosa, I find a sitter with verified reviews from people within 3 miles,
  book dates, pay in-app, and get photo updates.
- As a sitter, I build a profile from completed swaps (trust graph carries over)
  and get booked for paid gigs.

**Requirements**
- **Profiles:** photo, bio, service area (zip codes), services offered (drop-in
  watering, vacation care, boarding), per-visit pricing set by sitter, availability
  calendar.
- **Trust graph bootstrap:** completed swaps and pick-your-own visits count toward
  sitter reputation. A user with 15 completed swaps starts sitting with a visible
  history — this is the unfair advantage over standalone sitter apps.
- **Reviews:** only from completed bookings (verified-booking badge). Two-sided:
  owners review sitters, sitters review owners (access, clarity of instructions).
- **Booking flow:** dates → services → in-app payment (hold until completion) →
  care instructions form (per-plant notes, photo of setup) → visit check-ins with
  photos → completion → payment released minus platform fee → reviews.
- **Identity verification:** government-ID check required before a sitter's first
  paid booking. Background checks: offer as paid add-on for sitters (trust badge),
  required for boarding (plants come to sitter's home).
- **Safety:** in-app messaging only until booking confirmed; exact address shared
  only post-booking; emergency contact + cancellation policy per sitter.
- **Platform fee:** X% on paid bookings (to be set; 15–20% starting point).
  Fee funds verification, support, and dispute resolution.

## 6. Pillar 3 — Pick-your-own

**User stories**
- As Dev, I list my lemon tree once per season; neighbors harvest it; I do nothing.
- As a picker, I get an alert that 3 trees near me are ripe this week and claim a
  Saturday slot.

**Requirements**
- **Tree listing:** variety, location (fuzzed), estimated yield, ripe window
  (start–end dates, editable), per-picker limit (e.g. 5 lbs), pickup rules
  (daylight only, which entrance, owner present or not).
- **Spray disclosure (required field):** unsprayed / organic / conventional /
  unknown. Shown prominently on every listing.
- **Harvest alerts:** the core loop. When a tree enters its ripe window, push to
  users within radius who opted into that fruit category. Not a browse experience —
  an event stream.
- **Slot claiming:** owner defines slots (day + time window + max pickers).
  Claiming a slot costs credits (owner earns) or cash if owner sets a price.
- **First-timer gating:** owner-present required for a picker's first 3 visits OR
  until picker has 3+ completed swaps. After that, solo pickup unlocked per owner's
  setting.
- **Post-visit:** picker confirms lbs taken; owner confirms; credits move; both
  can review (tree condition respected? fruit as described?).
- **Damage/abuse:** 1 verified complaint = warning; 2 = 90-day pick suspension.

## 7. Pillar 4 — Harvest swap

**User stories**
- As a gardener with 20 lbs of zucchini, I list the surplus; neighbors claim it
  for credits or free.

**Requirements**
- **Listing flow:** same as seedlings (photo, variety, quantity, pickup window)
  plus optional "free" flag (donation — no credits change hands, still counts
  toward reputation).
- **Perishability tiers:** harvest listings default to 48h expiry (produce rots
  faster than seedlings decline). Giver can extend to 5 days max.
- **Batch claims:** allow partial claims (take 5 of 20 lbs); listing stays live
  until quantity hits zero or expiry.
- **Free vs credit:** giver chooses per listing. Free listings are always allowed
  and encouraged; the "free swaps forever" promise is a brand pillar.

## 8. Credit system (cross-pillar)

**Principles**
- Credits are **fungible across all four pillars and all users**. Earn anywhere,
  spend anywhere. This is load-bearing — without it, the system collapses to
  bilateral barter.
- Credits are **not money**: non-transferable outside the app, non-cashable,
  no cash-out. Arcade tokens, not currency.

**Requirements**
- **Issuance:** credits minted only on *completed* exchanges with both-side
  confirmation. Flat 1 credit per completed swap/pickup by default; giver may set
  1–3 credits per listing (bounded, set at listing time, visible upfront).
- **New-user bootstrap:** 3 starter credits on signup (enables Sam to claim before
  giving).
- **Seasonal expiry:** credits expire at season end (two seasons/year: Mar–Sep,
  Oct–Feb, configurable per hemisphere). 30-day and 7-day expiry warnings.
  Rationale: expiry creates velocity; hoarding kills the economy.
- **Anti-gaming:**
  - Max 10 credits earned per user per week (tunable).
  - Both-side completion confirmation required; disputed completions freeze
    issuance pending review.
  - New accounts (< 14 days) capped at 5 claims/week. **Removed
    2026-10-02 (product decision):** no new-account claim cap — claiming
    is what keeps the swap ecosystem going, so new accounts are
    encouraged to claim freely. Server enforcement (API-147) removed.
  - Device fingerprinting removed — the `X-Device-Fingerprint` header is no
    longer collected (privacy review M17); phone verification at signup.
- **Negative balances:** not allowed. If a user has 0 credits, they must give
  before claiming (starter credits are granted once per account, not
  seasonally — reconciled per review M22; the credits module docstring is the
  source of truth).
- **Ledger:** append-only transaction log per user (earned/spent/expired with
  references to the exchange). Auditable by user; support can inspect.
- **Supply-health metric:** active listings per weekly-active user, tracked as the
  #1 economy-health KPI. Alert threshold: < 0.3 listings/WAU triggers supply
  interventions (nudge campaigns, starter-credit refresh).

## 9. Trust & safety (cross-cutting)

- Phone + device verification at signup (required).
- Government-ID verification required for: paid sitting bookings (sitters),
  solo pick-your-own access.
- Location fuzzing: exact address hidden until a swap/booking is confirmed.
- In-app messaging with photo sharing; phone numbers masked until both parties
  agree to share.
- Review system: verified-completion only, two-sided, 1–5 + tags
  ("on time", "as described", "great communication", "respectful of property").
- Strike system: 2 no-shows → 30-day claim suspension; 2 verified complaints →
  90-day suspension from the relevant pillar; fraud → permanent ban.
- Content: photo required on all listings (reduces spam/fake listings
  dramatically).
- Dispute flow: in-app report → support review SLA 48h → credit reversal if
  warranted.

## 10. Notifications (the re-engagement engine)

- Harvest alerts (pillar 3 ripe windows) — highest priority, seasonal bursts.
- Want-list matches (pillar 1).
- Listing expiry nudges (48h, 12h).
- Credit expiry warnings (30d, 7d).
- Booking reminders (sitting: 24h before, day-of).
- Quiet hours respected; per-category opt-in/out. Notification fatigue kills
  hyperlocal apps — every push must be actionable and local.

## 11. Monetization hooks (built in, not bolted on)

- Platform fee on paid sitting bookings (15–20%).
- Optional cash price on pick-your-own slots and produce sales; platform fee on
  the transaction (free swaps and donations: $0 fee, forever).
- Payment processing via Stripe Connect (or equivalent); on-platform payment
  required for reviews, guarantees, and dispute coverage (anti-disintermediation).
- Later: nursery B2B dashboard, sponsored placements, affiliate supply links.
  Explicitly out of v1.

## 12. Metrics

- **North star:** completed exchanges per WAU per week.
- **Economy health:** listings per WAU, credit velocity (issued → spent median
  days), % of credits expiring unused (target < 25%).
- **Liquidity:** % of listings claimed within 48h; median time-to-claim per pillar.
- **Trust:** % of exchanges with both-side completion, dispute rate (target < 2%),
  review rate (target > 60%).
- **Geography:** WAU density per zip code — expansion gated on hitting liquidity
  thresholds in existing zips.

## 13. MVP scope (v1)

**In:** pillars 1–4 with credit system, want-list matching, harvest alerts,
basic reviews, in-app messaging, Stripe Connect for sitting payments, ID
verification for sitters.
**Out:** nursery B2B, affiliate links, advanced analytics dashboard, background
checks (ID only in v1), multi-metro expansion tooling, iOS/Android feature parity
gaps acceptable in beta (launch Android-first if resources constrained —
matches the team's on-device verification setup).

## 14. Open questions

1. Flat 1-credit vs bounded 1–3 per listing — decision needed before build.
2. Season boundaries for credit expiry (hemisphere-aware?).
3. Platform fee % — needs comp analysis (Rover et al.).
4. Cottage-food / produce-sale regulations per launch state.
5. Who builds? (Authorship/ownership conventions per team norms.)
6. Launch geography: Phoenix East Valley (Tempe/Mesa/Chandler) assumed — confirm.
7. Name, branding — TBD.
