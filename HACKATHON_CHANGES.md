# What existed before the hackathon, and what was built during it

## Existing before
- **`app/detection_core.py`**: the graph scoring engine from the author's earlier project, *Abuse-Ring Sentinel* (built for a different event). It links accounts through shared attributes and scores clusters. It is reused as is, and only this file predates the hackathon.

## Built during the hackathon (everything else)
- **PayPal integration:** Orders v2 (create, get, authorize, capture), Payments v2 (capture authorization, void, refund) with idempotency keys, signature-verified webhooks, Disputes (get, provide evidence, send message).
- **Authorize-screen-capture checkout** with a server-set price, checkout binding and replay protection, held-buyer blocking before capture, and atomic order transitions.
- **Screening rules:** core versus peripheral evidence, sticky identifiers, and the device-spread rule (added after the self-test exposed a slow-ring evasion).
- **AI investigator** (tool-calling model) with deterministic guardrails (schema validation, allow-listed actions, core-only hold and refund, mandatory caveats), single-shot and offline fallbacks, and read-only PayPal lookups through the PayPal Agent Toolkit core.
- **Self-test and AI red team:** synthetic evaluation, and a separate model that invents attacks which we replay (offline sample and quota limits included).
- **Merchant dashboard** (AG Grid tables, ring graph, held-orders panel, activity timeline, no-login replay), the store and result pages, the admin lock.
- **Disputes:** webhook storage, linking to buyers and rings, evidence drafting and sending.
- **Deployment** (Render, Postgres), documentation, and the automated test suite.

## Commit history (generated from git)
