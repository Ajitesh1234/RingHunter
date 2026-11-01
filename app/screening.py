"""Pre-capture screening: authorize first, then decide. Only buyers with a STRONG link (shared device or card)
to a flagged ring are held; address/IP-only links are weak evidence and are captured normally."""
import json
from . import engine as eng, paypal
from .db import Txn, Order
from .investigator import split_members


def screen(s, account_id):
    """Returns (hold, case_id, reason)."""
    for c in eng.detect(s):
        entry = json.loads(c.entry_json)
        if account_id in entry["cluster_members"]:
            core, _ = split_members(entry)
            if account_id in core:
                return True, c.id, f"Shares a device or card with other accounts in case {c.id} (score {c.score})."
    return False, None, None


def capture_order(s, o: Order):
    r = paypal.capture_authorization(o.auth_id)
    cap_id = r["id"]
    pending = s.get(Txn, f"auth:{o.auth_id}")
    ts = pending.ts if pending else o.ts
    if pending:
        s.delete(pending)
        s.flush()
    s.add(Txn(id=cap_id, account_id=o.account_id, ts=ts, amount=o.amount, capture_id=cap_id))  # id = capture id, so disputes link
    o.capture_id, o.status = cap_id, "captured"
    return cap_id


def void_order(s, o: Order):
    paypal.void_authorization(o.auth_id)
    o.status = "voided"  # the Txn stays (capture_id empty) so the attempt still shows in the ring evidence
