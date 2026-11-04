import json
from datetime import datetime, timezone
import secrets
from pathlib import Path
from contextlib import asynccontextmanager
import uuid
from fastapi import FastAPI, Request, HTTPException, Depends, Security
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import APIKeyHeader
from pydantic import BaseModel
from sqlalchemy import select
from . import paypal, engine as eng, investigator, disputes as dsp, llm, screening, evaluation, redteam, paypal_tools
from .config import settings
from .db import init_db, SessionLocal, Account, Txn, Case, ActionLog, Blocked, Event, Dispute, Order, now

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
        return
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
body:JSON.stringify({amount:'25.00',device_id:d})});const j=await r.json();
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
    amount: str = "25.00"
    device_id: str


@app.post("/checkout/create")
def checkout_create(body: CreateIn, request: Request):
    if not paypal.enabled():
        raise HTTPException(503, "PayPal sandbox credentials not configured")
    o = paypal.create_order(body.amount, "USD", f"{settings.base_url}/checkout/return?dev={body.device_id}",
                            f"{settings.base_url}/", intent="AUTHORIZE" if settings.screening else "CAPTURE")
    return o


@app.get("/checkout/return", response_class=HTMLResponse)
def checkout_return(token: str, request: Request, dev: str = "", s=Depends(db)):
    order = paypal.get_order(token)
    pre_pid = order.get("payer", {}).get("payer_id")
    if pre_pid and s.get(Blocked, pre_pid):
        return _result_page("no", "Order declined.", "This order could not be completed. You have not been charged.")
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
        # NOTE: capture already happened above; auto-refund keeps the merchant whole.
        paypal.refund_capture(c["id"], request_id=f"blocked-{c['id']}")
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
    hold, case_id, reason = screening.screen(s, pid)
    if hold:
        o.status, o.reason, o.case_id = "pending_review", reason, case_id
        s.commit()
        return _result_page("hold", "Thanks. Your order is being reviewed. You have not been charged yet.", "The seller will confirm shortly. Nothing has been taken from your account.")
    screening.capture_order(s, o)
    s.commit()
    eng.detect(s)
    return _result_page("ok", "Thanks, payment received.", "Your order is confirmed.")


# ---------- orders held for review ----------
@app.get("/api/orders")
def list_orders(s=Depends(db)):
    rows = list(s.scalars(select(Order).order_by(Order.ts.desc())))
    refunded = {x.id for x in s.scalars(select(Txn).where(Txn.refunded == 1))}
    return [{"id": o.id, "account_id": o.account_id, "amount": o.amount, "ts": o.ts, "status": o.status,
             "reason": o.reason, "case_id": o.case_id, "capture_id": o.capture_id,
             "refunded": o.capture_id in refunded} for o in rows]


def _order(s, oid) -> Order:
    o = s.get(Order, oid)
    if not o:
        raise HTTPException(404, "order not found")
    return o


@app.post("/api/orders/{oid}/capture", dependencies=[ADMIN])
def order_capture(oid: str, s=Depends(db)):
    o = _order(s, oid)
    if o.status == "captured":
        return {"idempotent_replay": True, "status": o.status}
    if o.status == "voided":
        raise HTTPException(409, "order was voided")
    try:
        screening.capture_order(s, o)
    except paypal.PayPalError as e:
        raise HTTPException(502, f"PayPal rejected the capture: {e}")
    s.commit()
    eng.detect(s)
    return {"status": o.status, "capture_id": o.capture_id}


@app.post("/api/orders/{oid}/void", dependencies=[ADMIN])
def order_void(oid: str, s=Depends(db)):
    o = _order(s, oid)
    if o.status == "voided":
        return {"idempotent_replay": True, "status": o.status}
    if o.status == "captured":
        raise HTTPException(409, "order already captured; use refund instead")
    try:
        screening.void_order(s, o)
    except paypal.PayPalError as e:
        raise HTTPException(502, f"PayPal rejected the void: {e}")
    s.commit()
    return {"status": o.status}


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
        for t in s.scalars(select(Txn).where(Txn.account_id.in_(ids))):
            if t.refunded or not t.capture_id:
                continue
            if paypal.enabled() and not t.capture_id.startswith("CAP_"):
                r = paypal.refund_capture(t.capture_id, request_id=f"rh-{cid}-{t.id}")
                result["details"].append({"txn": t.id, "paypal_refund_id": r.get("id")})
            else:
                result["details"].append({"txn": t.id, "simulated": True})
            t.refunded = 1
    s.add(ActionLog(key=key, case_id=cid, action=rec["action"], result_json=json.dumps(result)))
    if rec["action"] in ("hold_accounts", "refund_transactions"):
        c.status = "actioned"
    elif rec["action"] == "no_action":
        c.status = "dismissed"  # request_more_info only logs a note, so the case stays open
    s.commit()
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
    return {"submitted": True, "method": body.method, "notes": notes, "paypal": r}
