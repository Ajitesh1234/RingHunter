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


def test_admin_token_guards_state_changing_endpoints(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "admin_token", "s3cret", raising=False)
    with TestClient(app) as c:
        assert c.post("/api/detect").status_code == 401
        assert c.post("/api/detect", headers={"X-Admin-Token": "wrong"}).status_code == 401
        assert c.post("/api/detect", headers={"X-Admin-Token": "s3cret"}).status_code == 200
        assert c.get("/api/cases").status_code == 200       # read-only stays public
        assert c.get("/dashboard").status_code == 200


def test_demo_reset_clears_cases_but_keeps_disputes():
    from app.db import SessionLocal, Dispute, init_db
    init_db()
    s = SessionLocal()
    s.merge(Dispute(id="PP-R-KEEP", raw_json="{}", evidence_submitted=0))
    s.commit()
    with TestClient(app) as c:
        c.post("/api/demo/seed")
        assert c.get("/api/cases").json()
        assert c.post("/api/demo/reset").json() == {"reset": True}
        assert c.get("/api/cases").json() == []
        assert any(d["id"] == "PP-R-KEEP" for d in c.get("/api/disputes").json())


def test_refund_is_always_offered_as_optional_and_more_info_keeps_case_open():
    from app.investigator import _enforce, CaseFile, Recommendation
    cf = CaseFile(summary="s", confidence="medium", key_evidence=["e"], caveats=[],
                  recommendations=[Recommendation(action="hold_accounts", account_ids=["A", "B"], reason="r")])
    out = _enforce(cf, {"A", "B"}, {"A", "B"})
    assert [r.action for r in out.recommendations] == ["hold_accounts", "refund_transactions"]
    assert "approve only if you agree" in out.recommendations[1].reason
    with TestClient(app) as c:
        c.post("/api/demo/seed")
        cid = c.get("/api/cases").json()[0]["id"]
        c.post(f"/api/cases/{cid}/investigate")
        first = c.get("/api/cases").json()[0]
        assert first["done"] == []
        c.post(f"/api/cases/{cid}/approve", json={"recommendation_index": 0})
        again = [x for x in c.get("/api/cases").json() if x["id"] == cid][0]
        assert again["done"] == [0] and again["status"] == "actioned"


def test_create_order_supports_authorize_intent(monkeypatch):
    from app import paypal
    seen = {}
    def fake(method, path, body=None, request_id=None):
        seen.update(path=path, body=body)
        return {"id": "O1", "links": [{"rel": "payer-action", "href": "https://x"}]}
    monkeypatch.setattr(paypal, "_call", fake)
    out = paypal.create_order("25.00", "USD", "r", "c", intent="AUTHORIZE")
    assert seen["body"]["intent"] == "AUTHORIZE" and out["id"] == "O1"
    paypal.create_order("25.00", "USD", "r", "c")
    assert seen["body"]["intent"] == "CAPTURE"  # default behaviour unchanged


def test_screening_holds_strong_linked_buyer_and_captures_weak_ones():
    from app.db import SessionLocal, Account, Txn, init_db
    from app import screening
    init_db()
    s = SessionLocal()
    s.merge(Account(id="S1", device_id="devS", ip="9.9.9.1", shipping_address="x|1|us"))
    s.merge(Txn(id="CAPS1", account_id="S1", ts="2026-10-28T11:00:00", amount=25.0, capture_id="CAPS1"))
    s.merge(Account(id="S2", device_id="devS", ip="9.9.9.2", shipping_address="y|1|us"))
    s.merge(Txn(id="auth:A2", account_id="S2", ts="2026-10-28T11:02:00", amount=25.0, capture_id=None))
    s.merge(Account(id="S3", device_id="devOther", ip="9.9.9.3", shipping_address="x|1|us"))
    s.merge(Txn(id="auth:A3", account_id="S3", ts="2026-10-28T11:03:00", amount=25.0, capture_id=None))
    s.commit()
    assert screening.screen(s, "S2")[0] is True     # shares a device with S1: held for review
    assert screening.screen(s, "S3")[0] is False    # address-only link: weak evidence, captured normally


def test_order_capture_and_void_are_idempotent_and_exclusive(monkeypatch):
    from app import paypal
    from app.db import SessionLocal, Account, Txn, Order, init_db
    init_db()
    s = SessionLocal()
    s.merge(Account(id="V1", device_id="dv", ip="1", shipping_address="a"))
    for n in ("1", "2"):
        s.merge(Txn(id=f"auth:AV{n}", account_id="V1", ts="2026-10-28T12:00:00", amount=25.0))
        s.merge(Order(id=f"OV{n}", account_id="V1", auth_id=f"AV{n}", amount=25.0, ts="2026-10-28T12:00:00", status="pending_review"))
    s.commit()
    monkeypatch.setattr(paypal, "capture_authorization", lambda a: {"id": "CAP-" + a, "status": "COMPLETED"})
    monkeypatch.setattr(paypal, "void_authorization", lambda a: {})
    with TestClient(app) as c:
        assert c.post("/api/orders/OV1/capture").json()["status"] == "captured"
        assert c.post("/api/orders/OV1/capture").json()["idempotent_replay"] is True
        assert c.post("/api/orders/OV1/void").status_code == 409
        assert c.post("/api/orders/OV2/void").json()["status"] == "voided"
        assert c.post("/api/orders/OV2/void").json()["idempotent_replay"] is True
        assert c.post("/api/orders/OV2/capture").status_code == 409


def _paypal_env(monkeypatch, payers, amount="25.00", status="APPROVED", authorize=None):
    """Mocks PayPal for the checkout flow. payers maps a PayPal order id to (payer id, address number)."""
    from app import paypal
    from app.config import settings
    monkeypatch.setattr(settings, "screening", True, raising=False)
    monkeypatch.setattr(paypal, "enabled", lambda: True)
    tokens, calls = iter(list(payers)), {"create": [], "authorize": []}
    def create(amount_, currency, ret, cancel, intent="CAPTURE"):
        calls["create"].append((amount_, currency, intent))
        return {"id": next(tokens), "approve_url": "https://pay.example/approve"}
    def get_order(t):
        return {"status": status, "payer": {"payer_id": payers[t][0]},
                "purchase_units": [{"amount": {"value": amount, "currency_code": "USD"}}]}
    def authorize_order(t):
        calls["authorize"].append(t)
        pid, n = payers[t]
        return {"payer": {"payer_id": pid, "email_address": f"{pid}@x.test"},
                "purchase_units": [{"shipping": {"address": {"address_line_1": f"{n} elm st", "postal_code": str(n), "country_code": "US"}},
                                    "payments": {"authorizations": [{"id": f"AUTH-{t}", "amount": {"value": "25.00", "currency_code": "USD"}}]}}]}
    monkeypatch.setattr(paypal, "create_order", create)
    monkeypatch.setattr(paypal, "get_order", get_order)
    monkeypatch.setattr(paypal, "authorize_order", authorize_order)
    monkeypatch.setattr(paypal, "capture_authorization", lambda a: {"id": "CAP-" + a, "status": "COMPLETED"})
    return calls


def _buy(c, device):
    """Start a checkout the way the store page does, then come back from PayPal as the buyer."""
    tok = c.post("/checkout/create", json={"device_id": device}).json()["id"]
    return tok, c.get("/checkout/return", params={"token": tok})


def test_screened_checkout_end_to_end_with_mocked_paypal(monkeypatch):
    _paypal_env(monkeypatch, {"TOK-E1": ("E1", 10), "TOK-E2": ("E2", 20)})
    with TestClient(app) as c:
        t1, r1 = _buy(c, "e2e-ring")
        t2, r2 = _buy(c, "e2e-ring")
        orders = {o["id"]: o for o in c.get("/api/orders").json()}
        assert "payment received" in r1.text and orders[t1]["status"] == "captured"
        assert "being reviewed" in r2.text and orders[t2]["status"] == "pending_review"
        assert "device or card" in orders[t2]["reason"]


def test_returning_buyer_cannot_shake_ring_by_switching_device(monkeypatch):
    from app.db import SessionLocal, Account
    _paypal_env(monkeypatch, {"T1": ("R9A", 1), "T2": ("R9B", 2), "T3": ("R9A", 3)})   # R9A comes back
    with TestClient(app) as c:
        _buy(c, "sticky-ring")
        _buy(c, "sticky-ring")
        _, r3 = _buy(c, "a-brand-new-browser")   # tries to shake the link
        assert SessionLocal().get(Account, "R9A").device_id == "sticky-ring"
        assert "being reviewed" in r3.text


def test_price_is_set_by_the_server_not_the_browser(monkeypatch):
    calls = _paypal_env(monkeypatch, {"TP1": ("P1", 1)})
    with TestClient(app) as c:
        c.post("/checkout/create", json={"device_id": "d", "amount": "0.01"})
        assert calls["create"] == [("25.00", "USD", "AUTHORIZE")]


def test_returning_order_must_match_the_checkout_we_started(monkeypatch):
    from app.db import SessionLocal, Checkout
    calls = _paypal_env(monkeypatch, {"TQ1": ("Q1", 1), "TQ2": ("Q2", 2)})
    with TestClient(app) as c:
        unknown = c.get("/checkout/return", params={"token": "NEVER-STARTED-HERE"})
        assert "not recognised" in unknown.text and calls["authorize"] == []          # PayPal is never touched
        tok, first = _buy(c, "once")
        again = c.get("/checkout/return", params={"token": tok})                       # reload / replayed link
        assert "payment received" in first.text and "already processed" in again.text
        assert calls["authorize"] == [tok]                                              # authorized exactly once
        assert SessionLocal().get(Checkout, tok).status == "used"


def test_amount_or_status_mismatch_is_declined_before_any_money_moves(monkeypatch):
    from app.db import SessionLocal, Checkout
    calls = _paypal_env(monkeypatch, {"TM1": ("M1", 1)}, amount="1.00")                 # PayPal says $1, we asked for $25
    with TestClient(app) as c:
        tok, r = _buy(c, "d")
        assert "Order declined" in r.text and calls["authorize"] == []
        assert SessionLocal().get(Checkout, tok).status == "rejected"
    calls = _paypal_env(monkeypatch, {"TM2": ("M2", 2)}, status="CREATED")             # buyer never approved
    with TestClient(app) as c:
        _, r = _buy(c, "d")
        assert "Order declined" in r.text and calls["authorize"] == []


def test_admin_endpoints_fail_closed_when_the_token_is_missing_in_production(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "admin_required", True, raising=False)
    monkeypatch.setattr(settings, "admin_token", "", raising=False)
    with TestClient(app) as c:
        assert c.post("/api/detect").status_code == 503
        assert c.get("/api/cases").status_code == 200                                   # read-only stays public
        monkeypatch.setattr(settings, "admin_token", "tok", raising=False)
        assert c.post("/api/detect").status_code == 401
        assert c.post("/api/detect", headers={"X-Admin-Token": "tok"}).status_code == 200


def test_order_transitions_are_atomic_and_roll_back_on_paypal_failure(monkeypatch):
    from app import paypal
    from app.db import SessionLocal, Account, Txn, Order, init_db
    init_db()
    s = SessionLocal()
    s.merge(Account(id="AT1", device_id="atd", ip="1", shipping_address="a"))
    for n in ("1", "2"):
        s.merge(Txn(id=f"auth:AT{n}", account_id="AT1", ts="2026-11-05T10:00:00", amount=25.0))
    s.merge(Order(id="OAT1", account_id="AT1", auth_id="AT1", amount=25.0, ts="2026-11-05T10:00:00", status="pending_review"))
    s.merge(Order(id="OAT2", account_id="AT1", auth_id="AT2", amount=25.0, ts="2026-11-05T10:00:00", status="capturing"))   # another request owns it
    s.commit()
    def boom(a): raise paypal.PayPalError("declined by PayPal")
    monkeypatch.setattr(paypal, "capture_authorization", boom)
    monkeypatch.setattr(paypal, "void_authorization", boom)
    with TestClient(app) as c:
        assert c.post("/api/orders/OAT2/capture").status_code == 409                    # already being processed
        assert c.post("/api/orders/OAT2/void").status_code == 409
        assert c.post("/api/orders/OAT1/capture").status_code == 502                    # PayPal failed...
        assert {o["id"]: o for o in c.get("/api/orders").json()}["OAT1"]["status"] == "pending_review"   # ...so it is retryable
        assert c.post("/api/orders/OAT1/void").status_code == 502
        assert {o["id"]: o for o in c.get("/api/orders").json()}["OAT1"]["status"] == "pending_review"


def test_spread_rule_holds_slow_ring_but_not_a_household():
    from app.screening import spread_rule
    assert spread_rule(["a", "b", "c"]) is True
    assert spread_rule(["a", "a", "a"]) is False   # a household: one device, one address
    assert spread_rule(["a", "b"]) is False


def test_screen_holds_third_buyer_on_a_device_shipping_to_three_addresses():
    from app.db import SessionLocal, Account, Txn, init_db
    from app import screening
    init_db()
    s = SessionLocal()
    for i, day in enumerate(("10", "13", "16")):   # days apart, so the timing signal is quiet
        s.merge(Account(id=f"SP{i}", device_id="spread-dev", ip=f"7.7.7.{i}", shipping_address=f"{i} road|{i}|us"))
        s.merge(Txn(id=f"auth:SP{i}", account_id=f"SP{i}", ts=f"2026-10-{day}T09:00:00", amount=25.0))
    s.commit()
    hold, _, reason = screening.screen(s, "SP2")
    assert hold is True and "different addresses" in reason


def test_evaluation_is_deterministic_and_honest():
    from app.evaluation import run_eval
    r = run_eval(5)
    assert r == run_eval(5)
    att = {a["name"]: a for a in r["attacks"]}
    assert att["Same device, buying within minutes"]["blocked_pct"] == 100
    slow = att["Same device, spread over days"]
    assert slow["blocked_pct"] > slow["before_spread_rule_pct"]
    assert att["Rotating devices, shared address only (blind spot)"]["blocked_pct"] == 0   # reported, not hidden
    assert all(i["groups_affected_pct"] == 0 for i in r["innocent"])


def test_eval_endpoint_is_public_and_capped():
    with TestClient(app) as c:
        r = c.get("/api/eval", params={"seeds": 999}).json()
        assert r["trials_per_scenario"] == 40 and r["summary"]["avg_innocent_groups_disrupted_pct"] == 0


def test_orders_report_which_captured_payments_were_refunded():
    from app.db import SessionLocal, Account, Txn, Order, init_db
    init_db()
    s = SessionLocal()
    s.merge(Account(id="RF1", device_id="rf", ip="1", shipping_address="a"))
    s.merge(Txn(id="CAP-RF", account_id="RF1", ts="2026-11-02T10:00:00", amount=25.0, capture_id="CAP-RF", refunded=1))
    s.merge(Txn(id="CAP-KEEP", account_id="RF1", ts="2026-11-02T10:01:00", amount=25.0, capture_id="CAP-KEEP", refunded=0))
    s.merge(Order(id="ORF", account_id="RF1", auth_id="A1", amount=25.0, ts="2026-11-02T10:00:00", status="captured", capture_id="CAP-RF"))
    s.merge(Order(id="OKEEP", account_id="RF1", auth_id="A2", amount=25.0, ts="2026-11-02T10:01:00", status="captured", capture_id="CAP-KEEP"))
    s.commit()
    with TestClient(app) as c:
        o = {x["id"]: x for x in c.get("/api/orders").json()}
        assert o["ORF"]["refunded"] is True and o["OKEEP"]["refunded"] is False


def test_replay_walkthrough_is_public_deterministic_and_plain():
    with TestClient(app) as c:
        names = c.get("/api/replay").json()["scenarios"]
        assert len(names) == 10 and names[0].startswith("ATTACK") and names[-1].startswith("INNOCENT")
        a = c.get("/api/replay", params={"i": 0}).json()
        assert a == c.get("/api/replay", params={"i": 0}).json()
        assert a["steps"][0]["decision"] == "charged" and a["held"] >= 3 and a["prevented_usd"] == 25.0 * a["held"]
        assert all(s["reason"] for s in a["steps"] if s["decision"] == "held")
        for i in range(5, 10):   # every innocent scenario: nobody held
            assert c.get("/api/replay", params={"i": i}).json()["held"] == 0
        assert "Watch an attack" in c.get("/dashboard").text and "no login" in c.get("/").text


def test_chain_scenario_is_a_real_alternating_chain():
    import random
    from app.evaluation import _purchases, ATTACKS
    spec = [s for s in ATTACKS if s[0].startswith("Chain")][0]
    ps = _purchases(spec, 5, random.Random(1), "c")
    dev = [p["device"] for p in ps]
    adr = [p["addr"] for p in ps]
    assert dev[0] == dev[1] and dev[2] == dev[3] and dev[1] != dev[2]      # devices pair up (0,1) (2,3)
    assert adr[1] == adr[2] and adr[3] == adr[4] and adr[0] != adr[1]      # addresses pair neighbours (1,2) (3,4)


def test_redteam_scores_model_attacks_and_drops_invalid_ones(monkeypatch):
    import json as _j
    from app import llm, redteam
    good = lambda n, **kw: {"name": n, "ring_size": 5, "device": "shared", "address": "unique", "ip": "unique",
                            "gap_unit": "m", "gap_low": 1, "gap_high": 4, "rationale": "r", **kw}
    payload = {"attacks": [good("Burst on one device"), good("Slow drip", gap_unit="h", gap_low=20, gap_high=30),
                           good("Fully unique", device="unique"), good("Invalid size", ring_size=99), good("Bad mode", device="teleport")]}
    monkeypatch.setattr(llm, "provider", lambda: "groq")
    monkeypatch.setattr(llm, "model_name", lambda: "fake-model")
    monkeypatch.setattr(llm, "complete", lambda s, u, m=0, json_mode=False: _j.dumps(payload) if json_mode else "Commentary.")
    redteam._state.update(last=None, ts=0.0)
    r = redteam.run_live()
    assert r["source"] == "fake-model" and [s["name"] for s in r["strategies"]] == ["Burst on one device", "Slow drip", "Fully unique"]
    st = {s["name"]: s for s in r["strategies"]}
    assert st["Burst on one device"]["status"] == "caught" and st["Fully unique"]["status"] == "evaded"
    assert r["commentary"] == "Commentary." and r["evaded"] == 1


def test_redteam_falls_back_to_offline_sample_and_is_rate_limited(monkeypatch):
    from app import llm, redteam
    monkeypatch.setattr(llm, "provider", lambda: "groq")
    def boom(*a, **k): raise RuntimeError("rate limited")
    monkeypatch.setattr(llm, "complete", boom)
    redteam._state.update(last=None, ts=0.0)
    with TestClient(app) as c:
        r = c.post("/api/redteam").json()
        assert r["source"] == "offline sample" and len(r["strategies"]) == 6 and "failed" in r["note"]
        assert any(s["status"] == "evaded" for s in r["strategies"])        # we do not hide what gets through
        assert c.post("/api/redteam").status_code == 429                     # cooldown protects the free-tier quota
        assert c.get("/api/redteam").json()["source"] == "offline sample"


def test_activity_timeline_lists_flags_orders_and_approvals():
    with TestClient(app) as c:
        c.post("/api/demo/seed")
        cid = c.get("/api/cases").json()[0]["id"]
        c.post(f"/api/cases/{cid}/investigate")
        c.post(f"/api/cases/{cid}/approve", json={"recommendation_index": 0})
        ev = c.get("/api/activity").json()
        kinds = {e["kind"] for e in ev}
        assert {"case", "action"} <= kinds and any("Merchant approved" in e["text"] for e in ev)
        assert [e["when"] for e in ev] == sorted((e["when"] for e in ev), reverse=True)


def test_store_and_result_pages_keep_their_contract():
    with TestClient(app) as c:
        home = c.get("/").text
        assert "Pay with PayPal" in home and "/checkout/create" in home and "device" in home and "no login" in home
        assert "Merchant view" in c.get("/dashboard").text and "Activity" in c.get("/dashboard").text
    from app.main import _result_page
    assert "payment received" in _result_page("ok", "Thanks, payment received.")
    assert "Order declined." in _result_page("no", "Order declined.")


def test_redteam_daily_cap_protects_quota(monkeypatch):
    from datetime import datetime, timezone
    from app import llm, redteam
    monkeypatch.setattr(llm, "provider", lambda: "groq")
    monkeypatch.setattr(llm, "complete", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not call the model")))
    today = datetime.now(timezone.utc).date().isoformat()
    redteam._state.update(last=None, ts=0.0, day=today, runs=redteam.DAILY_CAP)
    r = redteam.run_live()
    assert "Daily live-run limit" in r["note"] and r["strategies"]


def _case_with_dispute(prefix):
    from app.db import SessionLocal, Account, Dispute, Order, init_db
    init_db()
    s = SessionLocal()
    for i in (1, 2):
        s.merge(Account(id=f"{prefix}{i}", device_id=f"{prefix}dev", ip=f"5.5.5.{i}", shipping_address=f"{i} lane"))
    s.merge(Dispute(id=f"PP-R-{prefix}", account_id=f"{prefix}1", reason="MERCHANDISE_OR_SERVICE_NOT_RECEIVED",
                    status="WAITING_FOR_SELLER_RESPONSE", amount=25.0, raw_json="{}", evidence_submitted=0))
    s.merge(Order(id=f"ORDER{prefix}", account_id=f"{prefix}2", amount=25.0, ts="2026-11-04T10:00:00", status="captured"))
    s.commit()
    return s, {f"{prefix}1", f"{prefix}2"}


def test_analyst_paypal_tools_are_scoped_to_the_case_and_report_their_path(monkeypatch):
    from app import investigator, paypal_tools
    s, members = _case_with_dispute("TKA")
    monkeypatch.setattr(paypal_tools, "get_dispute", lambda d: ({"status": "WAITING_FOR_SELLER_RESPONSE", "reason": "X",
                         "dispute_amount": {"value": "25.00"}, "messages": [1], "evidences": [1, 2]}, "PayPal Agent Toolkit"))
    monkeypatch.setattr(paypal_tools, "get_order", lambda o: ({"id": o, "status": "COMPLETED", "amount": "25.00"}, "REST"))
    used = []
    r = investigator._tool(s, members, "get_paypal_dispute", {"dispute_id": "PP-R-TKA"}, used)
    assert r["via"] == "PayPal Agent Toolkit" and r["evidences"] == 2 and r["amount"] == "25.00"
    assert investigator._tool(s, members, "get_paypal_dispute", {"dispute_id": "PP-R-NOPE"}, used)["error"]
    assert investigator._tool(s, {"someone-else"}, "get_paypal_dispute", {"dispute_id": "PP-R-TKA"}, used)["error"]   # not this case
    o = investigator._tool(s, members, "get_paypal_order", {"order_id": "ORDERTKA"}, used)
    assert o["status"] == "COMPLETED" and o["via"] == "REST"
    assert used == ["get_paypal_dispute (PayPal Agent Toolkit)", "get_paypal_order (REST)"]


def test_toolkit_falls_back_to_rest_when_missing_or_failing(monkeypatch):
    from app import paypal, paypal_tools
    monkeypatch.setattr(paypal, "get_dispute", lambda d: {"status": "RESOLVED"})
    monkeypatch.setattr(paypal_tools, "_state", {"api": None, "tried": True})
    assert paypal_tools.get_dispute("PP-R-1") == ({"status": "RESOLVED"}, "REST")
    class Boom:
        def run(self, method, params): raise RuntimeError("boom")
    monkeypatch.setattr(paypal_tools, "_state", {"api": Boom(), "tried": True})
    assert paypal_tools.get_dispute("PP-R-1")[1] == "REST"
    class Ok:
        def run(self, method, params): return '{"status": "OPEN", "echo": "%s"}' % method
    monkeypatch.setattr(paypal_tools, "_state", {"api": Ok(), "tried": True})
    data, via = paypal_tools.get_dispute("PP-R-1")
    assert via == "PayPal Agent Toolkit" and data["echo"] == "get_dispute"
    assert paypal_tools.get_order("OID123456789012345")[0]["echo"] == "get_order_details"


def test_case_file_lists_tools_used_and_always_shows_linked_disputes(monkeypatch):
    from app import investigator, llm, paypal_tools
    s, members = _case_with_dispute("TKB")
    monkeypatch.setattr(paypal_tools, "get_dispute", lambda d: ({"status": "OPEN", "reason": "X", "dispute_amount": {"value": "25.00"}}, "PayPal Agent Toolkit"))
    entry = {"cluster_members": sorted(members), "cluster_size": 2, "shared_attribute_types": ["device_id"],
             "decision": "FLAGGED_FOR_HUMAN_REVIEW", "suspicion_score": 0.7, "time_synchronization_score": 1,
             "edges": [{"pair": sorted(members), "shared_attributes": ["device_id"]}]}
    answer = {"summary": "s", "confidence": "medium", "key_evidence": ["e"], "caveats": [],
              "recommendations": [{"action": "request_more_info", "account_ids": sorted(members), "reason": "r"}]}
    def fake_agent(system, user, tools, tool_fn, **kw):
        assert {"get_paypal_dispute", "get_paypal_order"} <= {t["name"] for t in tools}
        tool_fn("get_paypal_dispute", {"dispute_id": "PP-R-TKB"})
        import json as _j
        return _j.dumps(answer)
    monkeypatch.setattr(llm, "provider", lambda: "groq")
    monkeypatch.setattr(llm, "model_name", lambda: "fake-model")
    monkeypatch.setattr(llm, "run_agent", fake_agent)
    cf, who = investigator.investigate(s, entry)
    assert who == "fake-model" and cf.tools_used == ["get_paypal_dispute (PayPal Agent Toolkit)"]
    assert any("PayPal dispute" in line and "TKB1" in line for line in cf.key_evidence)
    monkeypatch.setattr(llm, "provider", lambda: "offline")                       # offline analyst shows the dispute too
    cf2, _ = investigator.investigate(s, entry)
    assert any("PayPal dispute" in line for line in cf2.key_evidence) and cf2.tools_used == []
