"""Sandbox feasibility check for authorize / void / capture. Does not touch the database or the running app.

  python -m app.auth_check create                 -> makes an AUTHORIZE order, prints the buyer approval link
  python -m app.auth_check authorize ORDER_ID     -> after the buyer approves: places the authorization hold
  python -m app.auth_check void AUTH_ID           -> releases the hold (no money taken)
  python -m app.auth_check capture AUTH_ID        -> takes the money
"""
import sys
from . import paypal


def main(argv):
    if len(argv) < 2 or argv[1] not in ("create", "authorize", "void", "capture"):
        raise SystemExit(__doc__)
    if not paypal.enabled():
        raise SystemExit("No PayPal sandbox keys loaded. Check .env")
    cmd = argv[1]
    try:
        if cmd == "create":
            o = paypal.create_order("25.00", "USD", "https://example.com/done", "https://example.com/cancel", intent="AUTHORIZE")
            print("ORDER_ID:", o["id"])
            print("Open this in a private window, log in as a sandbox PERSONAL buyer, approve:")
            print(o["approve_url"])
        elif cmd == "authorize":
            r = paypal.authorize_order(argv[2])
            a = r["purchase_units"][0]["payments"]["authorizations"][0]
            print("order status:", r.get("status"))
            print("AUTH_ID:", a["id"], "| status:", a["status"], "| amount:", a["amount"]["value"], a["amount"]["currency_code"])
        elif cmd == "void":
            paypal.void_authorization(argv[2])
            print("voided OK (no money was taken)")
        else:
            r = paypal.capture_authorization(argv[2])
            print("capture id:", r.get("id"), "| status:", r.get("status"))
    except paypal.PayPalError as e:
        print("PayPal said:", str(e)[:600])
    except IndexError:
        raise SystemExit("Missing the ID argument.")


main(sys.argv)
