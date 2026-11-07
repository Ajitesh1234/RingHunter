"""Pre-capture screening: authorize first, then decide. Only buyers with a STRONG link (shared device or card)
to a flagged ring are held; address/IP-only links are weak evidence and are captured normally."""
import copy
import hashlib
import json
from datetime import datetime, timedelta, timezone
from sqlalchemy import select
from . import detection_core as core, engine as eng, ledger, paypal, policy as policy_mod
from .db import Txn, Order, Account, Decision
from .investigator import split_members


def spread_rule(shipping_addresses, min_accounts=3, min_addresses=3) -> bool:
    """One device used by several accounts that ship to several different addresses. Catches slow rings that dodge the
    timing signal, while a household sharing one device and one address is left alone."""
    addrs = {a for a in shipping_addresses if a}
    return len(shipping_addresses) >= min_accounts and len(addrs) >= min_addresses


def decide(accounts, txns, account_id, policy=None):
    """THE hold decision, as a pure function of the evidence: same inputs and policy always give the same answer.
    The live checkout, the replay, the counterfactuals and the self-test all call this one function."""
    pol = policy or policy_mod.current()
    strong = set(pol["strong_attrs"])
    trail, _ = core.run_detection(accounts, txns)
    best = None
    for e in trail:
        if e["cluster_size"] < 2 or account_id not in e["cluster_members"]:
            continue
        best = max(best or 0.0, e["suspicion_score"])
        if e["suspicion_score"] < pol["min_score"]:
            continue
        core_ids = {m for ed in e.get("edges", []) if strong & set(ed["shared_attributes"]) for m in ed["pair"]}
        if account_id in core_ids:
            mine = [ed for ed in e["edges"] if account_id in ed["pair"] and strong & set(ed["shared_attributes"])]
            attrs = sorted({x for ed in mine for x in ed["shared_attributes"] if x in strong})
            partners = {m for ed in mine for m in ed["pair"] if m != account_id}
            return {"hold": True, "kind": "cluster", "score": e["suspicion_score"], "cluster": e["cluster_members"],
                    "core": sorted(core_ids), "edges": e["edges"], "breakdown": e.get("score_breakdown") or [],
                    "reason": f"shares {' and '.join(x.replace('_id', '').replace('_fp', '') for x in attrs)} with {len(partners)} "
                              f"earlier buyer(s) in a flagged ring"}
    me = accounts.get(account_id)
    if me and not str(me["device_id"]).startswith("none_dev_"):
        group = [a for a in accounts.values() if a["device_id"] == me["device_id"]]
        addrs = [a["shipping_address"] for a in group if not str(a["shipping_address"]).startswith("none_addr_")]
        if spread_rule(addrs if len(group) == len(addrs) else addrs + [None] * (len(group) - len(addrs)),
                       pol["spread_min_accounts"], pol["spread_min_addresses"]):
            return {"hold": True, "kind": "device_spread", "score": None, "group": len(group), "addresses": len(set(addrs)),
                    "breakdown": [], "reason": f"one device used by {len(group)} accounts shipping to {len(set(addrs))} different addresses"}
    return {"hold": False, "kind": None, "score": best, "breakdown": [], "reason": None}


def screen(s, account_id):
    """Returns (hold, case_id, reason). Also keeps the cases table up to date."""
    cases = eng.detect(s)
    accounts, txns = eng.load_inputs(s)
    d = decide(accounts, txns, account_id)
    if d["hold"] and d["kind"] == "cluster":
        cid = next((c.id for c in cases if json.loads(c.entry_json)["cluster_members"] == d["cluster"]), None)
        return True, cid, f"Shares a device or card with other accounts in case {cid} (score {d['score']})."
    if d["hold"]:
        return True, None, f"One device is used by {d['group']} accounts shipping to {d['addresses']} different addresses."
    return False, None, None


def _canon(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


MAX_SNAPSHOT_ACCOUNTS = 400


def record_decision(s, order_id, account_id, accounts, txns):
    """Keep the exact evidence and policy behind this decision, with a hash, so it can be replayed later."""
    pol = policy_mod.current()
    result = decide(accounts, txns, account_id, pol)
    blob = _canon({"accounts": accounts, "txns": txns}) if len(accounts) <= MAX_SNAPSHOT_ACCOUNTS else None
    s.add(Decision(order_id=order_id, account_id=account_id, ts=datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds"),
                   policy_json=_canon(pol), inputs_json=blob, inputs_sha256=hashlib.sha256(blob.encode()).hexdigest() if blob else None,
                   result_json=_canon(result)))
    s.commit()
    ledger.append(s, "system", "decision", order_id, {"hold": result["hold"], "kind": result["kind"], "score": result.get("score"),
                                                      "policy": pol["version"], "evidence_sha256": (hashlib.sha256(blob.encode()).hexdigest() if blob else None)})
    return result


def _variant(accounts, txns, account_id, change):
    a, t = copy.deepcopy(accounts), copy.deepcopy(txns)
    if change in ("device", "all"):
        a[account_id]["device_id"] = f"cf_dev_{account_id}"
    if change in ("address", "all"):
        a[account_id]["shipping_address"] = f"cf_addr_{account_id}"
    if change in ("ip", "all"):
        a[account_id]["ip"] = f"cf_ip_{account_id}"
    if change == "timing":
        for tx in t.get(account_id, []):
            tx["timestamp"] = (datetime.fromisoformat(tx["timestamp"]) + timedelta(days=3)).isoformat(timespec="seconds")
    return a, t


COUNTERFACTUALS = [("device", "If this buyer had used a different device"), ("address", "If this buyer had shipped to a different address"),
                   ("ip", "If this buyer had come from a different IP"), ("timing", "If this buyer had purchased 3 days later"),
                   ("all", "If this buyer shared nothing with anyone")]


def counterfactuals(accounts, txns, account_id, policy, original):
    """Deterministic what-ifs: change one piece of this buyer's evidence, re-run the same decision function, report the result."""
    out = []
    for key, text in COUNTERFACTUALS:
        a, t = _variant(accounts, txns, account_id, key)
        d = decide(a, t, account_id, policy)
        out.append({"change": text, "hold": d["hold"], "score": d.get("score"), "flips": d["hold"] != original["hold"]})
    return out


def _same(x, y):
    return (x["hold"], x["kind"], x.get("score"), x.get("core"), x.get("cluster")) == (y["hold"], y["kind"], y.get("score"), y.get("core"), y.get("cluster"))


def explain(s, order_id):
    """The full story of one decision: the evidence with its provenance, what-ifs, and a replay under the original policy."""
    row = s.scalars(select(Decision).where(Decision.order_id == order_id).order_by(Decision.id.desc())).first()
    if not row:
        return None
    pol, orig = json.loads(row.policy_json), json.loads(row.result_json)
    out = {"order_id": order_id, "decided_at": row.ts, "policy": pol, "decision": "HOLD" if orig["hold"] else "CHARGE",
           "kind": orig["kind"], "reason": orig["reason"], "score": orig.get("score"), "factors": [], "counterfactuals": [], "replay": None}
    total = orig.get("score") or 0
    for b in orig.get("breakdown") or []:
        key = policy_mod.provenance_key(b["signal"])
        share = (b["contribution"] / total) if total else 0
        out["factors"].append({"signal": b["signal"], "contribution": b["contribution"], "share": round(share, 3),
                               "severity": "major" if share >= 0.2 else "moderate" if share >= 0.08 else "minor",
                               "provenance": policy_mod.PROVENANCE.get(key)})
    if orig["kind"] == "device_spread":
        out["factors"].append({"signal": orig["reason"], "contribution": None, "share": None, "severity": "major",
                               "provenance": policy_mod.PROVENANCE["device_id"]})
    if not row.inputs_json:
        out["replay"] = {"possible": False, "note": "The evidence snapshot was too large to keep, so this decision cannot be replayed. We report that limit instead of faking a replay."}
        return out
    intact = hashlib.sha256(row.inputs_json.encode()).hexdigest() == row.inputs_sha256
    inputs = json.loads(row.inputs_json)
    replayed = decide(inputs["accounts"], inputs["txns"], row.account_id, pol)
    now = decide(inputs["accounts"], inputs["txns"], row.account_id, policy_mod.current())
    out["counterfactuals"] = counterfactuals(inputs["accounts"], inputs["txns"], row.account_id, pol, orig)
    out["replay"] = {"possible": True, "evidence_intact": intact, "matches_original": intact and _same(replayed, orig),
                     "policy_version_original": pol["version"], "policy_version_now": policy_mod.current()["version"],
                     "current_policy_gives_same": _same(now, orig), "current_decision": "HOLD" if now["hold"] else "CHARGE",
                     "note": ("Replayed from the stored evidence under the original policy." if intact else
                              "The stored evidence no longer matches its hash, so it was NOT replayed. Something changed it.")}
    if not intact:
        out["replay"]["matches_original"] = False
    return out


def _paypal_view(data):
    """What PayPal says about an order's money, from one GET on the order."""
    pu = (data.get("purchase_units") or [{}])[0]
    pay = pu.get("payments") or {}
    auths, caps = pay.get("authorizations") or [], pay.get("captures") or []
    return {"order": data.get("status"), "authorization": auths[0].get("status") if auths else None,
            "capture": caps[0].get("status") if caps else None, "capture_id": caps[0].get("id") if caps else None,
            "amount": (pu.get("amount") or {}).get("value")}


def reconcile(s, o: Order):
    """Settle an order whose outcome we could not confirm, using PayPal's answer as the truth."""
    v = _paypal_view(paypal.get_order(o.id))
    if v["capture"] in ("COMPLETED", "PENDING", "PARTIALLY_REFUNDED", "REFUNDED") and v["capture_id"]:
        cid = v["capture_id"]
        pending = s.get(Txn, f"auth:{o.auth_id}")
        ts = pending.ts if pending else o.ts
        if pending:
            s.delete(pending)
            s.flush()
        if not s.get(Txn, cid):
            s.add(Txn(id=cid, account_id=o.account_id, ts=ts, amount=o.amount, capture_id=cid, refunded=1 if v["capture"] == "REFUNDED" else 0))
        o.capture_id, o.status = cid, "captured"
        return "captured", v
    if v["authorization"] == "VOIDED":
        o.status = "voided"
        return "voided", v
    if v["authorization"] in ("CREATED", "PENDING"):
        o.status, o.reason = "pending_review", ((o.reason or "") + " Reconciled with PayPal: still authorized, nothing captured.").strip()
        return "still_authorized", v
    return "unresolved", v


def verify(s, o: Order):
    """Compare our record of an order with PayPal's, field by field."""
    v = _paypal_view(paypal.get_order(o.id))
    refunded = bool(o.capture_id and (s.get(Txn, o.capture_id) or Txn(refunded=0)).refunded)
    ours_state = {"pending_review": "authorized, not captured", "authorized": "authorized, not captured", "voided": "voided",
                  "captured": "captured, then refunded" if refunded else "captured"}.get(o.status, o.status)
    if o.status in ("pending_review", "authorized"):
        ok = v["authorization"] in ("CREATED", "PENDING") and not v["capture"]
    elif o.status == "voided":
        ok = v["authorization"] == "VOIDED" and not v["capture"]
    elif o.status == "captured":
        ok = (v["capture"] in ("REFUNDED", "PARTIALLY_REFUNDED")) if refunded else (v["capture"] in ("COMPLETED", "PENDING"))
    else:
        ok = False   # outcome_unknown, capturing, voiding: not settled, so it cannot match yet
    theirs = f"authorization {v['authorization'] or 'none'}" + (f", capture {v['capture']}" if v["capture"] else "")
    checks = [{"field": "Money state", "ours": ours_state, "paypal": theirs, "ok": ok},
              {"field": "Amount", "ours": f"{o.amount:.2f}", "paypal": str(v["amount"]), "ok": str(v["amount"]) in (f"{o.amount:.2f}", f"{o.amount}")}]
    if o.capture_id or v["capture_id"]:
        checks.append({"field": "Capture ID", "ours": o.capture_id or "none", "paypal": v["capture_id"] or "none", "ok": (o.capture_id or None) == (v["capture_id"] or None)})
    match = all(c["ok"] for c in checks)
    return {"order_id": o.id, "match": match, "checks": checks, "paypal": v,
            "verdict": "Our record and PayPal agree." if match else
                       "Our record and PayPal disagree, or the outcome is still unsettled. Use Reconcile to settle it from PayPal's answer."}


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
