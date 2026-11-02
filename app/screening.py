"""Pre-capture screening: authorize first, then decide. Only buyers with a STRONG link (shared device or card)
to a flagged ring are held; address/IP-only links are weak evidence and are captured normally."""
import json
from sqlalchemy import select
from . import engine as eng, paypal
from .db import Txn, Order, Account
from .investigator import split_members


def spread_rule(shipping_addresses) -> bool:
    """One device used by 3+ accounts that ship to 3+ different addresses. Catches slow rings that dodge the timing signal,
    while a household sharing one device and one address is left alone."""
    addrs = {a for a in shipping_addresses if a}
    return len(shipping_addresses) >= 3 and len(addrs) >= 3


def screen(s, account_id):
    """Returns (hold, case_id, reason)."""
    for c in eng.detect(s):
        entry = json.loads(c.entry_json)
        if account_id in entry["cluster_members"]:
            core, _ = split_members(entry)
            if account_id in core:
                return True, c.id, f"Shares a device or card with other accounts in case {c.id} (score {c.score})."
    me = s.get(Account, account_id)
    if me and me.device_id:
        group = list(s.scalars(select(Account).where(Account.device_id == me.device_id)))
        if spread_rule([g.shipping_address for g in group]):
            n_addr = len({g.shipping_address for g in group if g.shipping_address})
            return True, None, f"One device is used by {len(group)} accounts shipping to {n_addr} different addresses."
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
