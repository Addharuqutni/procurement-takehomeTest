"""Procurement request state machine. Pure functions: no DB, no FastAPI."""

# action -> (allowed source statuses, target status, roles allowed)
TRANSITIONS: dict[str, tuple[frozenset[str], str, frozenset[str]]] = {
    "submit":  (frozenset({"draft"}), "submitted", frozenset({"requester", "admin"})),
    "verify":  (frozenset({"submitted"}), "verified", frozenset({"procurement", "admin"})),
    "revise":  (frozenset({"submitted", "verified"}), "draft", frozenset({"procurement", "admin"})),
    "approve": (frozenset({"verified"}), "approved", frozenset({"manager", "admin"})),
    "reject":  (frozenset({"verified"}), "rejected", frozenset({"manager", "admin"})),
    "source":  (frozenset({"approved"}), "sourcing", frozenset({"procurement", "admin"})),
    "issue_po": (frozenset({"sourcing"}), "po_issued", frozenset({"procurement", "admin"})),
    "receive": (frozenset({"po_issued"}), "received", frozenset({"procurement", "admin"})),
    "close":   (frozenset({"received"}), "closed", frozenset({"procurement", "admin"})),
    "cancel":  (
        frozenset({"draft", "submitted", "verified", "approved", "sourcing"}),
        "cancelled",
        frozenset({"requester", "procurement", "admin"}),
    ),
}

# Only the requester who owns a draft may submit it.
OWNER_ONLY = frozenset({"submit"})

STATUS_LABEL = {
    "draft": "Draft",
    "submitted": "Menunggu verifikasi",
    "verified": "Menunggu approval",
    "approved": "Disetujui",
    "rejected": "Ditolak",
    "sourcing": "Sourcing vendor",
    "po_issued": "PO diterbitkan",
    "received": "Barang diterima",
    "closed": "Selesai",
    "cancelled": "Dibatalkan",
}


def allowed(action: str, role: str, status: str, *, is_owner: bool) -> bool:
    """Can this user perform `action` on a request currently in `status`?"""
    spec = TRANSITIONS.get(action)
    if spec is None:
        return False
    sources, _target, roles = spec
    if status not in sources or role not in roles:
        return False
    return not (action in OWNER_ONLY and not is_owner)
