import json
import secrets
from pathlib import Path
from contextlib import asynccontextmanager
import uuid
from fastapi import FastAPI, Request, HTTPException, Depends, Security
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import APIKeyHeader
from pydantic import BaseModel
from sqlalchemy import select
from . import paypal, engine as eng, investigator, disputes as dsp, llm
from .config import settings
from .db import init_db, SessionLocal, Account, Txn, Case, ActionLog, Blocked, Event, Dispute, now

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
    return {"ok": True, "paypal_sandbox": paypal.enabled(), "ai": llm.provider(), "admin_protected": bool(settings.admin_token)}


# ---------- storefront (demo checkout that captures the signals PayPal does not expose) ----------
STORE = """<!doctype html><meta name=viewport content="width=device-width,initial-scale=1"><title>Demo Store</title>
<body style="font-family:system-ui;max-width:420px;margin:40px auto"><h2>Demo Store (PayPal sandbox)</h2>
<p>Sandbox Widget — $25.00</p><button id=b style="padding:12px 20px;font-size:16px">Pay with PayPal</button>
<p id=m></p><script>
let d=new URLSearchParams(location.search).get('device')||localStorage.dev||(localStorage.dev='dev_'+crypto.randomUUID().slice(0,12));
b.onclick=async()=>{const r=await fetch('/checkout/create',{method:'POST',headers:{'Content-Type':'application/json'},
body:JSON.stringify({amount:'25.00',device_id:d})});const j=await r.json();
if(j.approve_url)location=j.approve_url;else m.textContent=JSON.stringify(j)}</script>"""


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
                            f"{settings.base_url}/")
    return o


@app.get("/checkout/return", response_class=HTMLResponse)
def checkout_return(token: str, request: Request, dev: str = "", s=Depends(db)):
    order = paypal.get_order(token)
    pre_pid = order.get("payer", {}).get("payer_id")
    if pre_pid and s.get(Blocked, pre_pid):
        return "<h3>Order declined.</h3>"  # never captured, so no money moves
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
        return "<h3>Order declined.</h3>"
    ip = (request.headers.get("x-forwarded-for") or request.client.host).split(",")[0].strip()
    s.merge(Account(id=pid, email=payer.get("email_address"), device_id=dev or None, ip=ip,
                    shipping_address=shipping))  # PayPal gives no signup time; first-seen time would be a fake signal
    s.merge(Txn(id=c["id"], account_id=pid, ts=now().replace(tzinfo=None).isoformat(timespec="seconds"),
                amount=float(c["amount"]["value"]), capture_id=c["id"]))
    s.commit()
    eng.detect(s)
    return "<h3>Thanks, payment received.</h3>"


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


@app.post("/api/detect", dependencies=[ADMIN])
def detect(s=Depends(db)):
    return [c.as_dict() for c in eng.detect(s)]


@app.get("/api/cases")
def cases(s=Depends(db)):
    return [c.as_dict() for c in s.scalars(select(Case).where(Case.status != "superseded").order_by(Case.score.desc()))]


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
    return c.as_dict()


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
    c.status = "actioned" if rec["action"] != "no_action" else "dismissed"
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
