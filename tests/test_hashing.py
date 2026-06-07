from __future__ import annotations

from decimal import Decimal

from degreezeor.core.hashing import canonical_json, hash_payload, sha256_hex


def test_canonical_json_is_order_independent() -> None:
    a = {"b": 1, "a": 2, "c": [3, 2, 1]}
    b = {"a": 2, "c": [3, 2, 1], "b": 1}
    assert canonical_json(a) == canonical_json(b)


def test_decimal_normalization_is_stable() -> None:
    # 0.10 and 0.1 must hash identically; trailing-zero artifacts must not matter.
    assert hash_payload({"x": Decimal("0.10")}) == hash_payload({"x": Decimal("0.1")})


def test_sha256_is_deterministic() -> None:
    assert sha256_hex("hello") == sha256_hex(b"hello")
    assert len(sha256_hex("hello")) == 64


def test_different_content_differs() -> None:
    assert hash_payload({"x": 1}) != hash_payload({"x": 2})
