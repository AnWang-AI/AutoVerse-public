"""Credential factories + composition operators (network-free, §3.4a)."""

from __future__ import annotations

import os

from autoverse.infra.llm.transport.credential import (
    AuthContext,
    CredentialResolver,
    all_of,
    appkey,
    bearer,
    header,
    hmac_sha1,
    none,
    or_else,
)


class DictSecrets:
    def __init__(self, data: dict[str, str]) -> None:
        self._data = data

    def get(self, key: str) -> str | None:
        return self._data.get(key)

    def require(self, key: str) -> str:
        return self._data[key]


def _ctx(data: dict[str, str], **kw: object) -> AuthContext:
    return AuthContext(secrets=CredentialResolver(DictSecrets(data)), **kw)  # type: ignore[arg-type]


def test_resolver_env_precedes_secrets(monkeypatch) -> None:
    monkeypatch.setenv("K", "from_env")
    r = CredentialResolver(DictSecrets({"K": "from_secret"}))
    assert r.get("K") == "from_env"


def test_resolver_falls_back_to_secrets(monkeypatch) -> None:
    monkeypatch.delenv("K", raising=False)
    r = CredentialResolver(DictSecrets({"K": "from_secret"}))
    assert r.get("K") == "from_secret"


def test_resolver_empty_when_missing(monkeypatch) -> None:
    monkeypatch.delenv("MISSING", raising=False)
    assert CredentialResolver().get("MISSING") == ""


def test_bearer() -> None:
    cred = bearer("TOK")
    out = cred.apply({}, _ctx({"TOK": "abc"}))
    assert out == {"Authorization": "Bearer abc"}


def test_bearer_missing_skips() -> None:
    cred = bearer("TOK")
    assert cred.apply({}, _ctx({})) == {}


def test_header_static_value() -> None:
    cred = header("x-api-version", value="2024-10")
    assert cred.apply({}, _ctx({})) == {"x-api-version": "2024-10"}


def test_header_from_secret() -> None:
    cred = header("x-account", secret_name="ACC")
    assert cred.apply({}, _ctx({"ACC": "node7"})) == {"x-account": "node7"}


def test_all_of_stacks_headers() -> None:
    cred = all_of(
        bearer("TOK"),
        header("x-api-version", value="v1"),
        header("x-account", secret_name="ACC"),
    )
    out = cred.apply({}, _ctx({"TOK": "abc", "ACC": "node7"}))
    assert out == {
        "Authorization": "Bearer abc",
        "x-api-version": "v1",
        "x-account": "node7",
    }


def test_or_else_falls_back() -> None:
    cred = or_else(bearer("PRIMARY"), bearer("SECONDARY"))
    out = cred.apply({}, _ctx({"SECONDARY": "fb"}))
    assert out == {"Authorization": "Bearer fb"}


def test_or_else_prefers_primary() -> None:
    cred = or_else(bearer("PRIMARY"), bearer("SECONDARY"))
    out = cred.apply({}, _ctx({"PRIMARY": "p", "SECONDARY": "fb"}))
    assert out == {"Authorization": "Bearer p"}


def test_or_else_falls_back_when_primary_only_changes_other_header() -> None:
    """Primary auth value empty but primary still mutates a non-auth header
    (e.g. a static x-api-version): or_else must NOT treat that as success and
    must fall back to the secondary credential."""
    primary = all_of(bearer("PRIMARY"), header("x-api-version", value="v1"))
    cred = or_else(primary, bearer("SECONDARY"))
    out = cred.apply({}, _ctx({"SECONDARY": "fb"}))
    assert out["Authorization"] == "Bearer fb"


def test_hmac_sha1_signs() -> None:
    cred = hmac_sha1("APP_ID", "APP_KEY", "myapp")
    out = cred.apply({}, _ctx({"APP_ID": "id1", "APP_KEY": "key1"}))
    assert out["Authorization"].startswith('hmac id="id1"')
    assert "signature=" in out["Authorization"]
    assert out["Source"] == "myapp"
    assert "Date" in out


def test_appkey_inlines_and_pins_node() -> None:
    cred = appkey("APP_ID", "APP_KEY")
    out = cred.apply({}, _ctx({"APP_ID": "id1", "APP_KEY": "key1"}, account_id="n3"))
    assert out["Authorization"] == "Bearer id1:key1&account_id=n3"


def test_none_noop() -> None:
    assert none().apply({"a": "b"}, _ctx({})) == {"a": "b"}
