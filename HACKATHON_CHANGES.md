# What existed before the hackathon, and what was built during it

## Existing before
- **`app/detection_core.py`**: the graph scoring engine from the author's earlier project, *Abuse-Ring Sentinel* (built for a different event). It links accounts through shared attributes and scores clusters. It is reused as is, and only this file predates the hackathon.

## Built during the hackathon (everything else)
- **PayPal integration:** Orders v2 (create, get, authorize, capture), Payments v2 (capture authorization, void, refund) with idempotency keys, signature-verified webhooks, Disputes (get, provide evidence, send message).
- **Authorize-screen-capture checkout** with a server-set price, checkout binding and replay protection, held-buyer blocking before capture, and atomic order transitions.
- **Screening rules:** core versus peripheral evidence, sticky identifiers, and the device-spread rule (added after the self-test exposed a slow-ring evasion).
- **AI investigator** (tool-calling model) with deterministic guardrails (schema validation, allow-listed actions, core-only hold and refund, mandatory caveats), single-shot and offline fallbacks, and read-only PayPal lookups through the PayPal Agent Toolkit core.
- **Explainable, replayable decisions:** one pure decision function, a versioned policy, stored evidence snapshots with hashes, signal provenance, counterfactuals and replay.
- **Payment truth:** Verify-with-PayPal comparison, unknown-outcome handling with reconcile, retry-safe refunds, a hash-chained audit ledger and a case evidence export.
- **Self-test and AI red team:** synthetic evaluation, and a separate model that invents attacks which we replay (offline sample and quota limits included).
- **Merchant dashboard** (AG Grid tables, ring graph, held-orders panel, activity timeline, no-login replay), the store and result pages, the admin lock.
- **Disputes:** webhook storage, linking to buyers and rings, evidence drafting and sending.
- **Deployment** (Render, Postgres), documentation, and the automated test suite.

## Commit history (generated from git)
- 2026-10-28  Initial RingHunter scaffold
- 2026-10-28  Lock closed cases from re-investigation
- 2026-10-29  Restore main.py; live PayPal sandbox checkout verified
- 2026-10-29  Supersede grown cases; drop fake signup signal; device override for demos
- 2026-10-30  Core vs peripheral members; block held payers before capture
- 2026-10-31  Render deployment config, Postgres support, lifespan startup
- 2026-10-31  Dispute handler: store, link to rings, draft and submit evidence
- 2026-10-31  Evidence note never claims shipping without tracking
- 2026-10-31  Groq gpt-oss investigator with guardrails, fallbacks, calibrated wording
- 2026-10-31  Dashboard with AG Grid and ring graph; admin token on state-changing endpoints
- 2026-11-01  Dashboard theme fixes and demo reset
- 2026-11-01  Per-action approval state; optional refund offer; more-info keeps case open
- 2026-11-02  README for hackathon submission
- 2026-11-02  Pre-capture screening: authorize, screen, capture or void; orders panel
- 2026-11-02  Sticky account identifiers
- 2026-11-02  Dashboard: in-page confirmations, visible messages
- 2026-11-02  Dashboard: readable errors, inline status line
- 2026-11-02  Self-test panel, device-spread rule, README update
- 2026-11-02  Dashboard: Captured is net of refunds; add Refunded tile
- 2026-11-03  No-login replay walkthrough
- 2026-11-03  AI red team; fix chain scenario in self-test
- 2026-11-04  Design polish, activity timeline, red-team quota limits
- 2026-11-04  README: PayPal toolkit decision
- 2026-11-04  PayPal Agent Toolkit for the analyst's read-only lookups
- 2026-11-05  Payment integrity: server price, checkout binding, atomic orders, fail-closed admin; docs
- 2026-11-05  Add dated commit history
- 2026-11-06  Replayable, explainable decisions with provenance and counterfactuals
- 2026-11-08  Payment truth: verify, reconcile, safe refunds, audit ledger, evidence export
- 2026-11-11  Add dispute workflows and evaluation improvements
