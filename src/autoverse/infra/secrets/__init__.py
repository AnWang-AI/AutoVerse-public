"""Encrypted secret vault (docs/reference/config.md §2).

Default :class:`~autoverse.ports.SecretProvider` implementation: secrets are
stored on disk as AES-256-GCM ciphertext in ``~/.autoverse/secrets.enc``, with
the master key derived from a stable machine-bound identity (F4 — plaintext
NEVER written). :class:`EnvFirstSecretProvider` layers ``os.environ`` over the
vault so an explicit shell export always wins (F5). Decryption failure after a
machine migration raises a friendly :class:`DecryptError` with recovery
guidance rather than crashing (F7).

This subsystem lives in ``infra`` and is never imported by ``core`` (P3).
"""

from __future__ import annotations

from autoverse.infra.secrets.vault import (
    CRYPTOGRAPHY_AVAILABLE,
    DecryptError,
    EnvFirstSecretProvider,
    SecretMissing,
    SecretVault,
    VaultUnavailable,
)

__all__ = [
    "CRYPTOGRAPHY_AVAILABLE",
    "DecryptError",
    "EnvFirstSecretProvider",
    "SecretMissing",
    "SecretVault",
    "VaultUnavailable",
]
