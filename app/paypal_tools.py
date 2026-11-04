"""PayPal's own Agent Toolkit (its framework-free core, installed without the LangChain/CrewAI extras) for the AI analyst's
READ-ONLY lookups. If the toolkit is not installed, or a call fails, the same lookup goes through our own REST client and the
result says which path was used. Money-moving actions never go through here."""
import json
from . import paypal
from .config import settings

_state = {"api": None, "tried": False}


def _toolkit():
    if not _state["tried"]:
        _state["tried"] = True
        try:
            from paypal_agent_toolkit.shared.api import PayPalAPI
            from paypal_agent_toolkit.shared.configuration import Context
            if paypal.enabled():
                _state["api"] = PayPalAPI(settings.paypal_client_id, settings.paypal_secret, Context(sandbox=True))
        except Exception:
            _state["api"] = None
    return _state["api"]


def toolkit_installed() -> bool:
    return _toolkit() is not None


def _run(method, params, fallback):
    api = _toolkit()
    if api is not None:
        try:
            return json.loads(api.run(method, params)), "PayPal Agent Toolkit"
        except Exception:
            pass  # fall through to REST
    return fallback(), "REST"


def get_dispute(dispute_id):
    return _run("get_dispute", {"dispute_id": dispute_id}, lambda: paypal.get_dispute(dispute_id))


def get_order(order_id):
    return _run("get_order_details", {"order_id": order_id}, lambda: paypal.get_order(order_id))
