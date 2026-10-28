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
