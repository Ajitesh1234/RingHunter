# RingHunter

**Stops coordinated buyer rings at checkout, before any money moves. An AI analyst explains each case, and the merchant approves every action.**

Built for the PayPal AI Hackathon. PayPal **sandbox only**; no real money moves.

**Live demo:** https://ringhunter.onrender.com (free tier: the first load can take about a minute to wake) · **Dashboard:** `/dashboard` · **API docs:** `/docs`

## The problem
Small merchants lose money to rings of accounts that share devices and addresses and buy or dispute in coordinated bursts. PayPal shows each payment on its own, so the pattern is easy to miss, and acting on a hunch can punish honest customers.

## What it does
1. **Authorize first.** The demo checkout uses PayPal Orders v2 with `intent=AUTHORIZE`: the buyer's funds are reserved, nothing is charged yet. It also records the device and IP that PayPal does not expose.
2. **Screen before capture.** The buyer is linked to existing accounts in a graph. If they share a **device** (or a card fingerprint, where the merchant has one) with a flagged ring, the order is **held for review**. Otherwise it is captured automatically. Holding costs the merchant nothing: no money has moved.
3. **Merchant decides.** Held orders appear on the dashboard with the reason. **Capture** charges the buyer; **Void** releases the hold. Voided money never left the buyer.
4. **Strong versus weak evidence.** Accounts sharing a device (or card fingerprint) are **core**. Accounts linked *only by address or IP* are **peripheral** and are never held or refunded, so families and offices are left alone.
5. **AI analyst.** For a flagged case, a tool-using model inspects accounts and writes a structured finding: evidence, caveats, recommended actions (hold accounts, refund payments).
6. **Approve to act.** Hold accounts and refund payments (real PayPal refunds) only after approval. Held buyers are blocked *before* capture on later orders.
7. **Disputes connected.** Verified PayPal dispute webhooks are stored and linked to the buyer, show whether they belong to a ring, and the merchant can send evidence to PayPal after approval.

## Does it work? (self-test)
The dashboard's **Run evaluation (100 trials each)** button (and `GET /api/eval?seeds=100`) generates synthetic rings and innocent look-alikes and replays them through the same hold rule the live checkout uses. The dashboard uses random draws that were not looked at while tuning the rule, and it also runs a **naive baseline**: "hold anyone who shares anything with a flagged account". Numbers below are from 100 trials per scenario on those unseen draws (the dashboard also shows 95% confidence intervals and a threshold sweep):

| Scenario | Follow-up buyers blocked (ours) | Naive baseline |
|---|---|---|
| Same device, buying within minutes | 100% | 100% |
| Same device + shared address, within minutes | 100% | 100% |
| Same device, spread over days | 71% (was 0% before we added the device-spread rule) | 100% |
| Chain: neighbours share a device or address | 57% | 100% |
| Rotating devices, shared address only | 0% (known blind spot; address alone is deliberately too weak to hold anyone) | 100% |
| **Average** | **82%** | 100% |

| Innocent look-alike | Groups with a wrongly held order (ours) | Naive baseline |
|---|---|---|
| Strangers: nothing shared | 0% | 0% |
| Office: shared IP only | 0% | 100% |
| Family: shared address, own devices, hours apart | 0% | 100% |
| Roommates: shared address and IP, same evening | 0% | 100% |
| Family tablet: one device, days apart (hard case) | 0% | 100% |
| **Average** | **0%** | **80%** |

The naive rule catches every ring but disrupts four of the five innocent groups. Our rule gives up some recall to disrupt none of them in these scenarios. The threshold sweep on the dashboard shows the trade-off: a hold threshold of 0.3 wrongly holds some innocent buyers, while 0.4 and above do not; we use 0.5.

(A bug in our first version of the chain scenario made it measure the wrong thing; it was found, fixed and re-run, which moved that row from 44% to about 56%.)

The evaluation exposed that a ring which slows down evaded the timing signal. We added one rule (a device used by 3+ accounts that ship to 3+ different addresses is held for review), and the table shows the before and after. **These are our own synthetic scenarios, not real traffic. The first buyer in a ring can never be blocked, because nothing links them yet.**

## Every decision can be explained and replayed
The hold decision is one pure, deterministic function of the evidence and a versioned policy. The live checkout, the self-test and the explanations all call that same function. For every screened order we store the exact evidence snapshot, the policy version and a SHA-256 hash of the snapshot. The dashboard's **Why was this decided?** panel then shows:
- **The evidence behind the decision**, factor by factor, with **where each signal came from and how far to trust it** (for example, the device ID is a browser value our checkout supplies and can be changed; the address comes from the PayPal order; the IP is seen by our server).
- **What would change the decision:** the same rule re-run with one piece of the buyer's evidence altered (a different device, address, IP, or a purchase three days later).
- **A decision replay:** the stored evidence is re-checked against its hash, re-run under the original policy, and compared with today's policy.

What this proves and what it does not: replay shows the decision is deterministic and that the stored evidence has not been altered since. It does not prove the decision was correct. A hash kept next to the data is tamper-evident for casual edits, not tamper-proof against someone who controls the database. Snapshots larger than 400 accounts are not kept, and the panel says so instead of faking a replay.

## Payment truth: verifiable, reconcilable, auditable
- **Verify with PayPal.** Every order has a **Verify** button. It reads PayPal's own record of that order (authorization and capture status, amount, capture ID) and compares it field by field with ours, showing a match or a mismatch. It is public and rate-limited so anyone reviewing the project can use it.
- **"No answer" is not "failed".** If a capture or void gets no response from PayPal (a timeout or a dropped connection), the money may or may not have moved. We never roll back or retry blindly: the order becomes `outcome_unknown`, capture and void are blocked, and the merchant **reconciles with PayPal**, which settles the order from PayPal's answer (captured, voided, or still authorized). When PayPal does answer "no", the order goes back to its previous state so it can be retried.
- **Refunds are all-or-nothing and safe to retry.** If PayPal does not confirm every refund, nothing is recorded as done and the case stays open. Retrying reuses the same idempotency keys, so no payment can be refunded twice. PayPal keeps request IDs for a limited time, so if in doubt, verify first.
- **Audit ledger.** Decisions, holds, captures, voids, unknown outcomes, reconciliations, approvals, dispute events, evidence sent and demo resets are written to an append-only, hash-chained log. Each event stores the hash of the one before it, so editing, removing or reordering an earlier event breaks the chain and the dashboard says where. Honest limit: this is tamper-evident for anyone who cannot rewrite the whole table; someone with full database access could recompute every hash, and cutting events off the end cannot be detected from inside the database, so the dashboard shows the head hash for you to record somewhere outside.
- **Evidence package.** `GET /api/cases/{id}/export` returns one JSON file: the case, accounts with hashed identifiers, orders, every decision with its replay and what-ifs, disputes, the ledger events for the case, and the ledger's verification result.

## AI red team: attacks we did not write
A self-test written by the same author as the detector is circular, so the dashboard also has an **AI red team**. A separate model (the same Groq model, prompted only as an adversary) is told what the detector can observe (device, address, IP, purchase times) and asked to invent six evasion strategies. Its output is validated against a fixed schema (bounded sizes, allowed modes only), each strategy is replayed against the live hold rule, and the dashboard reports what got through, with a short AI commentary on which extra signal might catch it. Live runs are public but rate-limited (90 seconds between runs, 40 per day) to protect the free model quota; in between, the most recent result stays on screen. The model never touches checkout, the database or PayPal. With no key, or if the call fails, an offline sample set (written by us, and labelled as such) is shown instead. In our offline sample, 3 of 6 strategies evade the detector (for example, rotating devices with no other link), and we show that rather than hide it.

## Architecture
```mermaid
flowchart LR
  B[Buyer] --> S[Demo checkout]
  S -->|Orders v2: authorize| P[(PayPal sandbox)]
  S --> D[(Postgres / SQLite)]
  D --> G[Graph engine: core vs peripheral + device-spread rule]
  G -->|clean| C[Capture]
  G -->|linked to a ring| H[Held for review]
  H --> M[Dashboard: merchant]
  M -->|Capture or Void| P
  P -->|verified webhooks: payments, disputes| W[RingHunter API]
  W --> D
  G --> A[AI analyst: Groq gpt-oss-120b with tools]
  A --> R[Guardrails: schema, allow-list, core-only]
  R --> M
  M -->|Payments v2 refund, Disputes evidence| P
```

## PayPal APIs used
- **Orders v2:** create (authorize or capture intent), get, authorize, capture.
- **Payments v2:** capture an authorization, void an authorization, refund a capture, with `PayPal-Request-Id` for idempotency.
- **Webhooks:** `verify-webhook-signature` on every event; payment and dispute events are stored.
- **Disputes v1:** get dispute, provide evidence, send message.

Money-moving calls (authorize, capture, void, refund, dispute evidence) use our own guarded REST client. The **PayPal Agent Toolkit** (v1.11.0, its framework-free core installed with `--no-deps`, without the LangChain and CrewAI extras) powers the AI analyst's read-only lookups of live disputes and orders, restricted to accounts in the case. Its tool list (create, get and pay orders; disputes list, get and accept; invoices; shipments; subscriptions) has no authorization void or capture and no dispute-evidence call, which is why those stay on REST. If the toolkit is not installed or a call fails, the same lookup uses REST, and the case file lists which tools were used and through which path.

## How the AI is used
- **Investigator agent:** a tool-calling loop over `openai/gpt-oss-120b` (Groq, OpenAI-compatible API). Tools return account details and transactions for accounts in the case only.
- **Red team:** a model invents evasion strategies from a schema; we replay them against the detector (see above).
- **Evidence drafting:** the model writes a short factual dispute note from the merchant's own records, and is told never to invent shipping, tracking or policy claims.
- **Safe by construction:** model output is untrusted.
  - It is validated against a schema.
  - Actions must come from an allow-list.
  - Account IDs must belong to the case.
  - Hold and refund are clamped to **core** accounts in code, whatever the model says.
  - Standard caveats ("heuristic, not proof of fraud") are always appended.
  - If the model fails, the app falls back to a single-shot answer, then to a deterministic offline analyst. It never blocks the merchant.
- The ring *detection* and the hold rule are deterministic graph logic. The AI explains and recommends; it does not decide who is held.
- **Nothing moves without approval.** State-changing endpoints require an admin token, are idempotent, and every action is logged. Account identifiers are sticky (first-seen device, IP and address are kept), so a returning buyer cannot shake ring links by switching browser.

## Testing instructions for judges
- **No login needed:** open `/dashboard#watch`, pick a scenario and press **Replay**. It walks an attack (or an innocent group) through the live hold rule purchase by purchase, in plain language. Then **Run evaluation (100 trials each)** for the measured results, and **Let an AI try to break it** for the red team.
- Open `/dashboard`. Reading is public; actions need the **admin token** given in the Devpost testing-instructions field (sandbox only).
- Click **Load demo data** for a ready-made ring, then **Investigate with AI analyst**.
- To make a live ring, buy at `/?device=ring1` with sandbox buyers (credentials in the Devpost field). The first buyer is charged; later buyers on the same device are held and appear under **Orders held for review**. Try **Void** and **Capture**.
- Approve an action. Refunds are real PayPal sandbox refunds. Try buying again as a held buyer: the order is declined before capture.

## Run locally
```bash
pip install -r requirements.txt
cp .env.example .env     # add PayPal sandbox keys; optionally GROQ_API_KEY; SCREENING=on for authorize-then-screen
uvicorn app.main:app --reload
# open http://localhost:8000/dashboard  and  /docs
python -m pytest -q
python -m app.llm_check  # checks your LLM key and model
python -m app.auth_check # sandbox check for authorize / void / capture
```
Without any keys, detection and the offline analyst still run. Set `ADMIN_TOKEN` to lock state-changing endpoints (leave it empty locally). `SCREENING=on` switches checkout to authorize-then-screen; the default is the plain capture flow.

## Tools and stack
Python, FastAPI, SQLAlchemy (SQLite locally, Postgres on Render), networkx, PayPal REST APIs, Groq (`openai/gpt-oss-120b`), AG Grid Community (dashboard tables), Render (hosting). Tests: pytest.

## Prior work disclosure
See [HACKATHON_CHANGES.md](HACKATHON_CHANGES.md) for exactly what existed before the hackathon and what was built during it.

`app/detection_core.py` (the graph scoring engine) comes from the author's earlier project, **Abuse-Ring Sentinel**, and is reused as is. Everything else was written for this hackathon: the PayPal integration, authorize-screen-capture checkout, webhooks, dispute handling, the device-spread rule, the AI investigator and its guardrails, the approval, refund, void and hold flows, the held-buyer block, the admin lock, the dashboard, the self-test and the tests.

## Security and payment-integrity notes
- **The server sets the price.** The browser cannot change the amount (`PRODUCT_PRICE`, default 25.00).
- **A returning order must match a checkout this store started.** Each checkout is recorded when created. On return we require the PayPal order to be `APPROVED` with the same amount and currency; anything else is declined before authorize or capture. A checkout is processed once: reloads, replayed links and double clicks report the outcome and change nothing. The device ID comes from the stored checkout, not from a URL parameter.
- **State changes are atomic and idempotent.** An order moves out of `pending_review` with a conditional update, so two simultaneous requests cannot both act. PayPal calls carry `PayPal-Request-Id`. If PayPal rejects a capture or void, the order goes back to its previous state so the merchant can retry.
- **The admin lock fails closed.** On Render (or with `REQUIRE_ADMIN=1`), state-changing endpoints return 503 unless `ADMIN_TOKEN` is set. Locally the lock is open for development.
- **Read-only views are public by design,** so judges can inspect the system. The data is sandbox-only with fake buyers; production would put these behind authentication.
- **The device ID is client-supplied** (a browser value; `?device=` in the demo). An attacker can change it, which is why production needs real device fingerprinting and why the red team reports what evades us.

## Deploy configuration
Set these environment variables (see `.env.example`). Secrets are never committed.

| Variable | Purpose |
|---|---|
| `PAYPAL_CLIENT_ID`, `PAYPAL_SECRET` | PayPal sandbox app credentials |
| `PAYPAL_WEBHOOK_ID` | webhook ID, so events are signature-verified |
| `BASE_URL` | public URL PayPal redirects buyers back to |
| `DATABASE_URL` | Postgres URL (SQLite locally) |
| `SCREENING` | `on` = authorize, screen, then capture or hold (the deployed demo uses `on`) |
| `ADMIN_TOKEN` | locks state-changing endpoints; required on Render |
| `PRODUCT_PRICE` | server-side price (default `25.00`) |
| `GROQ_API_KEY`, `GROQ_MODEL` | AI analyst and red team (without a key, the offline analyst runs) |
| `STRICT_ORDER_CHECK` | `on` (default) enforces the order checks above |

## Limitations (honest)
- Detection is heuristic scoring, not proof of fraud. A patient adversary who rotates devices and addresses can evade it (see the blind spot above).
- The first buyer in a ring is always charged; the system protects against the follow-up buyers.
- Live linking uses device, address, IP and purchase timing. PayPal gives merchants no card fingerprint or account-creation time, so those signals exist only in synthetic tests; we removed a fake signup-time signal rather than keep it. PayPal does not give merchants device fingerprints either, so the demo checkout captures device and IP itself. The device ID comes from the browser (or `?device=` in the demo), which an attacker can change. Production would need real device fingerprinting.
- Self-test results come from our own synthetic scenarios.
- Sandbox data is synthetic, and all sandbox buyers share PayPal's default shipping address. That is why address-only links are treated as weak evidence.
- Evidence notes only claim shipping when the merchant supplies real tracking.
- The public demo runs on a free tier and sleeps when idle.

## License
MIT
