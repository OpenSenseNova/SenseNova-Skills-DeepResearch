"""Authentication helpers for namespace-scoped Search Coordinator access."""

from __future__ import annotations

import hashlib
import hmac


def derive_namespace_token(root_token: str, namespace: str) -> str:
    """Derive a credential that is valid for exactly one attempt namespace."""
    if not root_token:
        raise ValueError("root_token must not be empty")
    if not namespace or len(namespace) > 300:
        raise ValueError("invalid search namespace")
    return hmac.new(
        root_token.encode("utf-8"),
        ("deepresearch-search-namespace-v1\0" + namespace).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
