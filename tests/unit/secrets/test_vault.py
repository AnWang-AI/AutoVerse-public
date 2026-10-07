"""Unit tests — SecretVault + EnvFirstSecretProvider (docs/reference/config.md §2)."""

from __future__ import annotations

import json
from base64 import b64encode
from pathlib import Path

import pytest

from autoverse.infra.secrets import CRYPTOGRAPHY_AVAILABLE

pytestmark = pytest.mark.skipif(
    not CRYPTOGRAPHY_AVAILABLE, reason="cryptography not installed"
)

from autoverse.infra.secrets import (  # noqa: E402
    DecryptError,
    EnvFirstSecretProvider,
    SecretMissing,
    SecretVault,
)


def _vault(tmp_path: Path, secret: str = "fixed-machine") -> SecretVault:
    return SecretVault(tmp_path / "secrets.enc", machine_secret=secret)


def test_set_get_roundtrip(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    vault.set("ANTHROPIC_API_KEY", "sk-ant-secret")
    assert vault.get("ANTHROPIC_API_KEY") == "sk-ant-secret"


def test_persists_across_instances(tmp_path: Path) -> None:
    _vault(tmp_path).set("K", "v1")
    # A fresh instance with the same machine secret decrypts the same value.
    assert _vault(tmp_path).get("K") == "v1"


def test_require_missing_raises(tmp_path: Path) -> None:
    with pytest.raises(SecretMissing):
        _vault(tmp_path).require("NOPE")


def test_delete(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    vault.set("K", "v")
    assert vault.delete("K") is True
    assert vault.delete("K") is False
    assert vault.get("K") is None


def test_list_aliases_never_returns_values(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    vault.set("A", "secret-a")
    vault.set("B", "secret-b")
    aliases = vault.list_aliases()
    assert aliases == ["A", "B"]
    assert "secret-a" not in str(aliases)


def test_plaintext_never_on_disk(tmp_path: Path) -> None:
    path = tmp_path / "secrets.enc"
    SecretVault(path, machine_secret="m").set("K", "PLAINTEXT-SECRET-VALUE")
    raw = path.read_bytes()
    assert b"PLAINTEXT-SECRET-VALUE" not in raw
    # File is owner-only.
    assert (path.stat().st_mode & 0o777) == 0o600


def test_wrong_machine_secret_raises_decrypt_error(tmp_path: Path) -> None:
    SecretVault(tmp_path / "secrets.enc", machine_secret="machine-a").set("K", "v")
    migrated = SecretVault(tmp_path / "secrets.enc", machine_secret="machine-b")
    with pytest.raises(DecryptError):
        migrated.get("K")


def test_legacy_hostname_vault_is_reencrypted_with_stable_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from autoverse.infra.secrets import vault as vault_mod

    monkeypatch.setattr(vault_mod, "_stable_machine_id", lambda: "stable-machine")
    monkeypatch.setattr(vault_mod, "_legacy_machine_id", lambda: "old-hostname")
    monkeypatch.setattr(vault_mod, "_current_user", lambda: "alden")

    path = tmp_path / "secrets.enc"
    salt = b"0" * 16
    nonce = b"1" * 12
    key = vault_mod._derive_key(salt, "old-hostname:alden")
    assert vault_mod.AESGCM is not None
    ciphertext = vault_mod.AESGCM(key).encrypt(nonce, b'{"K": "v"}', None)
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "salt": b64encode(salt).decode("ascii"),
                "nonce": b64encode(nonce).decode("ascii"),
                "ciphertext": b64encode(ciphertext).decode("ascii"),
            }
        ),
        encoding="utf-8",
    )

    vault = SecretVault(path)
    assert vault.get("K") == "v"
    rewritten = json.loads(path.read_text(encoding="utf-8"))
    assert rewritten["version"] == 2

    # A fresh instance now decrypts through the stable identity, not the legacy
    # hostname path.
    monkeypatch.setattr(vault_mod, "_legacy_machine_id", lambda: "changed-hostname")
    assert SecretVault(path).get("K") == "v"


def test_project_plaintext_secrets_import_into_encrypted_vault(tmp_path: Path) -> None:
    from autoverse.app.builder import _import_plaintext_secret_source

    project = tmp_path / "project"
    source = project / ".autoverse" / "secrets"
    source.parent.mkdir(parents=True)
    source.write_text(
        "export OPENROUTER_API_KEY='sk-openrouter-secret'\n"
        "DEEPSEEK_API_KEY=sk-deepseek-secret # local comment\n",
        encoding="utf-8",
    )
    path = tmp_path / "data" / "secrets.enc"
    vault = SecretVault(path, machine_secret="m")

    _import_plaintext_secret_source(vault, path, project)

    assert vault.get("OPENROUTER_API_KEY") == "sk-openrouter-secret"
    assert vault.get("DEEPSEEK_API_KEY") == "sk-deepseek-secret"
    assert b"sk-openrouter-secret" not in path.read_bytes()


def test_env_first_provider_env_wins(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    vault.set("K", "from-vault")
    provider = EnvFirstSecretProvider(vault, env={"K": "from-env"})
    assert provider.get("K") == "from-env"


def test_env_first_falls_back_to_vault(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    vault.set("K", "from-vault")
    provider = EnvFirstSecretProvider(vault, env={})
    assert provider.get("K") == "from-vault"


def test_env_first_degrades_on_migration(tmp_path: Path) -> None:
    SecretVault(tmp_path / "secrets.enc", machine_secret="machine-a").set("K", "v")
    migrated = SecretVault(tmp_path / "secrets.enc", machine_secret="machine-b")
    provider = EnvFirstSecretProvider(migrated, env={})
    # Migration → vault treated as empty, no crash; env-only continues.
    assert provider.get("K") is None
    provider_env = EnvFirstSecretProvider(migrated, env={"K": "env-v"})
    assert provider_env.get("K") == "env-v"
