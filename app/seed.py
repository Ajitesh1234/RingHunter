"""Deterministic demo data: a real fraud ring, a legit family (must NOT flag), office wifi (must NOT flag)."""
from datetime import datetime, timedelta
from .db import Account, Txn


def _iso(base, minutes):
    return (base + timedelta(minutes=minutes)).isoformat(timespec="seconds")


def seed(s):
    t0 = datetime(2026, 10, 20, 10, 0, 0)
    created_ring = datetime(2026, 10, 19, 22, 0, 0)
    # Ring: scripted batch signup, shared device/card chain, buying minutes apart
    ring = [("R1", "devA", "cardA", "addrA"), ("R2", "devA", "cardB", "addrB"),
            ("R3", "devC", "cardB", "addrB"), ("R4", "devC", "cardD", "addrD")]
    for i, (n, dev, card, addr) in enumerate(ring):
        s.merge(Account(id=n, email=f"{n.lower()}@ex.test", device_id=dev, card_fp=card, ip=f"203.0.113.{10 + i}",
                        shipping_address=addr, created_at=_iso(created_ring, i * 3)))
        s.merge(Txn(id=f"t_{n}", account_id=n, ts=_iso(t0, i * 4), amount=480.0 + i, capture_id=f"CAP_{n}"))
    # Family: shared card+address, different devices, hours apart, signed up years apart
    for i, n in enumerate(["F1", "F2", "F3"]):
        s.merge(Account(id=n, email=f"{n.lower()}@home.test", device_id=f"devF{i}", card_fp="cardFam", ip=f"198.51.100.{i}",
                        shipping_address="addrFam", created_at=f"202{i}-03-0{i + 1}T09:00:00"))
        s.merge(Txn(id=f"t_{n}", account_id=n, ts=_iso(t0, 120 * (i + 1)), amount=60.0, capture_id=f"CAP_{n}"))
    # Office wifi: same IP only
    for i, n in enumerate(["O1", "O2", "O3"]):
        s.merge(Account(id=n, email=f"{n.lower()}@corp.test", device_id=f"devO{i}", card_fp=f"cardO{i}", ip="192.0.2.50",
                        shipping_address=f"addrO{i}", created_at=f"2024-0{i + 1}-15T09:00:00"))
        s.merge(Txn(id=f"t_{n}", account_id=n, ts=_iso(t0, 300 + i), amount=25.0, capture_id=f"CAP_{n}"))
    s.commit()
