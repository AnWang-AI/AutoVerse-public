"""Unit tests — Redactor (docs/reference/events-and-storage.md §4.9, O7/O12)."""

from __future__ import annotations

from autoverse.infra.observ.redact import REDACTED, Redactor


def test_known_secret_value_masked_everywhere() -> None:
    r = Redactor(["my-literal-secret"])
    assert r.text("token=my-literal-secret done") == f"token={REDACTED} done"


def test_bearer_and_sk_patterns_masked_without_registration() -> None:
    r = Redactor()
    out = r.text("Authorization: Bearer abc123def456ghi")
    assert "abc123def456ghi" not in out
    assert REDACTED in out
    assert REDACTED in r.text("key sk-ABCDEF1234567890")


def test_nested_dict_redaction() -> None:
    r = Redactor(["topsecret"])
    data = {"a": {"b": ["x", "topsecret"]}, "c": "fine"}
    out = r.mapping(data)
    assert out["c"] == "fine"
    assert REDACTED in str(out["a"])
    assert "topsecret" not in str(out)


def test_non_string_values_passthrough() -> None:
    r = Redactor()
    assert r.value(42) == 42
    assert r.value(None) is None
    assert r.value(True) is True


def test_api_key_assignment_masked_keeps_label() -> None:
    r = Redactor()
    out = r.text("api_key=ABCDEFGHIJK")
    assert "ABCDEFGHIJK" not in out
    assert out.lower().startswith("api_key=")


def test_secret_named_key_masks_value_even_if_value_shape_is_benign() -> None:
    """A dict keyed by a secret-shaped name must have its value masked even when
    the value itself matches no token regex (docs/reference/events-and-storage.md §4.9)."""
    r = Redactor()
    out = r.mapping({"password": "plaintext", "user": "alice"})
    assert out["password"] == REDACTED
    assert out["user"] == "alice"  # non-secret keys untouched


def test_various_secret_key_names_masked() -> None:
    r = Redactor()
    out = r.mapping(
        {
            "api_key": "abc",
            "apiKey": "abc",
            "token": "abc",
            "secret": "abc",
            "authorization": "abc",
            "access_token": "abc",
            "safe": "keepme",
        }
    )
    for k in ("api_key", "apiKey", "token", "secret", "authorization", "access_token"):
        assert out[k] == REDACTED, k
    assert out["safe"] == "keepme"


def test_secret_named_key_masks_nested_value() -> None:
    r = Redactor()
    out = r.mapping({"creds": {"password": "p", "note": "ok"}})
    inner = out["creds"]
    assert isinstance(inner, dict)
    assert inner["password"] == REDACTED
    assert inner["note"] == "ok"
