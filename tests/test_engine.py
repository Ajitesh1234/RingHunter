import os
import tempfile
_db = os.path.join(tempfile.mkdtemp(), "t.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_db}"
from fastapi.testclient import TestClient
from app.main import app


def test_ring_flagged_family_and_office_not():
    with TestClient(app) as c:
        r = c.post("/api/demo/seed").json()
        cases = c.get("/api/cases").json()
        flagged = [set(x["cluster"]["cluster_members"]) for x in cases]
        assert {"R1", "R2", "R3", "R4"} in flagged            # ring caught as ONE case
        assert all(not (m & {"F1", "F2", "F3", "O1", "O2", "O3"}) for m in flagged)  # no false positives


def test_investigate_approve_is_idempotent_and_blocks():
    with TestClient(app) as c:
        c.post("/api/demo/seed")
        cid = c.get("/api/cases").json()[0]["id"]
        inv = c.post(f"/api/cases/{cid}/investigate").json()
        assert inv["casefile"]["recommendations"]
        a = c.post(f"/api/cases/{cid}/approve", json={"recommendation_index": 0}).json()
        b = c.post(f"/api/cases/{cid}/approve", json={"recommendation_index": 0}).json()
        assert b["idempotent_replay"] is True and a["accounts"]


def test_growing_ring_supersedes_smaller_case():
    from app.db import SessionLocal, Account, Txn, init_db
    from app import engine as eng
    init_db()
    s = SessionLocal()
    def add(n, minute):
        s.merge(Account(id=n, device_id="devX", ip="1.1.1.1", shipping_address="same|1|us"))
        s.merge(Txn(id=f"t{n}", account_id=n, ts=f"2026-10-28T10:0{minute}:00", amount=25.0))
        s.commit()
        eng.detect(s)
    add("G1", 0); add("G2", 1); add("G3", 2)
    open_cases = [c for c in eng.detect(s) if c.status == "open" and "G1" in c.entry_json]
    assert len(open_cases) == 1 and open_cases[0].as_dict()["cluster"]["cluster_size"] == 3


def test_peripheral_address_only_account_excluded_from_actions():
    from app.investigator import _offline
    e = {"cluster_members": ["A", "B", "C", "P"], "cluster_size": 4, "shared_attribute_types": ["device_id", "shipping_address"],
         "decision": "FLAGGED_FOR_AUTO_ACTION", "suspicion_score": 0.81, "time_synchronization_score": 1,
         "edges": [{"pair": ["A", "B"], "shared_attributes": ["device_id", "shipping_address"]},
                   {"pair": ["B", "C"], "shared_attributes": ["device_id"]},
                   {"pair": ["P", "A"], "shared_attributes": ["shipping_address"]}]}
    cf = _offline(e, {"transactions": 4, "total_amount": 100.0})
    acts = {r.action: r.account_ids for r in cf.recommendations}
    assert set(acts["hold_accounts"]) == {"A", "B", "C"} and "P" not in acts["refund_transactions"]


def test_closed_case_cannot_be_reinvestigated():
    with TestClient(app) as c:
        c.post("/api/demo/seed")
        cid = c.get("/api/cases").json()[0]["id"]
        c.post(f"/api/cases/{cid}/investigate")
        c.post(f"/api/cases/{cid}/approve", json={"recommendation_index": 0})
        assert c.post(f"/api/cases/{cid}/investigate").status_code == 409
