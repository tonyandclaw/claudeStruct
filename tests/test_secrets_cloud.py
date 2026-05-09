"""Tests for the AWS Secrets Manager + Vault provider adapters
(W5.4 follow-up — B.9).

We don't install boto3 / hvac in the lean test env; instead, both
providers expose a ``client=...`` injection seam that lets the
tests pass a fake client implementing the same surface the real
SDKs expose. That keeps the test suite offline and the test
matrix unchanged.
"""
from __future__ import annotations

import pytest

from claudestruct import secrets as secrets_mod

# --- AWS Secrets Manager --------------------------------------------


class _FakeAwsClient:
    """Mimics the subset of boto3's secretsmanager client the
    AwsSecretsManagerProvider uses."""

    def __init__(self, secrets: dict[str, dict] | None = None,
                 raise_on: set[str] | None = None) -> None:
        self._secrets = secrets or {}
        self._raise_on = raise_on or set()
        self.calls: list[str] = []

    def get_secret_value(self, *, SecretId: str):
        self.calls.append(SecretId)
        if SecretId in self._raise_on:
            raise RuntimeError("simulated AWS error")
        if SecretId not in self._secrets:
            raise RuntimeError(f"ResourceNotFoundException: {SecretId}")
        return dict(self._secrets[SecretId])


def test_aws_provider_translates_canonical_to_path():
    """`anthropic.api_key` → `<prefix>anthropic/api_key`. AWS
    convention is `/`-delimited; canonical is `.`-delimited."""
    fake = _FakeAwsClient(secrets={
        "prod/anthropic/api_key": {"SecretString": "sk-from-aws"},
    })
    p = secrets_mod.AwsSecretsManagerProvider(
        region_name="us-east-1", key_prefix="prod/", client=fake,
    )
    assert p.get("anthropic.api_key") == "sk-from-aws"
    # Single round-trip (cached afterwards), and the key was
    # translated as expected.
    assert fake.calls == ["prod/anthropic/api_key"]


def test_aws_provider_caches_within_ttl():
    """Repeated lookups within `cache_ttl_s` hit the cache, not AWS."""
    fake = _FakeAwsClient(secrets={
        "anthropic/api_key": {"SecretString": "sk-x"},
    })
    p = secrets_mod.AwsSecretsManagerProvider(
        region_name="us-east-1", client=fake, cache_ttl_s=60.0,
    )
    p.get("anthropic.api_key")
    p.get("anthropic.api_key")
    p.get("anthropic.api_key")
    # Three logical lookups, one AWS round-trip.
    assert len(fake.calls) == 1


def test_aws_provider_cache_disabled_by_zero_ttl():
    fake = _FakeAwsClient(secrets={
        "anthropic/api_key": {"SecretString": "sk-x"},
    })
    p = secrets_mod.AwsSecretsManagerProvider(
        region_name="us-east-1", client=fake, cache_ttl_s=0,
    )
    p.get("anthropic.api_key")
    p.get("anthropic.api_key")
    assert len(fake.calls) == 2


def test_aws_provider_miss_returns_none():
    """Unknown secret → AWS raises → provider returns None so the
    chain falls through to the next provider."""
    fake = _FakeAwsClient(secrets={})
    p = secrets_mod.AwsSecretsManagerProvider(
        region_name="us-east-1", client=fake,
    )
    assert p.get("anthropic.api_key") is None


def test_aws_provider_handles_secret_binary():
    """Some keys are stored as SecretBinary (bytes); the provider
    decodes them to UTF-8."""
    fake = _FakeAwsClient(secrets={
        "anthropic/api_key": {"SecretBinary": b"sk-binary"},
    })
    p = secrets_mod.AwsSecretsManagerProvider(
        region_name="us-east-1", client=fake,
    )
    assert p.get("anthropic.api_key") == "sk-binary"


def test_aws_provider_arbitrary_error_is_a_miss():
    """A boto3 / network error must NOT crash the producer — return
    None so the chain falls through."""
    fake = _FakeAwsClient(raise_on={"anthropic/api_key"})
    p = secrets_mod.AwsSecretsManagerProvider(
        region_name="us-east-1", client=fake,
    )
    assert p.get("anthropic.api_key") is None


def test_aws_provider_no_boto3_raises_runtimeerror(monkeypatch):
    """Without `client=` injected and without boto3 installed, the
    provider must surface a clear install hint rather than an
    opaque ImportError."""
    p = secrets_mod.AwsSecretsManagerProvider(region_name="us-east-1")
    # Force the lazy import to fail.
    import sys
    monkeypatch.setitem(sys.modules, "boto3", None)
    with pytest.raises(RuntimeError, match="claudestruct\\[secrets-aws\\]"):
        p.get("anthropic.api_key")


# --- Vault ----------------------------------------------------------


class _FakeVaultKvV2:
    def __init__(self, secrets: dict[str, dict] | None = None,
                 raise_on: set[str] | None = None) -> None:
        self._secrets = secrets or {}
        self._raise_on = raise_on or set()
        self.calls: list[tuple[str, str]] = []

    def read_secret_version(self, *, mount_point: str, path: str):
        self.calls.append((mount_point, path))
        if path in self._raise_on:
            raise RuntimeError("simulated vault error")
        if path not in self._secrets:
            raise RuntimeError(f"InvalidPath: {path}")
        return {"data": {"data": dict(self._secrets[path])}}


class _FakeVaultClient:
    def __init__(self, kv: _FakeVaultKvV2):
        # hvac shape: client.secrets.kv.v2.read_secret_version(...)
        self.secrets = type("_S", (), {"kv": type("_KV", (), {"v2": kv})})()


def test_vault_provider_reads_value_field():
    kv = _FakeVaultKvV2(secrets={
        "anthropic/api_key": {"value": "sk-from-vault"},
    })
    p = secrets_mod.VaultProvider(
        url="http://vault:8200", token="t", client=_FakeVaultClient(kv),
    )
    assert p.get("anthropic.api_key") == "sk-from-vault"
    assert kv.calls == [("secret", "anthropic/api_key")]


def test_vault_provider_custom_field():
    """If the secret stores the value under a non-default field
    (e.g. `data.api_key`), the operator overrides it via `field=`."""
    kv = _FakeVaultKvV2(secrets={
        "anthropic/api_key": {"api_key": "sk-from-vault"},
    })
    p = secrets_mod.VaultProvider(
        url="http://vault:8200", token="t",
        field="api_key", client=_FakeVaultClient(kv),
    )
    assert p.get("anthropic.api_key") == "sk-from-vault"


def test_vault_provider_uses_custom_mount():
    """Some operators run KV-v2 under a custom mount (`prod-kv`)."""
    kv = _FakeVaultKvV2(secrets={
        "anthropic/api_key": {"value": "sk-x"},
    })
    p = secrets_mod.VaultProvider(
        url="http://vault:8200", token="t",
        mount="prod-kv", client=_FakeVaultClient(kv),
    )
    p.get("anthropic.api_key")
    assert kv.calls == [("prod-kv", "anthropic/api_key")]


def test_vault_provider_caches_within_ttl():
    kv = _FakeVaultKvV2(secrets={
        "anthropic/api_key": {"value": "sk-x"},
    })
    p = secrets_mod.VaultProvider(
        url="http://vault:8200", token="t",
        cache_ttl_s=60.0, client=_FakeVaultClient(kv),
    )
    p.get("anthropic.api_key")
    p.get("anthropic.api_key")
    assert len(kv.calls) == 1


def test_vault_provider_miss_returns_none():
    kv = _FakeVaultKvV2(secrets={})
    p = secrets_mod.VaultProvider(
        url="http://vault:8200", token="t", client=_FakeVaultClient(kv),
    )
    assert p.get("anthropic.api_key") is None


def test_vault_provider_arbitrary_error_is_a_miss():
    kv = _FakeVaultKvV2(raise_on={"anthropic/api_key"})
    p = secrets_mod.VaultProvider(
        url="http://vault:8200", token="t", client=_FakeVaultClient(kv),
    )
    assert p.get("anthropic.api_key") is None


def test_vault_provider_no_hvac_raises_runtimeerror(monkeypatch):
    p = secrets_mod.VaultProvider(url="http://vault:8200")
    import sys
    monkeypatch.setitem(sys.modules, "hvac", None)
    with pytest.raises(RuntimeError, match="claudestruct\\[secrets-vault\\]"):
        p.get("anthropic.api_key")


# --- default_chain wiring ------------------------------------------


def test_default_chain_parses_aws_secrets_token(monkeypatch):
    """`CLAUDESTRUCT_SECRETS_PROVIDER=aws-secrets` mounts the AWS
    provider with the default region from env."""
    monkeypatch.setenv("CLAUDESTRUCT_SECRETS_PROVIDER", "aws-secrets")
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    chain = secrets_mod.default_chain()
    assert any(p.name == "aws-secrets" for p in chain)
    aws = next(p for p in chain if p.name == "aws-secrets")
    assert aws.region_name == "us-west-2"


def test_default_chain_parses_aws_secrets_with_inline_region(monkeypatch):
    """`aws-secrets:eu-central-1` pins the region inline."""
    monkeypatch.setenv("CLAUDESTRUCT_SECRETS_PROVIDER", "aws-secrets:eu-central-1")
    chain = secrets_mod.default_chain()
    aws = next(p for p in chain if p.name == "aws-secrets")
    assert aws.region_name == "eu-central-1"


def test_default_chain_parses_vault_token(monkeypatch):
    monkeypatch.setenv("CLAUDESTRUCT_SECRETS_PROVIDER", "vault")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.internal:8200")
    chain = secrets_mod.default_chain()
    vault = next(p for p in chain if p.name == "vault")
    assert vault.url == "https://vault.internal:8200"


def test_default_chain_chain_order_preserved(monkeypatch):
    """`env,aws-secrets,vault` preserves order — first hit wins.
    Critical: env-first means a developer can override a cloud
    secret locally without touching the cloud config."""
    monkeypatch.setenv(
        "CLAUDESTRUCT_SECRETS_PROVIDER", "env,aws-secrets,vault",
    )
    chain = secrets_mod.default_chain()
    names = [p.name for p in chain]
    assert names == ["env", "aws-secrets", "vault"]


def test_default_chain_skips_unknown_provider(monkeypatch):
    """Typos shouldn't break a run that has the value elsewhere."""
    monkeypatch.setenv(
        "CLAUDESTRUCT_SECRETS_PROVIDER", "env,asw-secrets",  # typo
    )
    chain = secrets_mod.default_chain()
    names = [p.name for p in chain]
    assert names == ["env"]
