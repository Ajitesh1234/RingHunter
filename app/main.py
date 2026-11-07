import json
import time
import hashlib
from decimal import Decimal
from datetime import datetime, timezone
import secrets
from pathlib import Path
from contextlib import asynccontextmanager
import uuid
from fastapi import FastAPI, Request, HTTPException, Depends, Security
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import APIKeyHeader
from pydantic import BaseModel
from sqlalchemy import select, update
from . import paypal, engine as eng, investigator, disputes as dsp, llm, screening, evaluation, redteam, paypal_tools, ledger
from .config import settings
from .db import init_db, SessionLocal, Account, Txn, Case, ActionLog, Blocked, Event, Dispute, Order, Checkout, Decision, now

@asynccontextmanager
async def lifespan(_app):
    init_db()
    yield


app = FastAPI(title="RingHunter", version="0.1.0", lifespan=lifespan)


def db():
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


_admin_header = APIKeyHeader(name="X-Admin-Token", auto_error=False)


def require_admin(token: str | None = Security(_admin_header)):
    """Guards every endpoint that changes state or spends money. Empty ADMIN_TOKEN = open (local dev only)."""
    if not settings.admin_token:
        if settings.admin_required:
            raise HTTPException(503, "ADMIN_TOKEN is not configured on this server, so state-changing endpoints are disabled.")
        return  # local development only
    if not token or not secrets.compare_digest(token, settings.admin_token):
        raise HTTPException(401, "admin token required")


ADMIN = Depends(require_admin)


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard():
    return (Path(__file__).parent / "static" / "dashboard.html").read_text(encoding="utf-8")


@app.get("/api/health")
def health():
    return {"ok": True, "paypal_sandbox": paypal.enabled(), "ai": llm.provider(), "admin_protected": bool(settings.admin_token), "screening": settings.screening, "agent_toolkit": paypal_tools.toolkit_installed()}


# ---------- storefront (demo checkout that captures the signals PayPal does not expose) ----------
ICON = ("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'%3E%3Ccircle cx='32' cy='32' r='29' "
        "fill='%230b5fff'/%3E%3Ccircle cx='32' cy='32' r='11' fill='none' stroke='white' stroke-width='5'/%3E%3C/svg%3E")
BASE_CSS = """:root{--bg:#f6f7f9;--card:#fff;--tx:#14181f;--mu:#66707f;--bd:#dde1e7;--ac:#0b5fff;--ok:#188038;--bad:#d93025;--warn:#e8a200}
@media(prefers-color-scheme:dark){:root{--bg:#0f1318;--card:#171c23;--tx:#e8ecf2;--mu:#98a2b3;--bd:#2a323d;--ac:#6ea1ff;--ok:#4cc27a;--bad:#ff6b5e;--warn:#f2b83d}}
*{box-sizing:border-box}body{margin:0;font:16px/1.5 system-ui,sans-serif;background:var(--bg);color:var(--tx)}
.mu{color:var(--mu);font-size:14px}"""
STORE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Demo Store | RingHunter</title><link rel="icon" href="__ICON__"><style>__CSS__
nav{display:flex;flex-wrap:wrap;gap:16px;align-items:center;padding:14px 22px;border-bottom:1px solid var(--bd);background:var(--card);font-size:14px}
nav b{font-size:18px;margin-right:auto}nav a{color:var(--ac);text-decoration:none}nav a:hover{text-decoration:underline}
main{max-width:920px;margin:30px auto;padding:0 18px;display:grid;grid-template-columns:1fr 1fr;gap:20px}
@media(max-width:760px){main{grid-template-columns:1fr}}
.card{background:var(--card);border:1px solid var(--bd);border-radius:14px;padding:22px;box-shadow:0 1px 3px rgba(0,0,0,.06)}
.prod{display:flex;flex-direction:column;gap:12px}
.art{height:150px;border-radius:10px;background:linear-gradient(135deg,var(--ac),#7c4dff);display:flex;align-items:center;justify-content:center;font-size:58px;color:#fff}
h1{font-size:22px;margin:0}h2{font-size:18px;margin:0 0 6px}.price{font-size:28px;font-weight:700}
button{font:inherit;font-size:17px;padding:13px 18px;border:0;border-radius:10px;background:#ffc439;color:#111;font-weight:700;cursor:pointer}
button:hover{filter:brightness(.96)}button:disabled{opacity:.6;cursor:default}
.steps{list-style:none;margin:12px 0 0;padding:0;counter-reset:s}.steps li{counter-increment:s;position:relative;padding:0 0 14px 40px}
.steps li:before{content:counter(s);position:absolute;left:0;top:0;width:27px;height:27px;border-radius:50%;background:var(--ac);color:var(--bg);text-align:center;line-height:27px;font-size:14px;font-weight:700}
</style></head><body>
<nav><b>Demo Store</b><a href="/dashboard#watch">Watch an attack get stopped (no login)</a><a href="/dashboard">Merchant dashboard</a><a href="/docs">API</a><a href="https://github.com/Ajitesh1234/RingHunter">Code</a></nav>
<main>
<section class="card prod"><div class="art" aria-hidden="true">&#9881;</div><h1>Sandbox Widget</h1>
<div class="mu">A test product for the PayPal sandbox. Pay with a sandbox buyer account; no real money moves.</div>
<div class="price">$25.00</div><button id="b">Pay with PayPal</button><div id="m" class="mu"></div></section>
<section class="card"><h2>What happens at checkout</h2>
<ol class="steps"><li><b>You approve the payment.</b><br><span class="mu">PayPal reserves the money. Nothing is charged yet.</span></li>
<li><b>RingHunter checks the order.</b><br><span class="mu">It looks for links to a ring of accounts: shared devices, addresses and timing.</span></li>
<li><b>Clean orders are charged.</b><br><span class="mu">Linked orders are held for the merchant, and no money moves.</span></li></ol>
<p class="mu">RingHunter stops coordinated buyer rings at checkout, before money moves.</p></section>
</main>
<script>
let d=new URLSearchParams(location.search).get('device')||localStorage.dev||(localStorage.dev='dev_'+crypto.randomUUID().slice(0,12));
b.onclick=async()=>{b.disabled=true;b.textContent='Opening PayPal...';const r=await fetch('/checkout/create',{method:'POST',headers:{'Content-Type':'application/json'},
body:JSON.stringify({device_id:d})});const j=await r.json();
if(j.approve_url)location=j.approve_url;else{m.textContent=JSON.stringify(j);b.disabled=false;b.textContent='Pay with PayPal'}}</script></body></html>""".replace("__ICON__", ICON).replace("__CSS__", BASE_CSS)


def _result_page(kind, headline, detail=""):
    """Plain, readable result page for the buyer after PayPal returns them to the store."""
    color = {"ok": "var(--ok)", "hold": "var(--warn)", "no": "var(--bad)"}[kind]
    mark = {"ok": "&#10003;", "hold": "&#8987;", "no": "&#10005;"}[kind]
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>Demo Store</title><link rel="icon" href="{ICON}"><style>{BASE_CSS}'
            f'.box{{max-width:460px;margin:12vh auto;background:var(--card);border:1px solid var(--bd);border-radius:14px;padding:30px;text-align:center}}'
            f'.m{{width:54px;height:54px;border-radius:50%;margin:0 auto 14px;background:{color};color:#fff;font-size:28px;line-height:54px}}'
            f'a{{color:var(--ac)}}</style></head><body><div class="box"><div class="m">{mark}</div><h2>{headline}</h2>'
            f'<p class="mu">{detail}</p><p><a href="/">Back to the store</a> &middot; <a href="/dashboard">Merchant dashboard</a></p></div></body></html>')


@app.get("/", response_class=HTMLResponse)
def store():
    return STORE


class CreateIn(BaseModel):
    device_id: str = ""
    amount: str | None = None  # accepted for compatibility but ignored: the server decides the price


@app.post("/checkout/create")
def checkout_create(body: CreateIn, request: Request, s=Depends(db)):
    if not paypal.enabled():
        raise HTTPException(503, "PayPal sandbox credentials not configured")
    o = paypal.create_order(settings.product_price, settings.currency, f"{settings.base_url}/checkout/return",
                            f"{settings.base_url}/", intent="AUTHORIZE" if settings.screening else "CAPTURE")
    s.merge(Checkout(id=o["id"], device=(body.device_id[:80] or None), amount=settings.product_price,
                     currency=settings.currency, status="created", ts=now().replace(tzinfo=None).isoformat(timespec="seconds")))
    s.commit()
    return o


def _order_problem(order, ck):
    """Does the PayPal order match the checkout we started? Returns a reason string if not."""
    if not settings.strict_order_check:
        return None
    pu = (order.get("purchase_units") or [{}])[0]
    amt = pu.get("amount") or {}
    if order.get("status") != "APPROVED":
        return f"order status is {order.get('status')}, expected APPROVED"
    try:
        same = Decimal(str(amt.get("value"))) == Decimal(ck.amount)
    except Exception:
        same = False
    if not same or amt.get("currency_code") != ck.currency:
        return "amount or currency does not match the checkout that was started"
    return None


def _already_processed(s, token):
    o = s.get(Order, token)
    if o and o.status == "captured":
        return _result_page("ok", "Thanks, payment received.", "This order was already processed.")
    if o and o.status == "pending_review":
        return _result_page("hold", "Thanks. Your order is being reviewed. You have not been charged yet.", "This order was already processed.")
    return _result_page("no", "This order was already processed.", "Nothing further was done.")


@app.get("/checkout/return", response_class=HTMLResponse)
def checkout_return(token: str, request: Request, dev: str = "", s=Depends(db)):
    ck = s.get(Checkout, token)
    if not ck:
        return _result_page("no", "Order not recognised.", "This checkout was not started from this store. You have not been charged.")
    if ck.status != "created":
        return _already_processed(s, token)          # reload, replayed link or double click: report, change nothing
    order = paypal.get_order(token)
    problem = _order_problem(order, ck)
    pre_pid = order.get("payer", {}).get("payer_id")
    if problem or (pre_pid and s.get(Blocked, pre_pid)):
        ck.status = "rejected"
        s.commit()
        return _result_page("no", "Order declined.", "This order could not be completed. You have not been charged.")
    claimed = s.execute(update(Checkout).where(Checkout.id == token, Checkout.status == "created").values(status="used"))
    s.commit()
    if claimed.rowcount != 1:                        # another request got here first
        return _already_processed(s, token)
    dev = ck.device or ""                            # the device we recorded at checkout start, not a URL parameter
    if settings.screening:
        return _screened_checkout(s, request, token, dev)
    cap = paypal.capture_order(token)
    payer = cap.get("payer", {})
    pid = payer.get("payer_id")
    pu = cap["purchase_units"][0]
    c = pu["payments"]["captures"][0]
    addr = pu.get("shipping", {}).get("address", {})
    shipping = "|".join(str(addr.get(k, "")).lower().strip() for k in ("address_line_1", "postal_code", "country_code")) or None
    if s.get(Blocked, pid):
        paypal.refund_capture(c["id"], request_id=f"blocked-{c['id']}")   # safety net only: the check above runs before capture
        return _result_page("no", "Order declined.", "This order could not be completed. You have not been charged.")
    ip = (request.headers.get("x-forwarded-for") or request.client.host).split(",")[0].strip()
    _upsert_account(s, pid, payer.get("email_address"), dev, ip, shipping)
    s.merge(Txn(id=c["id"], account_id=pid, ts=now().replace(tzinfo=None).isoformat(timespec="seconds"),
                amount=float(c["amount"]["value"]), capture_id=c["id"]))
    s.commit()
    eng.detect(s)
    return _result_page("ok", "Thanks, payment received.", "Your order is confirmed.")


def _upsert_account(s, pid, email, dev, ip, shipping):
    """Identifiers are sticky: the first-seen device, IP and address are kept. A returning account therefore
    cannot shake its ring links just by switching browser or address before screening runs."""
    a = s.get(Account, pid)
    if a:
        a.email = a.email or email
        a.device_id = a.device_id or dev or None
        a.ip = a.ip or ip
        a.shipping_address = a.shipping_address or shipping
    else:
        s.add(Account(id=pid, email=email, device_id=dev or None, ip=ip, shipping_address=shipping))
        s.flush()


@app.get("/api/activity")
def activity(s=Depends(db)):
    """Recent events for the dashboard timeline: cases flagged, orders and their outcomes, merchant approvals."""
    def naive(x):
        d = x if isinstance(x, datetime) else datetime.fromisoformat(x)
        return d.replace(tzinfo=None) if d.tzinfo is None else d.astimezone(timezone.utc).replace(tzinfo=None)
    refunded = {x.id for x in s.scalars(select(Txn).where(Txn.refunded == 1))}
    ev = []
    for c in s.scalars(select(Case).where(Case.status != "superseded")):
        ev.append((naive(c.created_at), "case", f"Case {c.id} flagged: {c.decision.replace('FLAGGED_FOR_', '').replace('_', ' ').lower()}, score {c.score}"))
    for o in s.scalars(select(Order)):
        what = {"pending_review": "held for review", "voided": "voided, no money moved", "authorized": "authorized",
                "captured": "charged, then refunded" if o.capture_id in refunded else "charged"}.get(o.status, o.status)
        ev.append((naive(o.ts), "order", f"Order ${o.amount:.2f} {what}"))
    for a in s.scalars(select(ActionLog)):
        ev.append((naive(a.ts), "action", f"Merchant approved: {a.action.replace('_', ' ')} (case {a.case_id})"))
    ev.sort(key=lambda e: e[0], reverse=True)
    return [{"when": e[0].isoformat(timespec="minutes"), "kind": e[1], "text": e[2]} for e in ev[:15]]


@app.get("/api/eval")
def run_evaluation(seeds: int = 20):
    """Attacks our own screening with synthetic rings and innocent look-alikes. Read-only, in memory, no PayPal calls."""
    return evaluation.run_eval(seeds)


@app.get("/api/replay")
def replay(i: int | None = None):
    """No-login walkthrough: one synthetic scenario, purchase by purchase. Read-only, in memory."""
    if i is None:
        return {"scenarios": evaluation.scenario_names()}
    return evaluation.replay_story(i)


@app.get("/api/redteam")
def redteam_last():
    return redteam.last_or_sample()


@app.post("/api/redteam")
def redteam_run():
    """A model invents evasion strategies; we replay them against the live hold rule. Public, rate-limited, read-only."""
    try:
        return redteam.run_live()
    except redteam.Cooldown as c:
        raise HTTPException(429, f"The red team is cooling down. Try again in {c.wait}s.")


def _screened_checkout(s, request, token, dev):
    """Authorize (no money taken), screen the buyer against known rings, then capture or hold for review."""
    auth = paypal.authorize_order(token)
    payer = auth.get("payer", {})
    pid = payer.get("payer_id")
    pu = auth["purchase_units"][0]
    a = pu["payments"]["authorizations"][0]
    addr = pu.get("shipping", {}).get("address", {})
    shipping = "|".join(str(addr.get(k, "")).lower().strip() for k in ("address_line_1", "postal_code", "country_code")) or None
    ip = (request.headers.get("x-forwarded-for") or request.client.host).split(",")[0].strip()
    ts = now().replace(tzinfo=None).isoformat(timespec="seconds")
    amount = float(a["amount"]["value"])
    _upsert_account(s, pid, payer.get("email_address"), dev, ip, shipping)
    s.merge(Txn(id=f"auth:{a['id']}", account_id=pid, ts=ts, amount=amount, capture_id=None))
    s.merge(Order(id=token, account_id=pid, auth_id=a["id"], amount=amount, ts=ts, status="authorized"))
    s.commit()
    o = s.get(Order, token)
    inputs_before = eng.load_inputs(s)              # the exact evidence this decision is made on
    hold, case_id, reason = screening.screen(s, pid)
    screening.record_decision(s, token, pid, *inputs_before)
    if hold:
        o.status, o.reason, o.case_id = "pending_review", reason, case_id
        s.commit()
        ledger.append(s, "system", "order.held", token, {"account": pid, "case": case_id, "reason": reason})
        return _result_page("hold", "Thanks. Your order is being reviewed. You have not been charged yet.", "The seller will confirm shortly. Nothing has been taken from your account.")
    try:
        screening.capture_order(s, o)
    except paypal.PayPalUnknown:
        o.status, o.reason = "outcome_unknown", "Automatic capture was sent but PayPal did not answer. Reconcile with PayPal."
        s.commit()
        ledger.append(s, "system", "order.outcome_unknown", token, {"step": "auto-capture"})
        return _result_page("hold", "Thanks. We are confirming your order.", "You will not be charged twice.")
    except paypal.PayPalError as e:
        o.status, o.reason = "pending_review", f"Automatic capture failed ({str(e)[:90]}). Review it and capture manually."
        s.commit()
        ledger.append(s, "system", "order.capture_failed", token, {"error": str(e)[:200]})
        return _result_page("hold", "Thanks. Your order is being reviewed. You have not been charged yet.", "The seller will confirm shortly. Nothing has been taken from your account.")
    s.commit()
    ledger.append(s, "system", "order.captured", token, {"auto": True, "capture_id": o.capture_id})
    eng.detect(s)
    return _result_page("ok", "Thanks, payment received.", "Your order is confirmed.")


# ---------- orders held for review ----------
@app.get("/api/orders")
def list_orders(s=Depends(db)):
    rows = list(s.scalars(select(Order).order_by(Order.ts.desc())))
    refunded = {x.id for x in s.scalars(select(Txn).where(Txn.refunded == 1))}
    decided = {d.order_id for d in s.scalars(select(Decision))}
    return [{"id": o.id, "account_id": o.account_id, "amount": o.amount, "ts": o.ts, "status": o.status,
             "reason": o.reason, "case_id": o.case_id, "capture_id": o.capture_id,
             "refunded": o.capture_id in refunded, "decision_recorded": o.id in decided} for o in rows]


def _order(s, oid) -> Order:
    o = s.get(Order, oid)
    if not o:
        raise HTTPException(404, "order not found")
    return o


@app.get("/api/orders/{oid}/explain")
def order_explain(oid: str, s=Depends(db)):
    """Why was this order held or charged? Evidence with provenance, what-ifs, and a replay under the original policy."""
    out = screening.explain(s, oid)
    if out is None:
        raise HTTPException(404, "no stored decision for this order (it was screened before decisions were recorded)")
    return out


def _claim(s, oid, from_statuses, to_status):
    """Atomic state transition: only one request can move an order out of its current state."""
    res = s.execute(update(Order).where(Order.id == oid, Order.status.in_(from_statuses)).values(status=to_status))
    s.commit()
    return res.rowcount == 1


def _unknown(s, o, step, actor="merchant"):
    o.status, o.reason = "outcome_unknown", f"The {step} was sent but PayPal did not answer. Reconcile with PayPal before doing anything else."
    s.commit()
    ledger.append(s, actor, "order.outcome_unknown", o.id, {"step": step})
    return JSONResponse(status_code=202, content={"status": "outcome_unknown", "message": o.reason})


@app.post("/api/orders/{oid}/capture", dependencies=[ADMIN])
def order_capture(oid: str, s=Depends(db)):
    o = _order(s, oid)
    prev = o.status
    if prev == "captured":
        return {"idempotent_replay": True, "status": prev}
    if prev == "voided":
        raise HTTPException(409, "order was voided")
    if prev == "outcome_unknown":
        raise HTTPException(409, "The outcome of the last attempt is unknown. Reconcile with PayPal first.")
    if not _claim(s, oid, ("pending_review", "authorized"), "capturing"):
        raise HTTPException(409, "order is already being processed")
    o = s.get(Order, oid)
    s.refresh(o)
    try:
        screening.capture_order(s, o)
    except paypal.PayPalUnknown:
        return _unknown(s, o, "capture")             # it may have worked: never assume, never roll back blindly
    except paypal.PayPalError as e:
        o.status = prev                               # PayPal answered "no": nothing moved, so put it back and allow a retry
        s.commit()
        raise HTTPException(502, f"PayPal rejected the capture: {e}")
    s.commit()
    ledger.append(s, "merchant", "order.captured", oid, {"capture_id": o.capture_id})
    eng.detect(s)
    return {"status": o.status, "capture_id": o.capture_id}


@app.post("/api/orders/{oid}/void", dependencies=[ADMIN])
def order_void(oid: str, s=Depends(db)):
    o = _order(s, oid)
    prev = o.status
    if prev == "voided":
        return {"idempotent_replay": True, "status": prev}
    if prev == "captured":
        raise HTTPException(409, "order already captured; use refund instead")
    if prev == "outcome_unknown":
        raise HTTPException(409, "The outcome of the last attempt is unknown. Reconcile with PayPal first.")
    if not _claim(s, oid, ("pending_review", "authorized"), "voiding"):
        raise HTTPException(409, "order is already being processed")
    o = s.get(Order, oid)
    s.refresh(o)
    try:
        screening.void_order(s, o)
    except paypal.PayPalUnknown:
        return _unknown(s, o, "void")
    except paypal.PayPalError as e:
        o.status = prev
        s.commit()
        raise HTTPException(502, f"PayPal rejected the void: {e}")
    s.commit()
    ledger.append(s, "merchant", "order.voided", oid, {})
    return {"status": o.status}


@app.post("/api/orders/{oid}/reconcile", dependencies=[ADMIN])
def order_reconcile(oid: str, s=Depends(db)):
    """Settle an order from PayPal's own answer (used when a capture or void got no response)."""
    o = _order(s, oid)
    try:
        outcome, view = screening.reconcile(s, o)
    except paypal.PayPalError as e:
        raise HTTPException(502, f"Could not get PayPal's answer to reconcile: {e}")
    s.commit()
    ledger.append(s, "merchant", "order.reconciled", oid, {"outcome": outcome, "paypal": view})
    eng.detect(s)
    return {"outcome": outcome, "status": o.status, "paypal": view}


_verify_log = []


@app.get("/api/orders/{oid}/verify")
def order_verify(oid: str, s=Depends(db)):
    """Ask PayPal what it says about this order and compare with our record. Public (judges can use it), rate-limited."""
    now_t = time.time()
    _verify_log[:] = [x for x in _verify_log if x > now_t - 60]
    if len(_verify_log) >= 30:
        raise HTTPException(429, "Too many PayPal checks in the last minute. Try again shortly.")
    o = _order(s, oid)
    _verify_log.append(now_t)
    try:
        return screening.verify(s, o)
    except paypal.PayPalError as e:
        raise HTTPException(502, f"PayPal did not answer: {e}")


@app.get("/api/ledger")
def ledger_view(limit: int = 12, s=Depends(db)):
    return {"verification": ledger.verify(s), "events": ledger.recent(s, max(1, min(limit, 50)))}


def _h8(v):
    return hashlib.sha256(str(v).encode()).hexdigest()[:8] if v else None


@app.get("/api/cases/{cid}/export")
def case_export(cid: int, s=Depends(db)):
    """A self-contained evidence package for one case. Identifiers are hashed (first 8 hex of SHA-256)."""
    c = _case(s, cid)
    entry = json.loads(c.entry_json)
    members = entry["cluster_members"]
    accts = [{"account_id": a.id, "device_hash": _h8(a.device_id), "ip_hash": _h8(a.ip), "address_hash": _h8(a.shipping_address)}
             for a in s.scalars(select(Account).where(Account.id.in_(members)))]
    orders = list(s.scalars(select(Order).where(Order.account_id.in_(members))))
    decisions = [d for d in (screening.explain(s, o.id) for o in orders) if d]
    refs = {o.id for o in orders} | {f"case:{cid}"}
    events = sorted((e for e in ledger.recent(s, 500) if e["ref"] in refs), key=lambda e: e["seq"])
    body = {"generated_at": now().replace(tzinfo=None).isoformat(timespec="seconds"), "case": _out(s, c), "accounts": accts,
            "orders": [{"id": o.id, "account_id": o.account_id, "amount": o.amount, "status": o.status, "reason": o.reason, "capture_id": o.capture_id} for o in orders],
            "decisions": decisions, "disputes": [dsp.as_dict(s, d) for d in s.scalars(select(Dispute).where(Dispute.account_id.in_(members)))],
            "ledger_events": events, "ledger_verification": ledger.verify(s),
            "notes": "Device, IP and address values are replaced by short hashes. Replay and what-ifs are in each decision."}
    return JSONResponse(body, headers={"Content-Disposition": f'attachment; filename="ringhunter-case-{cid}.json"'})


# ---------- verified PayPal webhooks ----------
@app.post("/webhooks/paypal")
async def webhook(request: Request, s=Depends(db)):
    event = await request.json()
    if not paypal.verify_webhook(dict(request.headers), event):
        raise HTTPException(400, "signature verification failed")
    s.merge(Event(id=event["id"], type=event["event_type"], payload_json=json.dumps(event)))
    if event["event_type"].startswith("CUSTOMER.DISPUTE"):
        try:
            dsp.upsert_from_resource(s, event.get("resource", {}))
        except Exception:  # never fail a verified webhook because of parsing
            s.rollback()
            s.merge(Event(id=event["id"], type=event["event_type"], payload_json=json.dumps(event)))
    s.commit()
    if event["event_type"].startswith("CUSTOMER.DISPUTE"):
        try:
            ledger.append(s, "paypal-webhook", "dispute.event", (event.get("resource") or {}).get("dispute_id"), {"type": event["event_type"]})
        except Exception:
            s.rollback()
    return {"received": True}


# ---------- cases ----------
@app.post("/api/demo/seed", dependencies=[ADMIN])
def demo_seed(s=Depends(db)):
    from .seed import seed
    seed(s)
    cases = eng.detect(s)
    return {"flagged_cases": [c.id for c in cases]}


@app.post("/api/demo/reset", dependencies=[ADMIN])
def demo_reset(s=Depends(db)):
    """Clears accounts, transactions, cases, actions and holds so a demo can start clean. Disputes are kept."""
    for m in (ActionLog, Blocked, Case, Txn, Account, Order):
        s.query(m).delete()
    s.commit()
    ledger.append(s, "merchant", "demo.reset", None, {})   # the ledger itself is never wiped
    return {"reset": True}


@app.post("/api/detect", dependencies=[ADMIN])
def detect(s=Depends(db)):
    return [_out(s, c) for c in eng.detect(s)]


@app.get("/api/cases")
def cases(s=Depends(db)):
    return [_out(s, c) for c in s.scalars(select(Case).where(Case.status != "superseded").order_by(Case.score.desc()))]


def _out(s, c):
    """Case plus the recommendation indexes already approved, so the dashboard can show per-action state."""
    d = c.as_dict()
    d["done"] = [int(a.key.split(":")[1]) for a in s.scalars(select(ActionLog).where(ActionLog.case_id == c.id))]
    return d


def _case(s, cid) -> Case:
    c = s.get(Case, cid)
    if not c:
        raise HTTPException(404, "case not found")
    return c


@app.post("/api/cases/{cid}/investigate", dependencies=[ADMIN])
def investigate(cid: int, s=Depends(db)):
    c = _case(s, cid)
    if c.status in ("actioned", "dismissed"):
        raise HTTPException(409, "case already closed; its case file is locked")
    cf, model = investigator.investigate(s, json.loads(c.entry_json))
    c.casefile_json = json.dumps({**cf.model_dump(), "analyst": model})
    c.status = "investigated"
    s.commit()
    return _out(s, c)


class ApproveIn(BaseModel):
    recommendation_index: int


@app.post("/api/cases/{cid}/approve", dependencies=[ADMIN])
def approve(cid: int, body: ApproveIn, s=Depends(db)):
    """The ONLY place money/state moves, and only after explicit merchant approval. Idempotent."""
    c = _case(s, cid)
    if not c.casefile_json:
        raise HTTPException(409, "investigate first")
    cf = json.loads(c.casefile_json)
    try:
        rec = cf["recommendations"][body.recommendation_index]
    except IndexError:
        raise HTTPException(404, "no such recommendation")
    key = f"{cid}:{body.recommendation_index}"
    done = s.scalar(select(ActionLog).where(ActionLog.key == key))
    if done:
        return {"idempotent_replay": True, "result": json.loads(done.result_json)}
    members = set(json.loads(c.entry_json)["cluster_members"])
    ids = [a for a in rec["account_ids"] if a in members]
    result = {"action": rec["action"], "accounts": ids, "details": []}
    if rec["action"] == "hold_accounts":
        for a in ids:
            s.merge(Blocked(account_id=a, case_id=cid))
    elif rec["action"] == "refund_transactions":
        try:
            for t in list(s.scalars(select(Txn).where(Txn.account_id.in_(ids)))):
                if t.refunded or not t.capture_id:
                    continue
                if paypal.enabled() and not t.capture_id.startswith("CAP_"):
                    r = paypal.refund_capture(t.capture_id, request_id=f"rh-{cid}-{t.id}")
                    result["details"].append({"txn": t.id, "paypal_refund_id": r.get("id")})
                else:
                    result["details"].append({"txn": t.id, "simulated": True})
                t.refunded = 1
        except paypal.PayPalError as e:
            s.rollback()
            raise HTTPException(502, f"PayPal did not confirm every refund ({str(e)[:160]}). Nothing was recorded as done. "
                                     "Approving again is safe: refunds carry idempotency keys, so none can be doubled.")
    s.add(ActionLog(key=key, case_id=cid, action=rec["action"], result_json=json.dumps(result)))
    if rec["action"] in ("hold_accounts", "refund_transactions"):
        c.status = "actioned"
    elif rec["action"] == "no_action":
        c.status = "dismissed"  # request_more_info only logs a note, so the case stays open
    s.commit()
    ledger.append(s, "merchant", "case.approved", f"case:{cid}",
                  {"action": rec["action"], "accounts": ids, "refunds": [d["paypal_refund_id"] for d in result["details"] if d.get("paypal_refund_id")]})
    return result


# ---------- disputes ----------
@app.get("/api/disputes")
def list_disputes(s=Depends(db)):
    return [dsp.as_dict(s, d) for d in s.scalars(select(Dispute))]


def _dispute(s, did) -> Dispute:
    d = s.get(Dispute, did)
    if not d:
        raise HTTPException(404, "dispute not found")
    return d


@app.post("/api/disputes/{did}/refresh", dependencies=[ADMIN])
def dispute_refresh(did: str, s=Depends(db)):
    """Pull the latest dispute details from PayPal and re-link it to the buyer."""
    d = dsp.upsert_from_resource(s, paypal.get_dispute(did))
    if not d:
        raise HTTPException(404, "PayPal returned no dispute")
    s.commit()
    return {**dsp.as_dict(s, d), "paypal": json.loads(d.raw_json)}


@app.get("/api/disputes/{did}/draft")
def dispute_draft(did: str, carrier: str | None = None, tracking_number: str | None = None, s=Depends(db)):
    return dsp.draft_evidence(s, _dispute(s, did), carrier, tracking_number)


class EvidenceIn(BaseModel):
    carrier: str | None = None
    tracking_number: str | None = None
    notes: str | None = None  # if omitted, the drafted note is used
    method: str = "evidence"  # "evidence" or "message"


@app.post("/api/disputes/{did}/evidence", dependencies=[ADMIN])
def dispute_evidence(did: str, body: EvidenceIn, s=Depends(db)):
    """Sends evidence to PayPal. Only runs on explicit merchant approval; idempotent."""
    d = _dispute(s, did)
    if d.evidence_submitted:
        return {"idempotent_replay": True}
    notes = body.notes or dsp.draft_evidence(s, d, body.carrier, body.tracking_number)["notes"]
    try:
        if body.method == "message":
            r = paypal.send_message(did, notes)
        else:
            r = paypal.provide_evidence(did, dsp.evidence_body(notes, body.carrier, body.tracking_number))
    except paypal.PayPalError as e:
        raise HTTPException(502, f"PayPal rejected the request: {e}")
    d.evidence_submitted = 1
    s.commit()
    ledger.append(s, "merchant", "dispute.evidence_sent", did, {"method": body.method, "tracking_attached": bool(body.tracking_number)})
    return {"submitted": True, "method": body.method, "notes": notes, "paypal": r}
