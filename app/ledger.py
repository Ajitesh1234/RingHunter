"""Append-only, hash-chained audit ledger. Every event stores the hash of the one before it, so editing, deleting or
reordering an earlier event breaks the chain and `verify` says where.

Honest limits: this is tamper-EVIDENT for anyone who cannot rewrite the whole table. Someone with full database access
could recompute every hash, and cutting events off the END of the chain cannot be detected from inside the database.
That is why the dashboard shows the head hash: record it somewhere outside (for example in the demo video) to anchor it."""
import hashlib
import json
from datetime import datetime, timezone
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from .db import LedgerEvent

GENESIS = "0" * 64


def _canon(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def _hash(seq, ts, actor, op, ref, payload_json, prev):
    return hashlib.sha256(_canon({"seq": seq, "ts": ts, "actor": actor, "op": op, "ref": ref, "payload": payload_json, "prev": prev}).encode()).hexdigest()


def append(s, actor, op, ref=None, payload=None):
    """Add one event to the chain. Commits. Call it after the action it records has been committed."""
    for _ in range(4):
        last = s.scalars(select(LedgerEvent).order_by(LedgerEvent.seq.desc())).first()
        seq, prev = (last.seq + 1, last.hash) if last else (1, GENESIS)
        ts = datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")
        pj = _canon(payload or {})
        ev = LedgerEvent(seq=seq, ts=ts, actor=actor, op=op, ref=ref, payload_json=pj, prev_hash=prev, hash=_hash(seq, ts, actor, op, ref, pj, prev))
        s.add(ev)
        try:
            s.commit()
            return ev
        except IntegrityError:   # two writers picked the same sequence number: take the next one
            s.rollback()
    raise RuntimeError("could not append to the audit ledger")


def verify(s):
    rows = list(s.scalars(select(LedgerEvent).order_by(LedgerEvent.seq)))
    problems, prev, expect = [], GENESIS, 1
    for r in rows:
        if r.seq != expect:
            problems.append(f"#{r.seq}: sequence gap (expected #{expect}); an event was removed or reordered")
            expect = r.seq
        if r.prev_hash != prev:
            problems.append(f"#{r.seq}: link to the previous event is broken")
        if _hash(r.seq, r.ts, r.actor, r.op, r.ref, r.payload_json, r.prev_hash) != r.hash:
            problems.append(f"#{r.seq}: content does not match its hash; the event was edited")
        prev, expect = r.hash, expect + 1
    return {"ok": not problems, "count": len(rows), "head": rows[-1].hash if rows else GENESIS, "problems": problems[:10]}


def recent(s, limit=12):
    rows = list(s.scalars(select(LedgerEvent).order_by(LedgerEvent.seq.desc()).limit(limit)))
    return [{"seq": r.seq, "ts": r.ts, "actor": r.actor, "op": r.op, "ref": r.ref, "payload": json.loads(r.payload_json), "hash": r.hash[:12]} for r in rows]
