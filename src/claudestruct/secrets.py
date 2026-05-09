"""Pluggable secrets backend (W5.4).

Replaces the direct ``os.environ["ANTHROPIC_API_KEY"]`` read scattered
through the codebase with a small abstraction so operators can keep
their API key in:

- ``env`` (default; no extra deps)
- ``keyring`` (OS keychain — macOS Keychain, Linux Secret Service /
  Gnome Keyring / KWallet, Windows Credential Manager). Optional dep.
- ``pass`` (the standard Unix password manager). Calls out to the
  ``pass`` binary; no Python dep.
- ``file`` (read a path from disk; useful for ``$XDG_RUNTIME_DIR``
  ramfs or Docker secrets at ``/run/secrets/...``).

The provider is selected by ``CLAUDESTRUCT_SECRETS_PROVIDER`` (default
``env``). Each provider answers ``get(name) -> str | None`` for a
canonical secret name like ``anthropic.api_key``. Misses fall back to
the next provider (chain configured by the caller); the ``env`` chain
also accepts the legacy uppercase env-var name (``ANTHROPIC_API_KEY``)
so existing setups keep working.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


class SecretsProvider(Protocol):
    """Read-only secrets backend. Returns ``None`` for misses (caller
    decides whether to fail or fall through to another provider)."""

    name: str

    def get(self, key: str) -> str | None: ...


# Canonical name -> uppercase env-var fallback. Lets users set
# ANTHROPIC_API_KEY without learning the new ``anthropic.api_key`` name.
_LEGACY_ENV_MAP = {
    "anthropic.api_key": "ANTHROPIC_API_KEY",
    "github.token": "GITHUB_TOKEN",
    "slack.bot_token": "SLACK_BOT_TOKEN",
    "slack.app_token": "SLACK_APP_TOKEN",
}


def _canonical_to_env(key: str) -> str:
    """Map ``anthropic.api_key`` to the env-var name a user would set
    (``CLAUDESTRUCT_SECRET_ANTHROPIC_API_KEY``)."""
    return "CLAUDESTRUCT_SECRET_" + key.upper().replace(".", "_")


@dataclass
class EnvProvider:
    """Reads from ``os.environ``. Two lookups per key:

    1. Canonical: ``CLAUDESTRUCT_SECRET_ANTHROPIC_API_KEY``
    2. Legacy: ``ANTHROPIC_API_KEY``

    The legacy fallback keeps every existing install working.
    """

    name: str = "env"

    def get(self, key: str) -> str | None:
        canonical = _canonical_to_env(key)
        if canonical in os.environ and os.environ[canonical]:
            return os.environ[canonical]
        legacy = _LEGACY_ENV_MAP.get(key)
        if legacy and legacy in os.environ and os.environ[legacy]:
            return os.environ[legacy]
        return None


@dataclass
class KeyringProvider:
    """Reads from the OS keychain via the ``keyring`` PyPI package.

    Service is hardcoded to ``claudestruct``; key is the canonical
    name. Set with::

        keyring set claudestruct anthropic.api_key

    The package import is lazy so users without ``keyring`` installed
    don't pay for it on cold start.
    """

    service: str = "claudestruct"
    name: str = "keyring"

    def get(self, key: str) -> str | None:
        try:
            import keyring  # type: ignore
        except ImportError:
            return None
        try:
            return keyring.get_password(self.service, key)
        except Exception:
            # keyring backends raise a zoo of unrelated errors when
            # the platform service is unavailable. Treat all as miss.
            return None


@dataclass
class PassProvider:
    """Reads from the Unix ``pass`` password manager.

    Looks up entries under the configurable ``prefix`` (default
    ``claudestruct/``) so a real ``pass`` store with personal items
    isn't shadowed.
    """

    prefix: str = "claudestruct"
    name: str = "pass"

    def get(self, key: str) -> str | None:
        binary = shutil.which("pass")
        if binary is None:
            return None
        path = f"{self.prefix}/{key}" if self.prefix else key
        try:
            res = subprocess.run(
                [binary, "show", path],
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if res.returncode != 0:
            return None
        # `pass` may emit a multi-line entry; the convention is the
        # first line is the secret and subsequent lines are metadata.
        first = res.stdout.partition("\n")[0].strip()
        return first or None


@dataclass
class FileProvider:
    """Reads from a directory of secret files.

    Designed for ``/run/secrets/<key>`` (Docker secrets) and
    ``$XDG_RUNTIME_DIR/claudestruct/<key>``. Trailing whitespace is
    stripped — a common gotcha with secrets shoveled in via shell
    redirects.
    """

    base: Path
    name: str = "file"

    def get(self, key: str) -> str | None:
        path = self.base / key.replace(".", "_")
        if not path.is_file():
            return None
        try:
            return path.read_text(encoding="utf-8").strip() or None
        except OSError:
            return None


# --- Cloud / centralised secret stores (W5.4 follow-up) -------------
#
# Both providers lazy-import their SDK so the default ``pip install
# claudestruct`` stays lean. Operators install ``[secrets-aws]`` /
# ``[secrets-vault]`` extras when they actually wire the chain.
#
# Both providers translate canonical key names (``anthropic.api_key``)
# into the cloud's native naming convention. AWS uses path-like
# names with ``/``; Vault uses KV-v2 paths under a configurable
# mount. The ``key_prefix`` attribute lets operators pin a per-env
# prefix so prod / staging never share a secret accidentally.


@dataclass
class AwsSecretsManagerProvider:
    """Read ``anthropic.api_key`` from AWS Secrets Manager.

    Canonical key → AWS secret name:
        ``anthropic.api_key`` → ``<key_prefix>anthropic/api_key``

    Authentication uses the standard boto3 chain (env / shared
    credentials file / IAM role on EC2 / EKS IRSA), so no extra
    config is needed inside an AWS-hosted runtime.

    Cache TTL is intentionally short (60 s) — the typical
    rotation interval is hours, but a stale cache surviving
    a key revocation would defeat the rotation. Tests can pass
    ``cache_ttl_s=0`` to disable.
    """

    region_name: str
    key_prefix: str = ""
    cache_ttl_s: float = 60.0
    client: object | None = None  # injected for tests
    name: str = "aws-secrets"

    def __post_init__(self) -> None:
        # Per-key cache: name -> (value, expires_at). Avoids
        # spamming Secrets Manager on every request.
        self._cache: dict[str, tuple[str, float]] = {}

    def _aws_name(self, key: str) -> str:
        return f"{self.key_prefix}{key.replace('.', '/')}"

    def _client(self) -> object:
        if self.client is not None:
            return self.client
        # Lazy import so the default install stays lean.
        try:
            import boto3  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "AwsSecretsManagerProvider requires boto3; "
                "install with `pip install claudestruct[secrets-aws]`"
            ) from exc
        return boto3.client("secretsmanager", region_name=self.region_name)

    def get(self, key: str) -> str | None:
        import time as _time
        now = _time.time()
        cached = self._cache.get(key)
        if cached is not None and cached[1] > now:
            return cached[0]

        aws_name = self._aws_name(key)
        client = self._client()
        try:
            resp = client.get_secret_value(SecretId=aws_name)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            # Lazy-imported botocore exceptions; treat any error as
            # a miss and let the chain fall through to the next
            # provider (e.g. env). Logged at debug level so it
            # doesn't spam normal operation.
            return None

        # Secrets Manager returns SecretString or SecretBinary.
        if "SecretString" in resp and resp["SecretString"]:
            value = resp["SecretString"]
        elif "SecretBinary" in resp and resp["SecretBinary"]:
            try:
                value = resp["SecretBinary"].decode("utf-8")
            except (UnicodeDecodeError, AttributeError):
                return None
        else:
            return None

        if self.cache_ttl_s > 0:
            self._cache[key] = (value, now + self.cache_ttl_s)
        return value


@dataclass
class VaultProvider:
    """Read from HashiCorp Vault KV-v2.

    Canonical key → Vault path:
        ``anthropic.api_key`` → ``<mount>/data/<key_prefix>anthropic/api_key``
    Field name within the secret defaults to ``value`` — the typical
    "single field" shape for app secrets — and is overridable via
    ``field``.

    Authentication via ``VAULT_TOKEN`` env (or ``token`` arg).
    Approle / k8s auth aren't bundled here; operators that need them
    inject a pre-configured ``client`` (e.g. an ``hvac.Client`` that
    already finished an Approle login).

    Cache TTL identical to AWS provider — short enough that a
    rotation is reflected within a minute.
    """

    url: str
    token: str | None = None
    mount: str = "secret"
    key_prefix: str = ""
    field: str = "value"
    cache_ttl_s: float = 60.0
    client: object | None = None  # injected for tests
    name: str = "vault"

    def __post_init__(self) -> None:
        self._cache: dict[str, tuple[str, float]] = {}

    def _vault_path(self, key: str) -> str:
        return f"{self.key_prefix}{key.replace('.', '/')}"

    def _client(self) -> object:
        if self.client is not None:
            return self.client
        try:
            import hvac  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "VaultProvider requires hvac; "
                "install with `pip install claudestruct[secrets-vault]`"
            ) from exc
        token = self.token or os.environ.get("VAULT_TOKEN")
        return hvac.Client(url=self.url, token=token)

    def get(self, key: str) -> str | None:
        import time as _time
        now = _time.time()
        cached = self._cache.get(key)
        if cached is not None and cached[1] > now:
            return cached[0]

        path = self._vault_path(key)
        client = self._client()
        try:
            kv = client.secrets.kv.v2  # type: ignore[attr-defined]
            resp = kv.read_secret_version(mount_point=self.mount, path=path)
        except Exception:  # noqa: BLE001
            return None

        # hvac shape: {"data": {"data": {<field>: <value>, ...}, "metadata": {...}}}
        try:
            value = resp["data"]["data"].get(self.field)
        except (KeyError, TypeError):
            return None
        if not value:
            return None
        if not isinstance(value, str):
            value = str(value)
        if self.cache_ttl_s > 0:
            self._cache[key] = (value, now + self.cache_ttl_s)
        return value


# --- Public API -----------------------------------------------------

def default_chain() -> list[SecretsProvider]:
    """Build the provider chain selected by env.

    ``CLAUDESTRUCT_SECRETS_PROVIDER`` accepts a comma-separated list:
    ``env,keyring,pass,file:/run/secrets``. Order matters; first hit
    wins. Default: ``env`` only (back-compat).
    """
    raw = os.environ.get("CLAUDESTRUCT_SECRETS_PROVIDER", "env")
    chain: list[SecretsProvider] = []
    for part in [p.strip() for p in raw.split(",") if p.strip()]:
        if part == "env":
            chain.append(EnvProvider())
        elif part == "keyring":
            chain.append(KeyringProvider())
        elif part == "pass":
            chain.append(PassProvider())
        elif part.startswith("file:"):
            chain.append(FileProvider(base=Path(part[5:])))
        elif part == "aws-secrets" or part.startswith("aws-secrets:"):
            # `aws-secrets` uses default region from env; explicit
            # form `aws-secrets:us-west-2` pins one. Prefix may be
            # set via CLAUDESTRUCT_AWS_SECRETS_PREFIX.
            region = os.environ.get("AWS_REGION", "us-east-1")
            if ":" in part:
                region = part.split(":", 1)[1] or region
            chain.append(AwsSecretsManagerProvider(
                region_name=region,
                key_prefix=os.environ.get(
                    "CLAUDESTRUCT_AWS_SECRETS_PREFIX", "",
                ),
            ))
        elif part == "vault" or part.startswith("vault:"):
            # `vault` reads VAULT_ADDR; `vault:https://my-vault:8200`
            # pins one inline.
            vault_url = os.environ.get("VAULT_ADDR", "http://localhost:8200")
            if part.startswith("vault:"):
                # part like `vault:https://...` — strip the "vault:"
                # but keep everything after, including any colons.
                tail = part[len("vault:"):]
                if tail:
                    vault_url = tail
            chain.append(VaultProvider(
                url=vault_url,
                mount=os.environ.get("CLAUDESTRUCT_VAULT_MOUNT", "secret"),
                key_prefix=os.environ.get(
                    "CLAUDESTRUCT_VAULT_PREFIX", "",
                ),
            ))
        # Unknown providers are skipped silently — typos shouldn't
        # break a run that has the value in another provider.
    if not chain:
        chain.append(EnvProvider())
    return chain


def get(
    key: str,
    *,
    providers: Iterable[SecretsProvider] | None = None,
) -> str | None:
    """Look up a canonical secret name across the configured chain.

    Returns ``None`` only if every provider misses; the caller decides
    whether to fall back to a default or raise.
    """
    chain = list(providers) if providers is not None else default_chain()
    for p in chain:
        v = p.get(key)
        if v:
            return v
    return None


def require(
    key: str,
    *,
    providers: Iterable[SecretsProvider] | None = None,
) -> str:
    """Like :func:`get` but raises ``KeyError`` on miss with a message
    listing which providers were consulted (for actionable errors)."""
    chain = list(providers) if providers is not None else default_chain()
    for p in chain:
        v = p.get(key)
        if v:
            return v
    names = ", ".join(p.name for p in chain) or "<none>"
    raise KeyError(
        f"secret '{key}' not found in providers: {names}. "
        f"Set it via env (CLAUDESTRUCT_SECRET_{key.upper().replace('.', '_')}), "
        f"keyring, pass, or file."
    )
