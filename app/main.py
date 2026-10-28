import json
from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy import select
from . import paypal, engine as eng, investigator
from .config import settings
from .db import init_db, SessionLocal, Account, Txn, Case, ActionLog, Blocked, Event, now

app = FastAPI(title="RingHunter", version="0.1.0")


@app.on_event("startup")
def _startup():
    init_db()


def db():
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


@app.get("/api/health")
def health():
    return {"ok": True, "paypal_sandbox": paypal.enabled(), "ai": "claude" if settings.anthropic_key else "offline"}


# ---------- storefront (captures the signals PayPal does not expose) ----------
STORE = """<!doctype html><meta name=viewport content="width=device-width,initial-scale=1"><title>Demo Store</title>
<body style="font-family:system-ui;max-width:420px;margin:40px auto"><h2>Demo Store (PayPal sandbox)</h2>
<p>Sandbox Widget — $25.00</p><button id=b style="padding:12px 20px;font-size:16px">Pay with PayPal</button>
<p id=m></p><script>
let d=localStorage.dev||(localStorage.dev='dev_'+crypto.randomUUID().slice(0,12));
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
    return paypal.create_order(body.amount, "USD", f"{settings.base_url}/checkout/return?dev={body.device_id}",
                               f"{settings.base_url}/")


@app.get("/checkout/return", response_class=HTMLResponse)
def checkout_return(token: str, request: Request, dev: str = "", s=Depends(db)):
    cap = paypal.capture_order(token)
    payer = cap.get("payer", {})
    pid = payer.get("payer_id")
    pu = cap["purchase_units"][0]
    c = pu["payments"]["captures"][0]
    addr = pu.get("shipping", {}).get("address", {})
    shipping = "|".join(str(addr.get(k, "")).lower().strip() for k in ("address_line_1", "postal_code", "country_code")) or None
    if s.get(Blocked, pid):
        paypal.refund_capture(c["id"], request_id=f"blocked-{c['id']}")
        return "<h3>Order declined.</h3>"
    ip = (request.headers.get("x-forwarded-for") or request.client.host).split(",")[0].strip()
    s.merge(Account(id=pid, email=payer.get("email_address"), device_id=dev or None, ip=ip,
                    shipping_address=shipping, created_at=now().replace(tzinfo=None).isoformat(timespec="seconds")))
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
    s.commit()
    return {"received": True}


# ---------- cases ----------
@app.post("/api/demo/seed")
def demo_seed(s=Depends(db)):
    from .seed import seed
    seed(s)
    return {"flagged_cases": [c.id for c in eng.detect(s)]}


@app.post("/api/detect")
def detect(s=Depends(db)):
    return [c.as_dict() for c in eng.detect(s)]


@app.get("/api/cases")
def cases(s=Depends(db)):
    return [c.as_dict() for c in s.scalars(select(Case).order_by(Case.score.desc()))]


def _case(s, cid) -> Case:
    c = s.get(Case, cid)
    if not c:
        raise HTTPException(404, "case not found")
    return c


@app.post("/api/cases/{cid}/investigate")
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


@app.post("/api/cases/{cid}/approve")
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
