"""Self-evaluation: attack our own screening with synthetic rings and innocent look-alikes, and report the results.
Runs entirely in memory (no database, no PayPal). It replays purchases in time order, exactly like the live checkout:
after each purchase it runs detection and HOLDS the buyer only if they share a device or card with a flagged ring."""
import functools
import math
import random
from datetime import datetime, timedelta
from . import policy as policy_mod
from .screening import decide

T0 = datetime(2026, 11, 2, 10, 0, 0)
PRICE = 25.0

# name, kind, device, address, ip, gap (low, high, unit), group size range
ATTACKS = [
    ("Same device, buying within minutes", "shared", "unique", "unique", (1, 4, "m")),
    ("Same device + shared address, within minutes", "shared", "shared", "unique", (1, 4, "m")),
    ("Same device, spread over days", "shared", "unique", "unique", (6, 30, "h")),
    ("Chain: neighbours share a device or address", "chain", "chain", "unique", (1, 4, "m")),
    ("Rotating devices, shared address only (blind spot)", "unique", "shared", "unique", (1, 4, "m")),
]
INNOCENT = [
    ("Strangers: nothing shared", "unique", "unique", "unique", (1, 60, "m")),
    ("Office: shared IP only", "unique", "unique", "shared", (5, 60, "m")),
    ("Family: shared address, own devices, hours apart", "unique", "shared", "unique", (2, 10, "h")),
    ("Roommates: shared address + IP, same evening", "unique", "shared", "shared", (10, 120, "m")),
    ("Family tablet: one device, days apart (hard case)", "shared", "shared", "unique", (24, 120, "h")),
]


def _value(mode, tag, i):
    """shared: everyone; unique: nobody; pairs: (0,1),(2,3),...;
    chain: devices pair up (0,1),(2,3),... while addresses pair the neighbours (1,2),(3,4),... so the ring is linked end to end."""
    if mode == "shared":
        return f"{tag}-shared"
    if mode == "pairs":
        return f"{tag}-{i // 2}"
    if mode == "chain":
        return f"{tag}-{i // 2}" if tag.endswith("-dev") else f"{tag}-{(i + 1) // 2}"
    return f"{tag}-{i}"


def _purchases(spec, n, rng, label):
    _, dev, addr, ip, (lo, hi, unit) = spec
    t, out = T0, []
    for i in range(n):
        out.append({"id": f"{label}-{i}", "device": _value(dev, f"{label}-dev", i), "addr": _value(addr, f"{label}-adr", i),
                    "ip": _value(ip, f"{label}-ip", i), "ts": t})
        t += timedelta(**{"minutes" if unit == "m" else "hours": rng.uniform(lo, hi)})
    return out


def _replay(purchases, use_spread=True, policy=None):
    """Replays purchases in time order through the SAME decision function the live checkout uses."""
    pol = dict(policy or policy_mod.current())
    if not use_spread:
        pol["spread_min_accounts"] = 10 ** 9
    accounts, txns, out = {}, {}, []
    for p in purchases:
        accounts[p["id"]] = {"device_id": p["device"], "card_fp": f"none_card_{p['id']}", "ip": p["ip"],
                             "shipping_address": p["addr"]}
        txns[p["id"]] = [{"timestamp": p["ts"].isoformat(timespec="seconds"), "amount": PRICE}]
        d = decide(accounts, txns, p["id"], pol)
        out.append({"hold": d["hold"], "reason": d["reason"]})
    return out


def replay(purchases, use_spread=True):
    """Which purchases would live screening hold? (the first buyer of a ring can never be caught: nothing links them yet)"""
    return [r["hold"] for r in _replay(purchases, use_spread)]


SCENARIOS = [(s, "attack") for s in ATTACKS] + [(s, "innocent") for s in INNOCENT]


def scenario_names():
    return [("ATTACK: " if k == "attack" else "INNOCENT: ") + s[0] for s, k in SCENARIOS]


def replay_story(i: int) -> dict:
    """One scenario, purchase by purchase, in plain language, for the no-login walkthrough."""
    i = max(0, min(int(i), len(SCENARIOS) - 1))
    spec, kind = SCENARIOS[i]
    rng = random.Random(777 + i)
    ps = _purchases(spec, 5 if kind == "attack" else 3, rng, f"story{i}")
    res = _replay(ps)
    names = {"device": {}, "addr": {}, "ip": {}}
    def lab(field, prefix, value, letters):
        m = names[field]
        if value not in m:
            m[value] = prefix + (chr(65 + len(m)) if letters else str(len(m) + 1))
        return m[value]
    steps = []
    for k, (p, r) in enumerate(zip(ps, res)):
        mins = (p["ts"] - ps[0]["ts"]).total_seconds() / 60
        when = "start" if k == 0 else (f"+{round(mins)} min" if mins < 180 else f"+{round(mins / 60)} h")
        steps.append({"buyer": f"Buyer {k + 1}", "device": lab("device", "Device ", p["device"], True),
                      "address": lab("addr", "Address ", p["addr"], False), "ip": lab("ip", "IP ", p["ip"], False),
                      "when": when, "decision": "held" if r["hold"] else "charged", "reason": r["reason"]})
    held = sum(1 for s in steps if s["decision"] == "held")
    return {"index": i, "title": spec[0], "kind": kind, "steps": steps, "held": held, "prevented_usd": held * PRICE}


def wilson(k, n, z=1.96):
    """95% confidence interval for a proportion k/n (Wilson score)."""
    if n == 0:
        return 0.0, 0.0
    p, d = k / n, 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return max(0.0, c - h), min(1.0, c + h)


def naive_replay(purchases):
    """Baseline for comparison: hold anyone who shares ANY attribute (device, address or IP) with an earlier buyer."""
    return [any(p["device"] == q["device"] or p["addr"] == q["addr"] or p["ip"] == q["ip"] for q in purchases[:i])
            for i, p in enumerate(purchases)]


def _sweep(seeds, offset):
    """What happens as the hold threshold moves: follow-up ring buyers caught versus innocent buyers wrongly held."""
    out = []
    for thr in (0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9):
        pol = {**policy_mod.current(), "min_score": thr}
        caught = base = held_i = buyers_i = 0
        for k, spec in enumerate(ATTACKS):
            for s in range(seeds):
                rng = random.Random(offset + 1000 * k + s)
                n = rng.randint(3, 6)
                held = [x["hold"] for x in _replay(_purchases(spec, n, rng, f"w{k}t{s}"), policy=pol)]
                base, caught = base + n - 1, caught + sum(held[1:])
        for k, spec in enumerate(INNOCENT):
            for s in range(seeds):
                rng = random.Random(offset + 5000 + 1000 * k + s)
                n = rng.randint(2, 4)
                held = [x["hold"] for x in _replay(_purchases(spec, n, rng, f"v{k}t{s}"), policy=pol)]
                buyers_i, held_i = buyers_i + n, held_i + sum(held)
        out.append({"min_score": thr, "recall_pct": round(100 * caught / base), "false_hold_pct": round(100 * held_i / buyers_i, 1)})
    return out


@functools.lru_cache(maxsize=8)
def _run_eval(seeds: int, offset: int) -> dict:
    attacks, innocent = [], []
    for k, spec in enumerate(ATTACKS):
        caught = base = before = naive = 0
        prevented = 0.0
        for s in range(seeds):
            rng = random.Random(offset + 1000 * k + s)
            n = rng.randint(3, 6)
            ps = _purchases(spec, n, rng, f"a{k}t{s}")
            held = replay(ps)
            base, caught, prevented = base + n - 1, caught + sum(held[1:]), prevented + PRICE * sum(held[1:])
            before += sum(replay(ps, use_spread=False)[1:])
            naive += sum(naive_replay(ps)[1:])
        lo, hi = wilson(caught, base)
        attacks.append({"name": spec[0], "blocked_pct": round(100 * caught / base), "ci_low": round(100 * lo), "ci_high": round(100 * hi),
                        "before_spread_rule_pct": round(100 * before / base), "naive_blocked_pct": round(100 * naive / base),
                        "follow_up_buyers": base, "avg_prevented_usd": round(prevented / seeds, 2)})
    for k, spec in enumerate(INNOCENT):
        affected = naive_aff = 0
        for s in range(seeds):
            rng = random.Random(offset + 5000 + 1000 * k + s)
            n = rng.randint(2, 4)
            ps = _purchases(spec, n, rng, f"i{k}t{s}")
            affected += any(replay(ps))
            naive_aff += any(naive_replay(ps))
        lo, hi = wilson(affected, seeds)
        innocent.append({"name": spec[0], "groups_affected_pct": round(100 * affected / seeds), "ci_low": round(100 * lo), "ci_high": round(100 * hi),
                         "naive_groups_affected_pct": round(100 * naive_aff / seeds), "groups": seeds})
    scored = [a for a in attacks if "blind spot" not in a["name"]]
    avg = lambda rows, key: round(sum(r[key] for r in rows) / len(rows))
    return {"trials_per_scenario": seeds, "draw_offset": offset, "attacks": attacks, "innocent": innocent,
            "summary": {"avg_followup_buyers_blocked_pct": avg(scored, "blocked_pct"), "avg_innocent_groups_disrupted_pct": avg(innocent, "groups_affected_pct"),
                        "naive_avg_followup_buyers_blocked_pct": avg(scored, "naive_blocked_pct"),
                        "naive_avg_innocent_groups_disrupted_pct": avg(innocent, "naive_groups_affected_pct")},
            "sweep": _sweep(min(seeds, 30), offset),
            "note": "Synthetic scenarios written by us, replayed through the same decision function the live checkout uses. The first buyer in a ring "
                    "can never be blocked. Intervals are 95% Wilson. Averages exclude the labelled blind spot. The scenario families are ours, "
                    "which is why the AI red team exists. Real traffic will differ."}


def run_eval(seeds: int = 20, offset: int = 0) -> dict:
    """offset 0 = the draws used while developing. The dashboard uses a different offset: draws nobody looked at while tuning."""
    return _run_eval(max(1, min(int(seeds), 100)), int(offset))
