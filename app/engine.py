"""Bridges the database to the Sentinel graph engine (detection_core) and turns clusters into cases."""
import hashlib
import json
from collections import defaultdict
from sqlalchemy import select
from . import detection_core as core
from .db import Account, Txn, Case


def load_inputs(s):
    accounts, txns = {}, defaultdict(list)
    for a in s.scalars(select(Account)):
        # IMPORTANT: a missing attribute must never be a shared value, or all
        # accounts with None would be linked together. Use a per-account placeholder.
        accounts[a.id] = {
            "device_id": a.device_id or f"none_dev_{a.id}",
            "card_fp": a.card_fp or f"none_card_{a.id}",
            "ip": a.ip or f"none_ip_{a.id}",
            "shipping_address": a.shipping_address or f"none_addr_{a.id}",
            **({"account_created_at": a.created_at} if a.created_at else {}),
        }
    for t in s.scalars(select(Txn)):
        txns[t.account_id].append({"timestamp": t.ts, "amount": t.amount})
    return accounts, dict(txns)


def case_key(members):
    return hashlib.sha1("|".join(sorted(members)).encode()).hexdigest()[:16]


def detect(s) -> list[Case]:
    accounts, txns = load_inputs(s)
    if not accounts:
        return []
    trail, _ = core.run_detection(accounts, txns)
    out, current = [], {}
    for entry in trail:
        if entry["decision"] == "NOT_FLAGGED" or entry["cluster_size"] < 2:
            continue
        key = case_key(entry["cluster_members"])
        current[key] = set(entry["cluster_members"])
        case = s.scalar(select(Case).where(Case.key == key))
        if case:  # re-run: refresh evidence, keep status and casefile
            case.score, case.decision, case.entry_json = entry["suspicion_score"], entry["decision"], json.dumps(entry)
        else:
            case = Case(key=key, score=entry["suspicion_score"], decision=entry["decision"], entry_json=json.dumps(entry))
            s.add(case)
        out.append(case)
    # A ring that grows must not leave its smaller earlier version open as a duplicate case.
    for old in s.scalars(select(Case).where(Case.status.in_(("open", "investigated")))):
        om = set(json.loads(old.entry_json)["cluster_members"])
        if old.key not in current and any(om < nm for nm in current.values()):
            old.status = "superseded"
    s.commit()
    return out


def exposure(s, members):
    rows = list(s.scalars(select(Txn).where(Txn.account_id.in_(members))))
    return {"transactions": len(rows), "total_amount": round(sum(r.amount for r in rows), 2),
            "refundable": [r.id for r in rows if r.capture_id and not r.refunded]}
