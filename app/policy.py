"""The screening policy, versioned. A stored decision records the policy it was made under, so it can be replayed
under the rules that produced it and compared with today's rules."""

POLICY = {
    "version": "2026-11-06.1",
    "min_score": 0.5,                          # a cluster must score at least this to hold a buyer
    "strong_attrs": ["device_id", "card_fp"],  # links that can hold a buyer; address and IP alone never do
    "spread_min_accounts": 3,                  # device-spread rule: one device used by this many accounts...
    "spread_min_addresses": 3,                 # ...shipping to this many different addresses
}


def current():
    return {**POLICY, "strong_attrs": list(POLICY["strong_attrs"])}


# Where each kind of evidence comes from and how far to trust it (shown next to every factor in an explanation).
PROVENANCE = {
    "device_id": {"label": "Shared device", "source": "Browser identifier supplied by our checkout page",
                  "reliability": "low to medium", "limit": "The browser can change it. Production needs real device fingerprinting."},
    "card_fp": {"label": "Shared card", "source": "Not available from PayPal; only if a merchant supplies it",
                "reliability": "high when present", "limit": "Not present in live PayPal data."},
    "shipping_address": {"label": "Shared shipping address", "source": "Address on the buyer's PayPal order",
                         "reliability": "medium", "limit": "Households share addresses innocently, and all sandbox buyers share a default one."},
    "ip": {"label": "Shared IP address", "source": "Seen by our server when the buyer returned from PayPal",
           "reliability": "low", "limit": "Shared networks and VPNs. Treated as corroboration only."},
    "timing": {"label": "Purchase timing", "source": "Authorization time on our server",
               "reliability": "medium", "limit": "Bursts happen innocently, such as sales and launches."},
    "size": {"label": "Cluster size", "source": "Count of linked accounts", "reliability": "derived", "limit": "More accounts raise the score slightly."},
}


def provenance_key(signal: str):
    s = signal.lower()
    if "device" in s: return "device_id"
    if "card" in s: return "card_fp"
    if "shipping" in s or "address" in s: return "shipping_address"
    if "ip" in s.split() or s.endswith(" ip"): return "ip"
    if "transaction time" in s: return "timing"
    if "cluster size" in s: return "size"
    return None
