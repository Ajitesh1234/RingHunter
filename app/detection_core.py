"""
detection_core.py
The actual detection logic, refactored to be storage-agnostic: it takes plain
dicts/lists in and returns plain dicts/lists out. This is what main.py (the
API) calls, and it's the same algorithm validated in the original prototype
(same fix for the IP-bridging bug, same three-tier decision, same IP cap).

EDGE PRUNING (added after a real bug was found): earlier versions connected
two accounts into the same cluster if they shared ANY hard attribute at all,
regardless of how far apart in time that specific pair transacted. This let
a single weak, temporally-distant edge (e.g. a decoy account sharing only a
card number with one ring member, 9 hours apart) drag an unrelated account
into a strong cluster and inherit its high score. Edges are now scored
individually - by that pair's own attribute overlap AND that pair's own
time gap, not the whole cluster's aggregate - and weak edges are pruned
before clustering even happens. See MIN_EDGE_STRENGTH below.
"""
from datetime import datetime
from collections import defaultdict

import networkx as nx

HARD_ATTRS = ["device_id", "card_fp", "shipping_address"]
ALL_ATTRS = HARD_ATTRS + ["ip"]

# Per-attribute weights: shared device is harder to explain innocently than
# shared shipping address, so it should count for more (kept in sync with
# the prototype's detect_rings.py after peer-review feedback).
ATTR_WEIGHTS = {"device_id": 0.40, "card_fp": 0.30, "shipping_address": 0.15}
MAX_HARD_WEIGHT = sum(ATTR_WEIGHTS.values())  # 0.85

AUTO_ACTION_THRESHOLD = 0.80
HUMAN_REVIEW_THRESHOLD = 0.50

# Per-EDGE time curve: deliberately much stricter than the per-CLUSTER curve
# further down. An edge is only trustworthy if THIS SPECIFIC PAIR transacted
# close together - full trust within 10 minutes, none left by 4 hours. This
# is what lets a decoy 9 hours away from its only connection get pruned,
# while genuine ring members transacting minutes apart stay connected.
EDGE_SYNC_FULL_MINUTES = 10
EDGE_SYNC_ZERO_MINUTES = 240
MIN_EDGE_STRENGTH = 0.35  # edges scoring below this are dropped before clustering


def _edge_time_sync(gap_minutes):
    if gap_minutes <= EDGE_SYNC_FULL_MINUTES:
        return 1.0
    if gap_minutes >= EDGE_SYNC_ZERO_MINUTES:
        return 0.0
    return 1.0 - (gap_minutes - EDGE_SYNC_FULL_MINUTES) / (EDGE_SYNC_ZERO_MINUTES - EDGE_SYNC_FULL_MINUTES)


def _min_pair_gap_minutes(acc_a, acc_b, transactions):
    """Smallest time gap, in minutes, between any transaction of A and any of B."""
    times_a = [datetime.fromisoformat(t["timestamp"]) for t in transactions.get(acc_a, [])]
    times_b = [datetime.fromisoformat(t["timestamp"]) for t in transactions.get(acc_b, [])]
    if not times_a or not times_b:
        return EDGE_SYNC_ZERO_MINUTES  # no data to corroborate - treat as maximally distant
    return min(abs((ta - tb).total_seconds()) / 60.0 for ta in times_a for tb in times_b)


def build_graph(accounts: dict, transactions: dict):
    """accounts: {account_id: {device_id, card_fp, ip, shipping_address}}
    transactions: {account_id: [{timestamp, amount}, ...]}

    Cluster membership uses HARD attributes only, AND each candidate edge must
    individually clear MIN_EDGE_STRENGTH (its own attribute weight + its own
    pair's time proximity) before it's added to the graph. IP never forms
    edges at all - it only corroborates a score for a cluster that already
    exists via hard attributes.
    """
    G = nx.Graph()
    for acc_id in accounts:
        G.add_node(acc_id)

    attr_index = {attr: defaultdict(list) for attr in ALL_ATTRS}
    for acc_id, acc in accounts.items():
        for attr in ALL_ATTRS:
            attr_index[attr][acc[attr]].append(acc_id)

    # Collect ALL attribute types shared by each candidate pair first, so a pair
    # sharing 2+ attributes (e.g. card+address) gets scored on their combined
    # weight, not evaluated as two separate single-attribute candidate edges.
    candidate_pairs = defaultdict(set)
    for attr in HARD_ATTRS:
        for value, acc_list in attr_index[attr].items():
            if len(acc_list) < 2:
                continue
            for i in range(len(acc_list)):
                for j in range(i + 1, len(acc_list)):
                    a, b = sorted([acc_list[i], acc_list[j]])
                    candidate_pairs[(a, b)].add(attr)

    edge_reasons = defaultdict(set)
    edge_weights = {}
    edge_syncs = {}
    for (a, b), attrs in candidate_pairs.items():
        gap = _min_pair_gap_minutes(a, b, transactions)
        attr_component = sum(ATTR_WEIGHTS[x] for x in attrs) / MAX_HARD_WEIGHT
        sync = _edge_time_sync(gap)
        weight = round(0.5625 * attr_component + 0.4375 * sync, 3)  # renormalized 0.45/0.35 split
        if weight >= MIN_EDGE_STRENGTH:
            edge_reasons[(a, b)] = attrs
            edge_weights[(a, b)] = weight
            edge_syncs[(a, b)] = sync
            G.add_edge(a, b)
        # else: edge pruned - too weak, too distant in time, or both. Not added to graph at all.

    ip_pairs = defaultdict(set)
    for value, acc_list in attr_index["ip"].items():
        if len(acc_list) < 2:
            continue
        for i in range(len(acc_list)):
            for j in range(i + 1, len(acc_list)):
                a, b = sorted([acc_list[i], acc_list[j]])
                ip_pairs[(a, b)].add("ip")

    return G, edge_reasons, edge_weights, edge_syncs, ip_pairs, attr_index["ip"]


def cluster_time_sync_score(members, edge_syncs):
    """
    Cluster-level time-sync score, computed as the average of the ACTUAL
    retained edges' own per-pair sync values - not a separate, more lenient
    whole-cluster-span calculation.

    This replaces an earlier version that computed sync independently from
    the cluster's overall min/max transaction timestamps using a lenient
    1hr-full/48hr-zero curve. That was architecturally inconsistent: edges
    are pruned using a strict 10min-full/240min-zero curve (see
    _edge_time_sync), but the cluster's OVERALL score then ignored that and
    used the lenient curve instead - so a cluster held together by two weak,
    already-marginal edges (e.g. 0.13 and 0.65 sync) could still get
    reported as 0.915 "time-synchronized" just because the cluster's outer
    span happened to fall in a forgiving part of the lenient curve. That
    produced a real false positive on a family cluster (09:00/12:30/14:00
    transactions, real edges were weak, but cluster-level sync came out to
    0.915 anyway). Now the cluster's sync can never be more confident than
    the edges that actually hold it together.
    """
    members = sorted(members)
    syncs = []
    for i in range(len(members)):
        for j in range(i + 1, len(members)):
            pair = (members[i], members[j])
            if pair in edge_syncs:
                syncs.append(edge_syncs[pair])
    if not syncs:
        return 0.0
    return sum(syncs) / len(syncs)


def distinct_shared_attrs(members, edge_reasons):
    attrs_seen = set()
    for i in range(len(members)):
        for j in range(i + 1, len(members)):
            a, b = sorted([members[i], members[j]])
            attrs_seen |= edge_reasons.get((a, b), set())
    return attrs_seen


def score_cluster(members, edge_reasons, edge_syncs, accounts=None, ip_shares_within=0):
    """
    accounts: optional {account_id: {..., account_created_at: ISO str}}. When
    account_created_at is present for all members, an additional signal is
    used: how tightly clustered their ACCOUNT CREATION times are, not just
    their transaction times. This is a real, standard fraud-ops signal - a
    ring created in a single batch minutes apart, then transacting together,
    is categorically different from a genuine household whose members'
    accounts were opened independently over months or years and who simply
    happen to shop together once. Two clusters can look IDENTICAL on
    transaction evidence alone and still be told apart by this signal.
    When account_created_at is missing (older data, or a real merchant feed
    that doesn't provide it), this signal is skipped entirely and weight
    reverts to the original distribution - nothing here can make behavior
    WORSE on data that doesn't have the field, only better on data that does.
    """
    shared_attrs = distinct_shared_attrs(members, edge_reasons)
    size = len(members)
    sync = cluster_time_sync_score(members, edge_syncs)

    if not shared_attrs:
        return 0.0, shared_attrs, sync, [], None

    size_component = min(1.0, (size - 2) / 6.0)
    n_pairs = size * (size - 1) / 2 if size > 1 else 1
    ip_corroboration = min(0.1, 0.1 * (ip_shares_within / n_pairs))

    creation_sync = _cluster_creation_sync_score(members, accounts) if accounts else None
    have_creation_signal = creation_sync is not None

    # Weight redistribution: WITH a creation-time signal, it earns real
    # weight (0.20) taken proportionally from the other three components,
    # since it is genuinely independent evidence. WITHOUT it, weights are
    # exactly the original 0.45 / 0.35 / 0.20 split - fully backward compatible.
    if have_creation_signal:
        w_attr, w_sync, w_creation, w_size = 0.35, 0.28, 0.20, 0.17
    else:
        w_attr, w_sync, w_creation, w_size = 0.45, 0.35, 0.0, 0.20

    breakdown = []
    for attr in HARD_ATTRS:
        if attr in shared_attrs:
            contribution = round(w_attr * (ATTR_WEIGHTS[attr] / MAX_HARD_WEIGHT), 3)
            breakdown.append({"signal": f"shared {attr}", "contribution": contribution})
    breakdown.append({"signal": "transaction time-synchronization", "contribution": round(w_sync * sync, 3)})
    if have_creation_signal:
        breakdown.append({"signal": "account-creation-time synchronization", "contribution": round(w_creation * creation_sync, 3)})
    breakdown.append({"signal": "cluster size", "contribution": round(w_size * size_component, 3)})
    if ip_corroboration > 0:
        breakdown.append({"signal": "corroborating shared IP", "contribution": round(ip_corroboration, 3)})

    score = round(min(sum(b["contribution"] for b in breakdown), 1.0), 3)
    return score, shared_attrs, sync, breakdown, creation_sync


ACCOUNT_CREATION_SYNC_FULL_MINUTES = 60     # accounts opened within an hour of each other: full suspicion
ACCOUNT_CREATION_SYNC_ZERO_MINUTES = 43200  # 30 days apart or more: zero suspicion from this signal


def _cluster_creation_sync_score(members, accounts):
    """Average pairwise closeness of account_created_at across the cluster's
    own members (mirrors the transaction time-sync design: derived from
    actual pairwise gaps, not a whole-cluster span, for the same consistency
    reason the transaction sync bug was fixed in an earlier round)."""
    times = []
    for m in members:
        raw = accounts.get(m, {}).get("account_created_at")
        if not raw:
            return None  # any member missing the field disables the signal for this cluster entirely
        times.append(datetime.fromisoformat(raw))
    if len(times) < 2:
        return None
    gaps = []
    for i in range(len(times)):
        for j in range(i + 1, len(times)):
            gap_min = abs((times[i] - times[j]).total_seconds()) / 60.0
            gaps.append(_creation_time_sync(gap_min))
    return sum(gaps) / len(gaps)


def _creation_time_sync(gap_minutes):
    if gap_minutes <= ACCOUNT_CREATION_SYNC_FULL_MINUTES:
        return 1.0
    if gap_minutes >= ACCOUNT_CREATION_SYNC_ZERO_MINUTES:
        return 0.0
    span = ACCOUNT_CREATION_SYNC_ZERO_MINUTES - ACCOUNT_CREATION_SYNC_FULL_MINUTES
    return 1.0 - (gap_minutes - ACCOUNT_CREATION_SYNC_FULL_MINUTES) / span


def decide(score):
    if score >= AUTO_ACTION_THRESHOLD:
        return "FLAGGED_FOR_AUTO_ACTION"
    if score >= HUMAN_REVIEW_THRESHOLD:
        return "FLAGGED_FOR_HUMAN_REVIEW"
    return "NOT_FLAGGED"


def run_detection(accounts: dict, transactions: dict):
    """
    accounts: {account_id: {device_id, card_fp, ip, shipping_address}}
    transactions: {account_id: [{timestamp, amount}, ...]}
    Returns: (audit_trail: list[dict], lookup_index: {account_id: entry})
    """
    G, edge_reasons, edge_weights, edge_syncs, ip_pairs, ip_index = build_graph(accounts, transactions)
    hard_components = [c for c in nx.connected_components(G) if len(c) >= 2]
    accounted_for = set()
    for c in hard_components:
        accounted_for |= c

    ip_only_components = []
    for value, acc_list in ip_index.items():
        remaining = [a for a in acc_list if a not in accounted_for]
        if len(remaining) >= 2:
            ip_only_components.append(set(remaining))
            accounted_for |= set(remaining)

    audit_trail = []
    lookup_index = {}

    for comp in hard_components:
        members = sorted(comp)
        n_ip_shares = sum(
            1 for i in range(len(members)) for j in range(i + 1, len(members))
            if "ip" in ip_pairs.get(tuple(sorted([members[i], members[j]])), set())
        )
        score, shared_attrs, sync, breakdown, creation_sync = score_cluster(
            members, edge_reasons, edge_syncs, accounts=accounts, ip_shares_within=n_ip_shares
        )
        decision = decide(score)

        # Build the actual edge list for this cluster - this is what fixes the
        # misleading-attribution bug. A previous version reported
        # "shared_attribute_types" as a flat union across every pair in the
        # cluster, which read as if all members shared all those attributes.
        # For a chain-linked ring (A-B share device, B-C share card, ...) that
        # implied evidence that doesn't exist. Now every pairwise edge that
        # actually survived pruning is listed individually and explicitly.
        edges = []
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                a, b = sorted([members[i], members[j]])
                if (a, b) in edge_reasons:
                    edges.append({
                        "pair": [a, b],
                        "shared_attributes": sorted(edge_reasons[(a, b)]),
                        "edge_weight": edge_weights.get((a, b)),
                    })
        edges.sort(key=lambda e: -(e["edge_weight"] or 0))
        is_fully_connected = len(edges) == len(members) * (len(members) - 1) / 2
        chain_note = (
            "Every member shares evidence directly with every other member."
            if is_fully_connected else
            "This is a CHAIN structure: no single attribute is shared by all members. "
            "See the edges list for the exact pairwise evidence connecting each pair."
        )

        if decision == "NOT_FLAGGED":
            explanation = (
                f"NOT FLAGGED. {len(members)} accounts share {', '.join(sorted(shared_attrs))} "
                f"(see edges for exact pairs), but suspicion score {score:.2f} is below the "
                f"human-review threshold ({HUMAN_REVIEW_THRESHOLD}). Insufficient corroborating "
                f"evidence: {'no ' if sync < 0.3 else 'some '}transaction time-clustering "
                f"(sync={sync:.2f}), only {len(shared_attrs)} of 3 hard identity signals appear "
                f"anywhere in this cluster. Consistent with an innocent explanation "
                f"(e.g. shared payment method within a household)."
            )
        else:
            explanation = (
                f"{decision.replace('_', ' ')}. {len(members)} accounts connected; attribute types "
                f"appearing across the cluster's edges: {', '.join(sorted(shared_attrs))}. {chain_note} "
                f"Time-sync score {sync:.2f}. Suspicion score {score:.2f} "
                f"(auto-action >= {AUTO_ACTION_THRESHOLD}, human review >= {HUMAN_REVIEW_THRESHOLD})."
            )

        entry = {
            "cluster_members": members,
            "cluster_size": len(members),
            "shared_attribute_types": sorted(shared_attrs),
            "edges": edges,
            "is_fully_connected": is_fully_connected,
            "time_synchronization_score": round(sync, 3),
            "account_creation_sync_score": round(creation_sync, 3) if creation_sync is not None else None,
            "suspicion_score": score,
            "score_breakdown": breakdown,
            "decision": decision,
            "explanation": explanation,
        }
        audit_trail.append(entry)
        for m in members:
            lookup_index[m] = entry

    for comp in ip_only_components:
        members = sorted(comp)
        score = 0.1
        entry = {
            "cluster_members": members,
            "cluster_size": len(members),
            "shared_attribute_types": ["ip"],
            "time_synchronization_score": None,
            "suspicion_score": score,
            "decision": "NOT_FLAGGED",
            "explanation": (
                f"{len(members)} accounts share only an IP address (e.g. office/campus wifi) "
                f"with no other overlapping identity attribute. Capped at {score} regardless of size."
            ),
        }
        audit_trail.append(entry)
        for m in members:
            lookup_index[m] = entry

    for acc_id in accounts:
        if acc_id not in lookup_index:
            entry = {
                "cluster_members": [acc_id], "cluster_size": 1, "shared_attribute_types": [],
                "time_synchronization_score": 0.0, "suspicion_score": 0.0, "decision": "NOT_FLAGGED",
                "explanation": "No shared attributes with any other account.",
            }
            audit_trail.append(entry)
            lookup_index[acc_id] = entry

    _annotate_network_bridges(audit_trail, transactions, accounts)

    audit_trail.sort(key=lambda x: -x["suspicion_score"])
    return audit_trail, lookup_index


NETWORK_BRIDGE_WINDOW_MINUTES = 15


def _annotate_network_bridges(audit_trail, transactions, accounts=None):
    """
    Reporting-only pass, run AFTER all scoring/decisions are final. Does not
    change any score or decision - it only adds a `network_bridge` note to
    clusters that were flagged independently but appear to be part of the
    same broader fraud network.

    Two independent trigger conditions, either is sufficient:
      1. Transaction proximity (original): transactions within 15 minutes.
      2. Account-creation proximity (new, safer): both clusters' accounts
         were opened within a tight batch window (60 min), even if their
         TRANSACTIONS happened days apart. This is a stronger signal for
         bridging separated sub-rings than transaction timing alone,
         because a legitimate high-traffic period (e.g. a flash sale) can
         make many unrelated customers transact within the same 15 minutes,
         but it's far less likely that many unrelated customers all opened
         their accounts within the same hour AND independently ended up
         forming separate hard-attribute-evidenced suspicious clusters.

    Both conditions are deliberately restricted to pairs where BOTH clusters
    already earned a flagged status on their own hard evidence. This never
    fires against a NOT_FLAGGED cluster, so it cannot turn an office-WiFi
    group or a legitimate family into part of a "network" - those clusters
    never reach flagged status in the first place, so they're never eligible.
    """
    flagged = [e for e in audit_trail if e["decision"] != "NOT_FLAGGED" and e["cluster_size"] >= 2]
    for e in flagged:
        e["network_bridge"] = None

    for i in range(len(flagged)):
        for j in range(i + 1, len(flagged)):
            a, b = flagged[i], flagged[j]
            txn_gap = _min_cluster_gap_minutes(a["cluster_members"], b["cluster_members"], transactions)
            creation_gap = (
                _min_cluster_creation_gap_minutes(a["cluster_members"], b["cluster_members"], accounts)
                if accounts else None
            )

            reason = None
            gap_for_ranking = None
            if txn_gap is not None and txn_gap <= NETWORK_BRIDGE_WINDOW_MINUTES:
                reason = f"transactions occurred only {txn_gap:.1f} minutes apart"
                gap_for_ranking = txn_gap
            if creation_gap is not None and creation_gap <= ACCOUNT_CREATION_BRIDGE_WINDOW_MINUTES:
                creation_reason = f"both clusters' accounts were created within {creation_gap:.1f} minutes of each other"
                # prefer the creation-time reason when both fire - it's the harder-to-fake signal
                reason = creation_reason
                gap_for_ranking = creation_gap if gap_for_ranking is None else min(gap_for_ranking, creation_gap)

            if reason is None:
                continue

            note_a = {
                "linked_cluster_members": b["cluster_members"],
                "gap_minutes": round(gap_for_ranking, 1),
                "note": (
                    f"No shared identity attribute with this cluster, but {reason}. "
                    f"Likely the same broader fraud network - recommend investigating both clusters together."
                ),
            }
            note_b = dict(note_a)
            note_b["linked_cluster_members"] = a["cluster_members"]
            if a["network_bridge"] is None or gap_for_ranking < a["network_bridge"]["gap_minutes"]:
                a["network_bridge"] = note_a
            if b["network_bridge"] is None or gap_for_ranking < b["network_bridge"]["gap_minutes"]:
                b["network_bridge"] = note_b


def _min_cluster_gap_minutes(members_a, members_b, transactions):
    times_a = [datetime.fromisoformat(t["timestamp"]) for m in members_a for t in transactions.get(m, [])]
    times_b = [datetime.fromisoformat(t["timestamp"]) for m in members_b for t in transactions.get(m, [])]
    if not times_a or not times_b:
        return None
    return min(abs((ta - tb).total_seconds()) / 60.0 for ta in times_a for tb in times_b)


ACCOUNT_CREATION_BRIDGE_WINDOW_MINUTES = 60


def _min_cluster_creation_gap_minutes(members_a, members_b, accounts):
    times_a = []
    for m in members_a:
        raw = accounts.get(m, {}).get("account_created_at")
        if raw:
            times_a.append(datetime.fromisoformat(raw))
    times_b = []
    for m in members_b:
        raw = accounts.get(m, {}).get("account_created_at")
        if raw:
            times_b.append(datetime.fromisoformat(raw))
    if not times_a or not times_b:
        return None
    return min(abs((ta - tb).total_seconds()) / 60.0 for ta in times_a for tb in times_b)
