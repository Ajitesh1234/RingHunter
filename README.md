# RingHunter

**An AI analyst that finds coordinated fraud and dispute rings in a merchant's PayPal activity, writes the case file, and acts on it only with the merchant's approval.**

Built for the PayPal AI Hackathon (PayPal sandbox only).

## Flow
```
Storefront (PayPal Orders v2) + verified webhooks
        -> accounts/transactions (SQLAlchemy; SQLite or Postgres)
        -> graph ring detection (networkx; per-edge evidence + timing + signup-batch signal)
        -> AI investigator (Claude tool use; schema-validated; offline fallback)
        -> merchant approves -> hold accounts / refund via PayPal Payments v2 (idempotent, audited)
```

## Design principles
- **Deterministic before generative.** Ring scoring is plain graph logic; the LLM explains and recommends, it never decides alone.
- **LLM output is untrusted.** Case files are schema-validated; actions come from an allow-list; account ids must belong to the cluster.
- **Nothing moves without approval.** `POST /api/cases/{id}/approve` is the only place state or money changes. Idempotent (DB key + PayPal-Request-Id) and audit-logged.
- **Low false positives on purpose.** A family sharing a card/address and an office sharing Wi-Fi are NOT flagged (see `tests/`).

## Run locally
```bash
pip install -r requirements.txt
cp .env.example .env            # optional: add PayPal sandbox + Anthropic keys
uvicorn app.main:app --reload
curl -X POST localhost:8000/api/demo/seed      # demo ring + family + office
open localhost:8000/docs                       # investigate and approve cases
pytest
```
Without keys, detection and the offline analyst still run. With sandbox keys, `/` is a live PayPal checkout; set the webhook URL to `<BASE_URL>/webhooks/paypal` and put its ID in `PAYPAL_WEBHOOK_ID`.

## Tools used
PayPal REST (Orders v2, Payments v2, Webhooks verify, Disputes read) · Claude (investigator agent with tool use) · FastAPI · networkx · SQLAlchemy · Render.

## Prior work disclosure
`app/detection_core.py` is the graph scoring engine from the author's earlier project (Abuse-Ring Sentinel), reused unchanged. Everything else (PayPal integration, webhooks, investigator agent, approval/action flow, storefront, API) was written for this hackathon.

## Limitations
Heuristic detection, not proof of fraud; PayPal does not expose device/card fingerprints, so the demo storefront captures device and IP itself; sandbox data is synthetic; a patient adversary can evade it.
