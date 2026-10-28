"""Thin PayPal REST client (sandbox only). Orders v2, Payments v2 refunds, webhook verification."""
import time
import httpx
from .config import settings

BASE = settings.paypal_base
_tok = {"v": None, "exp": 0.0}


class PayPalError(Exception):
    pass


def enabled() -> bool:
    return bool(settings.paypal_client_id and settings.paypal_secret)


def _token() -> str:
    if _tok["v"] and time.time() < _tok["exp"] - 30:
        return _tok["v"]
    r = httpx.post(f"{BASE}/v1/oauth2/token", auth=(settings.paypal_client_id, settings.paypal_secret),
                   data={"grant_type": "client_credentials"}, timeout=20)
    if r.status_code >= 400:
        raise PayPalError(r.text)
    d = r.json()
    _tok.update(v=d["access_token"], exp=time.time() + int(d.get("expires_in", 300)))
    return _tok["v"]


def _call(method: str, path: str, body: dict | None = None, request_id: str | None = None) -> dict:
    headers = {"Authorization": f"Bearer {_token()}", "Content-Type": "application/json"}
    if request_id:
        headers["PayPal-Request-Id"] = request_id  # PayPal-side idempotency
    r = httpx.request(method, f"{BASE}{path}", json=body, headers=headers, timeout=30)
    if r.status_code >= 400:
        raise PayPalError(f"{r.status_code} {r.text}")
    return r.json() if r.content else {}


def create_order(amount: str, currency: str, return_url: str, cancel_url: str) -> dict:
    body = {"intent": "CAPTURE",
            "purchase_units": [{"amount": {"currency_code": currency, "value": amount}}],
            "payment_source": {"paypal": {"experience_context": {
                "return_url": return_url, "cancel_url": cancel_url,
                "shipping_preference": "GET_FROM_FILE", "user_action": "PAY_NOW"}}}}
    order = _call("POST", "/v2/checkout/orders", body)
    link = next((l["href"] for l in order.get("links", []) if l["rel"] in ("payer-action", "approve")), None)
    return {"id": order["id"], "approve_url": link}


def capture_order(order_id: str) -> dict:
    return _call("POST", f"/v2/checkout/orders/{order_id}/capture", {}, request_id=f"cap-{order_id}")


def refund_capture(capture_id: str, request_id: str) -> dict:
    return _call("POST", f"/v2/payments/captures/{capture_id}/refund", {}, request_id=request_id)


def get_dispute(dispute_id: str) -> dict:
    return _call("GET", f"/v1/customer/disputes/{dispute_id}")


def verify_webhook(headers: dict, event: dict) -> bool:
    if not (enabled() and settings.paypal_webhook_id):
        return False
    h = {k.lower(): v for k, v in headers.items()}
    body = {"auth_algo": h.get("paypal-auth-algo"), "cert_url": h.get("paypal-cert-url"),
            "transmission_id": h.get("paypal-transmission-id"), "transmission_sig": h.get("paypal-transmission-sig"),
            "transmission_time": h.get("paypal-transmission-time"), "webhook_id": settings.paypal_webhook_id,
            "webhook_event": event}
    return _call("POST", "/v1/notifications/verify-webhook-signature", body).get("verification_status") == "SUCCESS"
