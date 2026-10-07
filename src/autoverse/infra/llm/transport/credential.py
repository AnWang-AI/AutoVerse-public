"""Composable credentials (docs/reference/models.md §3.4a).

Auth is modelled as a small pure function that transforms request headers.
A handful of composition operators (``all_of`` / ``or_else``) replace the old
``AuthKind`` enum hard-branches. Secret lookup is centralized in
``CredentialResolver`` (env → injected ``SecretProvider`` → empty), while
credential objects only receive ``AuthContext.secrets``. Everything here is
network-free and independently unit-testable.
"""

from __future__ import annotations

import base64
import email.utils
import hashlib
import hmac
from dataclasses import dataclass
from typing import Protocol

from autoverse.ports import SecretProvider


class CredentialResolver:
    """Resolve credential values: env → SecretProvider → empty string.

    A ``SecretProvider`` Port may be injected; vault, env-only, and test secret
    implementations stay behind that port instead of living in the resolver.
    """

    def __init__(self, secrets: SecretProvider | None = None) -> None:
        self._secrets = secrets

    def get(self, name: str) -> str:
        import os

        env_val = os.environ.get(name)
        if env_val:
            return env_val
        if self._secrets is not None:
            secret_val = self._secrets.get(name)
            if secret_val:
                return secret_val
        return ""


@dataclass(frozen=True)
class AuthContext:
    """Runtime facts an ``apply`` may need, assembled by Transport before send."""

    secrets: CredentialResolver
    body: bytes = b""
    account_id: str = ""


class Credential(Protocol):
    """Apply auth to a request's headers. Pure, composable, unit-testable.

    ``headers`` is the existing header map (possibly already mutated by a prior
    Credential); returns a new map (does not mutate in place).
    """

    def apply(
        self, headers: dict[str, str], ctx: AuthContext
    ) -> dict[str, str]: ...

    def is_satisfiable(self, resolver: "CredentialResolver") -> bool:
        """True iff every secret this credential needs resolves to a value.

        Network-free pre-flight: callers (e.g. the sub-agent tier resolver) use
        this to detect a missing key *before* a request 401s, and degrade
        gracefully instead. Composition mirrors auth — ``all_of`` needs every
        part, ``or_else`` needs either branch, ``none`` needs nothing.
        """
        ...


@dataclass(frozen=True)
class _Bearer:
    secret_name: str

    def apply(self, headers: dict[str, str], ctx: AuthContext) -> dict[str, str]:
        token = ctx.secrets.get(self.secret_name)
        out = dict(headers)
        if token:
            out["Authorization"] = f"Bearer {token}"
        return out

    def is_satisfiable(self, resolver: "CredentialResolver") -> bool:
        return bool(resolver.get(self.secret_name))


@dataclass(frozen=True)
class _Header:
    name: str
    secret_name: str | None = None
    value: str | None = None

    def apply(self, headers: dict[str, str], ctx: AuthContext) -> dict[str, str]:
        out = dict(headers)
        if self.secret_name is not None:
            resolved = ctx.secrets.get(self.secret_name)
            if resolved:
                out[self.name] = resolved
        elif self.value is not None:
            out[self.name] = self.value
        return out

    def is_satisfiable(self, resolver: "CredentialResolver") -> bool:
        # A static (secret-less) header carries no credential requirement.
        return True if self.secret_name is None else bool(resolver.get(self.secret_name))


@dataclass(frozen=True)
class _HmacSha1:
    app_id_name: str
    app_key_name: str
    source: str

    def apply(self, headers: dict[str, str], ctx: AuthContext) -> dict[str, str]:
        app_id = ctx.secrets.get(self.app_id_name)
        app_key = ctx.secrets.get(self.app_key_name)
        out = dict(headers)
        if not (app_id and app_key):
            return out
        date = email.utils.formatdate(usegmt=True)
        sign_str = f"date: {date}\nsource: {self.source}"
        sig = base64.b64encode(
            hmac.new(app_key.encode(), sign_str.encode(), hashlib.sha1).digest()
        ).decode()
        out["Authorization"] = (
            f'hmac id="{app_id}", algorithm="hmac-sha1", '
            f'headers="date source", signature="{sig}"'
        )
        out["Date"] = date
        out["Source"] = self.source
        out["Content-Type"] = "application/json"
        return out

    def is_satisfiable(self, resolver: "CredentialResolver") -> bool:
        return bool(resolver.get(self.app_id_name) and resolver.get(self.app_key_name))


@dataclass(frozen=True)
class _AppKey:
    app_id_name: str
    app_key_name: str

    def apply(self, headers: dict[str, str], ctx: AuthContext) -> dict[str, str]:
        app_id = ctx.secrets.get(self.app_id_name)
        app_key = ctx.secrets.get(self.app_key_name)
        out = dict(headers)
        if not (app_id and app_key):
            return out
        token = f"{app_id}:{app_key}"
        if ctx.account_id:
            token += f"&account_id={ctx.account_id}"
        out["Authorization"] = f"Bearer {token}"
        return out

    def is_satisfiable(self, resolver: "CredentialResolver") -> bool:
        return bool(resolver.get(self.app_id_name) and resolver.get(self.app_key_name))


@dataclass(frozen=True)
class _AllOf:
    creds: tuple[Credential, ...]

    def apply(self, headers: dict[str, str], ctx: AuthContext) -> dict[str, str]:
        out = dict(headers)
        for cred in self.creds:
            out = cred.apply(out, ctx)
        return out

    def is_satisfiable(self, resolver: "CredentialResolver") -> bool:
        return all(c.is_satisfiable(resolver) for c in self.creds)


@dataclass(frozen=True)
class _OrElse:
    primary: Credential
    fallback: Credential

    def apply(self, headers: dict[str, str], ctx: AuthContext) -> dict[str, str]:
        primary_out = self.primary.apply(headers, ctx)
        # Success = primary actually produced an auth credential, i.e. it wrote a
        # non-empty ``Authorization`` header (all real auth credentials —
        # bearer/hmac/appkey — write it). Comparing the whole header map is
        # fragile: a primary that always injects a static side header (e.g.
        # ``x-api-version``) would look "changed" even with an empty credential.
        primary_auth = primary_out.get("Authorization")
        if primary_auth and primary_auth != headers.get("Authorization"):
            return primary_out
        return self.fallback.apply(headers, ctx)

    def is_satisfiable(self, resolver: "CredentialResolver") -> bool:
        return self.primary.is_satisfiable(resolver) or self.fallback.is_satisfiable(
            resolver
        )


@dataclass(frozen=True)
class _Noop:
    def apply(self, headers: dict[str, str], ctx: AuthContext) -> dict[str, str]:
        return dict(headers)

    def is_satisfiable(self, resolver: "CredentialResolver") -> bool:
        return True  # local/mock endpoint needs no credential


def bearer(secret_name: str) -> Credential:
    return _Bearer(secret_name)


def header(
    name: str, secret_name: str | None = None, value: str | None = None
) -> Credential:
    return _Header(name, secret_name, value)


def hmac_sha1(app_id_name: str, app_key_name: str, source: str) -> Credential:
    return _HmacSha1(app_id_name, app_key_name, source)


def appkey(app_id_name: str, app_key_name: str) -> Credential:
    return _AppKey(app_id_name, app_key_name)


def all_of(*creds: Credential) -> Credential:
    return _AllOf(tuple(creds))


def or_else(primary: Credential, fallback: Credential) -> Credential:
    return _OrElse(primary, fallback)


def none() -> Credential:
    """No-op credential for local/mock endpoints."""
    return _Noop()


__all__ = [
    "CredentialResolver",
    "AuthContext",
    "Credential",
    "bearer",
    "header",
    "hmac_sha1",
    "appkey",
    "all_of",
    "or_else",
    "none",
]
