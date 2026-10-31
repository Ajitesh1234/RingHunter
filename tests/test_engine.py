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


def test_dispute_links_to_account_and_drafts_evidence():
    from app.db import SessionLocal, Account, Txn, init_db
    from app import disputes
    init_db()
    s = SessionLocal()
    s.merge(Account(id="D1", device_id="x", ip="1.1.1.1", shipping_address="a|1|us"))
    s.merge(Txn(id="CAPX", account_id="D1", ts="2026-10-28T10:00:00", amount=25.0, capture_id="CAPX"))
    s.commit()
    d = disputes.upsert_from_resource(s, {"dispute_id": "PP-R-TEST-1", "reason": "MERCHANDISE_OR_SERVICE_NOT_RECEIVED",
                                          "status": "OPEN", "dispute_amount": {"value": "25.00"},
                                          "disputed_transactions": [{"seller_transaction_id": "CAPX"}]})
    s.commit()
    assert d.account_id == "D1" and d.amount == 25.0
    draft = disputes.draft_evidence(s, d, carrier="UPS", tracking_number="1Z999")
    assert "1Z999" in draft["notes"] and "CAPX" in draft["notes"]
    body = disputes.evidence_body(draft["notes"], "UPS", "1Z999")
    assert body["evidences"][0]["evidence_type"] == "PROOF_OF_FULFILLMENT"


def test_evidence_template_never_claims_shipping_without_tracking():
    from app.disputes import _template
    f = {"capture_id": "C1", "amount": 25, "order_time": "2026-10-28T10:00:00", "carrier": None, "tracking_number": None}
    assert "shipped" not in _template(f).lower() and "$25.00" in _template(f)


def test_llm_output_is_clamped_to_core_members(monkeypatch):
    import json as _j
    from app import investigator, llm
    from app.db import SessionLocal, init_db
    init_db()
    entry = {"cluster_members": ["A", "B", "C", "P"], "cluster_size": 4, "shared_attribute_types": ["device_id"],
             "decision": "FLAGGED_FOR_AUTO_ACTION", "suspicion_score": 0.81, "time_synchronization_score": 1,
             "edges": [{"pair": ["A", "B"], "shared_attributes": ["device_id"]},
                       {"pair": ["B", "C"], "shared_attributes": ["device_id"]},
                       {"pair": ["P", "A"], "shared_attributes": ["shipping_address"]}]}
    bad = {"summary": "s", "confidence": "high", "key_evidence": ["e"], "caveats": [],
           "recommendations": [{"action": "refund_transactions", "account_ids": ["A", "P", "ZZZ"], "reason": "r"},
                               {"action": "hold_accounts", "account_ids": ["P"], "reason": "r"}]}
    monkeypatch.setattr(llm, "provider", lambda: "groq")
    monkeypatch.setattr(llm, "model_name", lambda: "fake-model")
    monkeypatch.setattr(llm, "run_agent", lambda *a, **k: "here: " + _j.dumps(bad))
    cf, who = investigator.investigate(SessionLocal(), entry)
    assert who == "fake-model" and len(cf.recommendations) == 1
    assert cf.recommendations[0].account_ids == ["A"]  # peripheral P and unknown ZZZ removed; P-only hold dropped


def test_llm_failure_falls_back_to_offline(monkeypatch):
    from app import investigator, llm
    from app.db import SessionLocal, init_db
    init_db()
    entry = {"cluster_members": ["A", "B"], "cluster_size": 2, "shared_attribute_types": ["device_id"],
             "decision": "FLAGGED_FOR_HUMAN_REVIEW", "suspicion_score": 0.6, "time_synchronization_score": 1,
             "edges": [{"pair": ["A", "B"], "shared_attributes": ["device_id"]}]}
    monkeypatch.setattr(llm, "provider", lambda: "groq")
    def boom(*a, **k): raise RuntimeError("rate limited")
    monkeypatch.setattr(llm, "run_agent", boom)
    monkeypatch.setattr(llm, "complete", boom)
    cf, who = investigator.investigate(SessionLocal(), entry)
    assert who == "offline-fallback" and cf.recommendations and "rate limited" in cf.fallback_reason


def test_single_shot_answer_used_when_tool_calling_fails(monkeypatch):
    import json as _j
    from app import investigator, llm
    from app.db import SessionLocal, init_db
    init_db()
    entry = {"cluster_members": ["A", "B"], "cluster_size": 2, "shared_attribute_types": ["device_id"],
             "decision": "FLAGGED_FOR_HUMAN_REVIEW", "suspicion_score": 0.6, "time_synchronization_score": 1,
             "edges": [{"pair": ["A", "B"], "shared_attributes": ["device_id"]}]}
    ok = {"summary": "s", "confidence": "medium", "key_evidence": ["e"], "caveats": [],
          "recommendations": [{"action": "request_more_info", "account_ids": ["A", "B"], "reason": "r"}]}
    def boom(*a, **k): raise RuntimeError("tool_use_failed")
    monkeypatch.setattr(llm, "provider", lambda: "groq")
    monkeypatch.setattr(llm, "model_name", lambda: "fake-model")
    monkeypatch.setattr(llm, "run_agent", boom)
    monkeypatch.setattr(llm, "complete", lambda *a, **k: _j.dumps(ok))
    cf, who = investigator.investigate(SessionLocal(), entry)
    assert who == "fake-model" and cf.recommendations[0].action == "request_more_info"


def test_groq_reasoning_models_get_low_effort_and_room_to_answer(monkeypatch):
    from app import llm
    from app.config import settings
    sent = {}
    class R:
        status_code = 200
        text = ""
        def json(self): return {"choices": [{"message": {"content": "ok"}}]}
    monkeypatch.setattr(settings, "groq_key", "k", raising=False)
    monkeypatch.setattr(settings, "groq_model", "openai/gpt-oss-120b", raising=False)
    monkeypatch.setattr(llm.httpx, "post", lambda url, **kw: sent.update(kw["json"]) or R())
    assert llm.complete("s", "u", 50) == "ok"
    assert sent["reasoning_effort"] == "low" and sent["model"] == "openai/gpt-oss-120b"


def test_model_caveats_always_include_the_standard_disclaimers():
    from app.investigator import _enforce, CaseFile, Recommendation
    cf = CaseFile(summary="s", confidence="high", key_evidence=["e"], caveats=["model caveat"],
                  recommendations=[Recommendation(action="hold_accounts", account_ids=["A"], reason="r")])
    out = _enforce(cf, {"A"}, {"A"})
    assert "model caveat" in out.caveats and "Heuristic graph scoring, not proof of fraud." in out.caveats
