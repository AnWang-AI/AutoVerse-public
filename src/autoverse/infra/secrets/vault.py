"""Machine-bound AES-256-GCM secret vault (docs/reference/config.md §2.6).

Key derivation (every start)::

    stable_machine_id  ":"  user   ──PBKDF2-HMAC-SHA256(salt, 200_000, 32)──►  master_key

The master key lives only in memory, never on disk. Secrets are JSON-encoded,
encrypted with ``AESGCM`` (fresh 12-byte nonce per write) and wrapped in an
envelope ``{version, salt, nonce, ciphertext}`` written to ``secrets.enc`` with
``0o600`` permissions. Plaintext is NEVER written to disk (F4). The random salt
is co-stored (it need not be secret — it only defeats precomputation); machine
binding comes from the master-key source, so copying ``secrets.enc`` to another
machine fails GCM verification — that is a feature, surfaced as a friendly
:class:`DecryptError` (F7), not a crash.

Version 2 uses stable OS machine identifiers (macOS IOPlatformUUID, Linux
machine-id, then hostname only as a last resort). Version 1 used hostname on
macOS, which can change with the network; reads therefore try the legacy
hostname identity and transparently re-encrypt with the stable identity when it
works.

``cryptography`` is optional: the import is guarded so the module imports (and
tests skip) gracefully when it is missing; the vault then reports itself
unavailable and callers fall back to env-only resolution.
"""

from __future__ import annotations

import getpass
import hashlib
import json
import os
import platform
import secrets as _secrets
import subprocess
from base64 import b64decode, b64encode
from collections.abc import Mapping
from pathlib import Path

try:  # pragma: no cover - exercised via CRYPTOGRAPHY_AVAILABLE branches
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    CRYPTOGRAPHY_AVAILABLE = True
except ImportError:  # pragma: no cover
    AESGCM = None  # type: ignore[assignment, misc]
    InvalidTag = Exception  # type: ignore[assignment, misc]
    CRYPTOGRAPHY_AVAILABLE = False

_ENVELOPE_VERSION = 2
_PBKDF2_ITERS = 200_000
_KEY_LEN = 32  # 256-bit
_SALT_LEN = 16
_NONCE_LEN = 12


class VaultUnavailable(RuntimeError):
    """``cryptography`` is not installed, so the encrypted vault is unusable.

    Callers downgrade to env-only resolution and warn (§2.8).
    """


class DecryptError(RuntimeError):
    """Vault could not be decrypted on this machine/user (F7).

    Almost always means ``secrets.enc`` was carried to a different machine or
    user. Message carries actionable recovery steps; callers treat the vault as
    empty and keep running env-only (§2.8) rather than crashing.
    """


class SecretMissing(KeyError):
    """A required secret alias is absent from every source (P5)."""

    def __init__(self, alias: str) -> None:
        self.alias = alias
        super().__init__(
            f"missing secret: {alias}\n"
            f"  add it to the vault via SecretVault.set({alias!r}, ...), or\n"
            f"  export {alias}=...  (env wins over the vault)"
        )


def _stable_machine_id() -> str:
    """A stable per-machine identifier with cross-platform fallbacks (§2.6)."""
    for source in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
        try:
            text = Path(source).read_text(encoding="utf-8").strip()
            if text:
                return text
        except OSError:
            continue
    if platform.system() == "Darwin":
        try:
            out = subprocess.run(
                ["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
                check=False,
                capture_output=True,
                text=True,
                timeout=2,
            ).stdout
            for line in out.splitlines():
                if "IOPlatformUUID" not in line:
                    continue
                _, _, value = line.partition("=")
                uuid = value.strip().strip('"')
                if uuid:
                    return uuid
        except (OSError, subprocess.SubprocessError, TimeoutError):
            pass
    return platform.node() or "unknown-host"


def _legacy_machine_id() -> str:
    """Pre-v2 fallback: hostname can change on macOS when networks change."""
    return platform.node() or "unknown-host"


def _current_user() -> str:
    try:
        return getpass.getuser()
    except (OSError, KeyError):  # pragma: no cover - depends on environment
        return os.environ.get("USER", "unknown-user")


def _primary_identity(*, machine_secret: str | None = None) -> str:
    if machine_secret is not None:
        return machine_secret
    return f"{_stable_machine_id()}:{_current_user()}"


def _legacy_identity() -> str:
    return f"{_legacy_machine_id()}:{_current_user()}"


def _identity_candidates(*, machine_secret: str | None = None) -> list[str]:
    primary = _primary_identity(machine_secret=machine_secret)
    if machine_secret is not None:
        return [primary]
    legacy = _legacy_identity()
    return [primary] if legacy == primary else [primary, legacy]


def _derive_key(salt: bytes, identity: str) -> bytes:
    """Derive the 256-bit master key from a machine-bound identity."""
    return hashlib.pbkdf2_hmac(
        "sha256", identity.encode("utf-8"), salt, _PBKDF2_ITERS, dklen=_KEY_LEN
    )


class SecretVault:
    """AES-256-GCM machine-bound vault implementing ``SecretProvider`` (F4).

    For deterministic tests, an explicit ``machine_secret`` can be injected so
    key derivation does not depend on the real machine fingerprint (§2.9). A
    *different* ``machine_secret`` on read reproduces the machine-migration
    decryption-failure path (F7).
    """

    def __init__(
        self,
        path: Path,
        *,
        machine_secret: str | None = None,
    ) -> None:
        if not CRYPTOGRAPHY_AVAILABLE:
            raise VaultUnavailable(
                "the 'cryptography' package is required for the encrypted vault; "
                "install it with: pip install cryptography"
            )
        self._path = path
        self._machine_secret = machine_secret
        self._cache: dict[str, str] = {}
        self._loaded = False
        self._rewrite_after_load = False

    # -- SecretProvider ---------------------------------------------------- #
    def get(self, key: str) -> str | None:
        self._ensure_loaded()
        return self._cache.get(key)

    def require(self, key: str) -> str:
        value = self.get(key)
        if value is None:
            raise SecretMissing(key)
        return value

    # -- mutation ---------------------------------------------------------- #
    def set(self, key: str, value: str) -> None:
        self._ensure_loaded()
        self._cache[key] = value
        self._flush()

    def update(self, values: Mapping[str, str]) -> None:
        """Merge multiple secrets and persist with a single encrypted write."""
        if not values:
            return
        self._ensure_loaded()
        self._cache.update({str(k): str(v) for k, v in values.items()})
        self._flush()

    def replace_all(self, values: Mapping[str, str]) -> None:
        """Replace the vault contents without first decrypting the old file.

        This is used for explicit operator-driven recovery from a plaintext
        source file after the old vault key can no longer be derived.
        """
        self._cache = {str(k): str(v) for k, v in values.items()}
        self._loaded = True
        self._rewrite_after_load = False
        self._flush()

    def delete(self, key: str) -> bool:
        self._ensure_loaded()
        existed = self._cache.pop(key, None) is not None
        if existed:
            self._flush()
        return existed

    def list_aliases(self) -> list[str]:
        """Return alias names only — never values (§2.3, security boundary)."""
        self._ensure_loaded()
        return sorted(self._cache)

    # -- persistence ------------------------------------------------------- #
    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._cache = self._read()
        self._loaded = True
        if self._rewrite_after_load:
            self._flush()

    def _read(self) -> dict[str, str]:
        if not self._path.is_file():
            return {}
        envelope = json.loads(self._path.read_text(encoding="utf-8"))
        version = int(envelope.get("version", 1))
        salt = b64decode(envelope["salt"])
        nonce = b64decode(envelope["nonce"])
        ciphertext = b64decode(envelope["ciphertext"])
        assert AESGCM is not None  # guarded by CRYPTOGRAPHY_AVAILABLE
        plaintext: bytes | None = None
        last_error: InvalidTag | None = None
        for idx, identity in enumerate(
            _identity_candidates(machine_secret=self._machine_secret)
        ):
            key = _derive_key(salt, identity)
            try:
                plaintext = AESGCM(key).decrypt(nonce, ciphertext, None)
                if idx > 0 or version < _ENVELOPE_VERSION:
                    self._rewrite_after_load = True
                break
            except InvalidTag as exc:
                last_error = exc
        if plaintext is None:
            raise DecryptError(
                f"the secret vault at {self._path} cannot be decrypted on this "
                "machine/user (most likely it was moved from another machine or "
                "account, or the old hostname-derived key changed).\n"
                f"  recover: re-import secrets from a plaintext source file, delete "
                f"{self._path} and re-add secrets, or inject them via environment "
                "variables (env wins over the vault)."
            ) from last_error
        data = json.loads(plaintext.decode("utf-8"))
        return {str(k): str(v) for k, v in data.items()}

    def _flush(self) -> None:
        salt = _secrets.token_bytes(_SALT_LEN)
        nonce = _secrets.token_bytes(_NONCE_LEN)
        key = _derive_key(salt, _primary_identity(machine_secret=self._machine_secret))
        assert AESGCM is not None  # guarded by CRYPTOGRAPHY_AVAILABLE
        plaintext = json.dumps(self._cache).encode("utf-8")
        ciphertext = AESGCM(key).encrypt(nonce, plaintext, None)
        envelope = {
            "version": _ENVELOPE_VERSION,
            "salt": b64encode(salt).decode("ascii"),
            "nonce": b64encode(nonce).decode("ascii"),
            "ciphertext": b64encode(ciphertext).decode("ascii"),
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic write: temp file → 0o600 → replace, so plaintext never lands and
        # no partial ciphertext is observable.
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(json.dumps(envelope), encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, self._path)
        self._rewrite_after_load = False


class EnvFirstSecretProvider:
    """Layer ``os.environ`` over a vault — env wins on the same key (F5).

    ``get``/``require`` check the environment first (an explicit export is the
    operator's intent and outranks the vault, §2.5). With no vault (e.g.
    ``cryptography`` missing or a migration ``DecryptError``) it degrades to
    env-only resolution instead of crashing (§2.8). A process-local session layer
    can be populated by interactive frontends; effective priority is env →
    session → vault. Persistent mutations are delegated to the vault when present.
    """

    def __init__(
        self,
        vault: SecretVault | None = None,
        *,
        env: dict[str, str] | None = None,
    ) -> None:
        self._vault = vault
        self._env: Mapping[str, str] = env if env is not None else os.environ
        self._session: dict[str, str] = {}

    def get(self, key: str) -> str | None:
        env_value = self._env.get(key)
        if env_value is not None:
            return env_value
        session_value = self._session.get(key)
        if session_value is not None:
            return session_value
        if self._vault is None:
            return None
        try:
            return self._vault.get(key)
        except DecryptError:
            # Machine migration: vault is treated as empty, env-only continues.
            return None

    def require(self, key: str) -> str:
        value = self.get(key)
        if value is None:
            raise SecretMissing(key)
        return value

    def set(self, key: str, value: str) -> None:
        if self._vault is None:
            raise VaultUnavailable("no vault configured; cannot persist secret")
        self._vault.set(key, value)

    def delete(self, key: str) -> bool:
        if self._vault is None:
            return False
        return self._vault.delete(key)

    def set_session(self, key: str, value: str) -> None:
        self._session[key] = value

    def delete_session(self, key: str) -> bool:
        return self._session.pop(key, None) is not None

    def session_aliases(self) -> list[str]:
        return sorted(self._session)

    def vault_aliases(self) -> list[str]:
        if self._vault is None:
            return []
        try:
            return self._vault.list_aliases()
        except DecryptError:
            return []

    def list_aliases(self) -> list[str]:
        """Return known non-env aliases only — never values."""
        return sorted(set(self.session_aliases()) | set(self.vault_aliases()))


__all__ = [
    "CRYPTOGRAPHY_AVAILABLE",
    "DecryptError",
    "EnvFirstSecretProvider",
    "SecretMissing",
    "SecretVault",
    "VaultUnavailable",
]
