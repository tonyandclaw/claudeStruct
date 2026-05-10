"""Customer-managed encryption keys -- envelope encryption (W8.6).

Two-tier key hierarchy, the standard CMEK shape:

    KEK (key-encryption key, held by the KMS provider)
        |
        |  encrypts/decrypts ↓
        v
    DEK (data-encryption key, AES-256, per-tenant)
        |
        |  encrypts/decrypts ↓
        v
    field bytes (AES-GCM with random 12-byte nonce)

Why envelope rather than encrypting fields directly with the KEK?

- The KEK lives in the KMS provider (AWS KMS, GCP KMS, the local dev
  provider, etc). Every direct-encrypt would round-trip there. With
  the envelope, we ask the KMS to wrap a fresh DEK once at tenant
  provisioning, then the DEK lives in memory (or hot-cached on the
  app side) and every per-field call is a local AES-GCM op.
- Rotating the KEK no longer requires re-encrypting every field --
  only the (small) wrapped-DEK blobs need to be re-wrapped under the
  new KEK. Field bytes stay put.

Threat model
------------

Defends against:
- DB at rest leak: the row stores ``ciphertext`` + ``nonce`` only.
  Without the wrapped DEK + KMS-side KEK, the ciphertext is opaque.
- Backup theft: same -- backups never contain the KEK.

Does NOT defend against:
- Live process compromise: the DEK is in memory.
- KMS misconfiguration: a KMS that grants every IAM principal
  access to the KEK is a wet paper bag.
- Side-channel attacks against AES-GCM (use a real KMS for prod;
  the local provider is dev/CI only).

Provider chain
--------------

The KMS provider is selected by ``CLAUDESTRUCT_KMS_PROVIDER`` (default
``local``):

  - ``local``: KEK derived from ``CLAUDESTRUCT_KEK_PASSPHRASE`` via
    PBKDF2-HMAC-SHA256 (480k iters). Dev / CI / air-gapped only.
  - ``aws``: AWS KMS via ``boto3``. KEK ARN read from
    ``CLAUDESTRUCT_KMS_KEY_ID``. ``boto3`` is lazy-imported so the
    OSS install never carries it.
  - ``gcp``: GCP KMS via ``google-cloud-kms``. Same lazy-import.

AWS / GCP wiring is sketched but not exercised in this draft -- the
local provider is the only fully-wired path. The intent is for a
follow-up PR to flesh out the cloud providers once the customer
deployment story is concrete.
"""
from __future__ import annotations

import base64
import os
import secrets as py_secrets
from dataclasses import dataclass
from typing import Protocol

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

# AES-256 key size, in bytes. AES-GCM requires 16, 24, or 32; we pin
# 32 so the threat model stays consistent regardless of provider.
DEK_BYTES = 32

# AES-GCM standard nonce length. 12 bytes lets the implementation use
# the optimized GCTR construction; longer nonces get hashed and lose
# the perf win.
NONCE_BYTES = 12

# PBKDF2 iteration count for the local provider. OWASP 2023 baseline
# is 600k for SHA-256; we pick 480k as a middle ground that keeps a
# fresh process boot under 200ms on commodity hardware while staying
# defensible against offline cracking of leaked passphrases.
_PBKDF2_ITERS = 480_000

# Salt for the local provider's KDF. Fixed (not per-tenant) because
# the passphrase is already a single shared secret -- a per-tenant
# salt would just be security theater. Real KMS providers don't use
# this code path.
_LOCAL_KDF_SALT = b"claudestruct-cmek-v1"


class KMSError(Exception):
    """Raised when a KMS operation fails (wrap/unwrap/network)."""


@dataclass(frozen=True)
class WrappedDEK:
    """A DEK plus the metadata needed to unwrap it on the way back.

    ``provider`` is recorded so a future migration (e.g. moving an org
    from ``local`` to ``aws``) can detect mixed-provider state and
    re-wrap on the way through. ``key_id`` is the provider-specific
    identifier of the KEK that produced this wrap (KMS ARN for AWS,
    the constant ``"local"`` for the local provider).
    """
    provider: str
    key_id: str
    wrapped_bytes: bytes  # the DEK encrypted under the KEK


class KMSProvider(Protocol):
    """Wrap/unwrap a 32-byte DEK under the provider's KEK.

    Providers MUST be deterministic about key_id: passing the same
    key_id to ``unwrap`` that ``wrap`` returned must yield back the
    original DEK. ``unwrap`` must raise :class:`KMSError` on
    authentication failure -- never return a wrong DEK silently.
    """
    name: str

    def wrap(self, dek: bytes) -> WrappedDEK: ...
    def unwrap(self, wrapped: WrappedDEK) -> bytes: ...


def _kek_from_passphrase(passphrase: str) -> bytes:
    """Derive a 32-byte KEK from a passphrase via PBKDF2-HMAC-SHA256."""
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=DEK_BYTES,
        salt=_LOCAL_KDF_SALT,
        iterations=_PBKDF2_ITERS,
    )
    return kdf.derive(passphrase.encode("utf-8"))


@dataclass
class LocalKMSProvider:
    """Dev / CI / air-gapped provider. The KEK is derived from a
    passphrase env var; not suitable for production.

    Configure via ``CLAUDESTRUCT_KEK_PASSPHRASE``. A missing
    passphrase falls back to a random per-process key, which means
    every restart loses the wrapped-DEK cipher -- safe for tests,
    obviously wrong for a real deployment. The boot path that calls
    :func:`default_provider` raises if the passphrase is unset and
    the runtime is non-test, so the silent fallback can't reach prod.
    """
    name: str = "local"
    _kek: bytes | None = None

    def __post_init__(self) -> None:
        if self._kek is None:
            phrase = os.environ.get("CLAUDESTRUCT_KEK_PASSPHRASE")
            if phrase:
                self._kek = _kek_from_passphrase(phrase)
            else:
                # Per-process random KEK -- every restart invalidates
                # existing ciphertext. Tests like this; prod must not
                # hit it. ``default_provider`` enforces.
                self._kek = py_secrets.token_bytes(DEK_BYTES)

    def wrap(self, dek: bytes) -> WrappedDEK:
        if len(dek) != DEK_BYTES:
            raise KMSError(f"DEK must be {DEK_BYTES} bytes, got {len(dek)}")
        nonce = py_secrets.token_bytes(NONCE_BYTES)
        ct = AESGCM(self._kek).encrypt(nonce, dek, associated_data=b"cs-dek-wrap")
        return WrappedDEK(provider=self.name, key_id="local", wrapped_bytes=nonce + ct)

    def unwrap(self, wrapped: WrappedDEK) -> bytes:
        if wrapped.provider != self.name:
            raise KMSError(f"provider mismatch: have {self.name!r}, blob is {wrapped.provider!r}")
        if len(wrapped.wrapped_bytes) < NONCE_BYTES + 16:  # at least nonce + GCM tag
            raise KMSError("wrapped blob too short")
        nonce, ct = wrapped.wrapped_bytes[:NONCE_BYTES], wrapped.wrapped_bytes[NONCE_BYTES:]
        try:
            return AESGCM(self._kek).decrypt(nonce, ct, associated_data=b"cs-dek-wrap")
        except Exception as exc:  # cryptography raises InvalidTag for auth failure
            raise KMSError(f"unwrap auth failed: {exc.__class__.__name__}") from exc


# --- AWS KMS provider (W5.4 / C.6 follow-up) ------------------------
#
# Wraps a 32-byte DEK under the configured KMS key (ARN or alias).
# boto3 is lazy-imported under the new ``[kms-aws]`` extra so the
# default install stays lean. Auth uses the standard boto3 chain
# (env / shared creds / EC2 IAM / EKS IRSA), so an AWS-hosted
# runtime needs no extra config.
#
# AWS KMS has a 4 KB plaintext limit on Encrypt; a 32-byte DEK is
# trivially under that.


@dataclass
class AwsKmsProvider:
    """KMS provider backed by AWS KMS Encrypt / Decrypt.

    Configure via ``CLAUDESTRUCT_AWS_KMS_KEY_ID`` (ARN, alias, or
    bare key id) + ``AWS_REGION`` (boto3 default chain).

    The wrapped blob carries the ciphertext bytes returned by
    ``kms.encrypt`` plus the key id used at wrap time. ``unwrap``
    re-uses ``KeyId`` from the wrapped record so an alias rotation
    doesn't break old ciphertext (KMS will look up the actual key
    via the alias, even if the alias points elsewhere now).
    """

    region_name: str
    key_id: str
    client: object | None = None  # injected for tests
    name: str = "aws"

    def _client(self) -> object:
        if self.client is not None:
            return self.client
        try:
            import boto3  # type: ignore
        except ImportError as exc:
            raise KMSError(
                "AwsKmsProvider requires boto3; "
                "install with `pip install claudestruct[kms-aws]`"
            ) from exc
        return boto3.client("kms", region_name=self.region_name)

    def wrap(self, dek: bytes) -> WrappedDEK:
        if len(dek) != DEK_BYTES:
            raise KMSError(f"DEK must be {DEK_BYTES} bytes, got {len(dek)}")
        client = self._client()
        try:
            resp = client.encrypt(KeyId=self.key_id, Plaintext=dek)  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001
            raise KMSError(f"AWS KMS encrypt failed: {type(exc).__name__}") from exc
        ciphertext = resp.get("CiphertextBlob")
        if not ciphertext:
            raise KMSError("AWS KMS encrypt returned empty CiphertextBlob")
        # KMS resp may include the canonical KeyId (resolved alias →
        # ARN). Prefer that over our config so the wrapped record
        # survives alias re-pointing.
        key_id = resp.get("KeyId", self.key_id)
        return WrappedDEK(
            provider=self.name, key_id=key_id, wrapped_bytes=bytes(ciphertext),
        )

    def unwrap(self, wrapped: WrappedDEK) -> bytes:
        if wrapped.provider != self.name:
            raise KMSError(
                f"provider mismatch: have {self.name!r}, blob is {wrapped.provider!r}",
            )
        if not wrapped.wrapped_bytes:
            raise KMSError("wrapped blob is empty")
        client = self._client()
        try:
            resp = client.decrypt(  # type: ignore[attr-defined]
                CiphertextBlob=wrapped.wrapped_bytes,
                KeyId=wrapped.key_id,
            )
        except Exception as exc:  # noqa: BLE001
            # AWS distinguishes IncorrectKeyException, InvalidCiphertextException,
            # AccessDeniedException, etc. We wrap them all uniformly as
            # KMSError to avoid leaking the specific failure mode upstream
            # (an attacker probing decrypt with crafted blobs shouldn't
            # learn which kind of error we hit).
            raise KMSError(f"AWS KMS decrypt failed: {type(exc).__name__}") from exc
        plaintext = resp.get("Plaintext")
        if not plaintext or len(plaintext) != DEK_BYTES:
            raise KMSError(
                f"AWS KMS returned unexpected plaintext length "
                f"(expected {DEK_BYTES}, got {len(plaintext) if plaintext else 0})",
            )
        return bytes(plaintext)


# --- GCP KMS provider (W5.4 / C.6 follow-up) ------------------------
#
# google-cloud-kms is lazy-imported under a new ``[kms-gcp]`` extra.
# Auth uses Application Default Credentials (env / metadata service /
# service account JSON), so a GKE-hosted runtime needs no extra
# config. The KMS resource path is constructed from
# (project, location, key_ring, key) so the operator only needs to
# set ``CLAUDESTRUCT_GCP_KMS_KEY_NAME`` to the full resource path.


@dataclass
class GcpKmsProvider:
    """KMS provider backed by Google Cloud KMS.

    Configure via ``CLAUDESTRUCT_GCP_KMS_KEY_NAME`` (full resource
    path: ``projects/.../locations/.../keyRings/.../cryptoKeys/...``).
    Auth via Application Default Credentials.

    The wrapped blob carries the ciphertext bytes returned by the
    encrypt RPC. Unlike AWS, GCP KMS doesn't echo the resolved key
    name on the response, so we record the configured one. Key
    rotation is handled server-side by GCP — the same key resource
    auto-resolves to the active version for decrypt.
    """

    key_name: str
    client: object | None = None  # injected for tests
    name: str = "gcp"

    def _client(self) -> object:
        if self.client is not None:
            return self.client
        try:
            from google.cloud import kms  # type: ignore
        except ImportError as exc:
            raise KMSError(
                "GcpKmsProvider requires google-cloud-kms; "
                "install with `pip install claudestruct[kms-gcp]`"
            ) from exc
        return kms.KeyManagementServiceClient()

    def wrap(self, dek: bytes) -> WrappedDEK:
        if len(dek) != DEK_BYTES:
            raise KMSError(f"DEK must be {DEK_BYTES} bytes, got {len(dek)}")
        client = self._client()
        try:
            resp = client.encrypt(  # type: ignore[attr-defined]
                request={"name": self.key_name, "plaintext": dek},
            )
        except Exception as exc:  # noqa: BLE001
            raise KMSError(
                f"GCP KMS encrypt failed: {type(exc).__name__}",
            ) from exc
        ciphertext = getattr(resp, "ciphertext", None)
        if not ciphertext:
            raise KMSError("GCP KMS encrypt returned empty ciphertext")
        return WrappedDEK(
            provider=self.name,
            key_id=self.key_name,
            wrapped_bytes=bytes(ciphertext),
        )

    def unwrap(self, wrapped: WrappedDEK) -> bytes:
        if wrapped.provider != self.name:
            raise KMSError(
                f"provider mismatch: have {self.name!r}, blob is {wrapped.provider!r}",
            )
        if not wrapped.wrapped_bytes:
            raise KMSError("wrapped blob is empty")
        client = self._client()
        try:
            resp = client.decrypt(  # type: ignore[attr-defined]
                request={
                    "name": wrapped.key_id,
                    "ciphertext": wrapped.wrapped_bytes,
                },
            )
        except Exception as exc:  # noqa: BLE001
            # Same error-class collapse as AWS: don't let an attacker
            # probing decrypt enumerate the failure mode.
            raise KMSError(
                f"GCP KMS decrypt failed: {type(exc).__name__}",
            ) from exc
        plaintext = getattr(resp, "plaintext", None)
        if not plaintext or len(plaintext) != DEK_BYTES:
            raise KMSError(
                f"GCP KMS returned unexpected plaintext length "
                f"(expected {DEK_BYTES}, got {len(plaintext) if plaintext else 0})",
            )
        return bytes(plaintext)


def default_provider(*, allow_random_kek: bool = False) -> KMSProvider:
    """Build the provider selected by env.

    Set ``CLAUDESTRUCT_KMS_PROVIDER=local|aws|gcp``. Default ``local``.

    ``allow_random_kek=True`` is the test-only escape hatch; the
    daemon's prod boot path keeps it False so a missing passphrase is
    a hard error rather than silently using a key that vanishes on
    restart.
    """
    name = os.environ.get("CLAUDESTRUCT_KMS_PROVIDER", "local").strip().lower()
    if name == "local":
        if not allow_random_kek and not os.environ.get("CLAUDESTRUCT_KEK_PASSPHRASE"):
            raise KMSError(
                "Local KMS provider requires CLAUDESTRUCT_KEK_PASSPHRASE. "
                "Use a real KMS (CLAUDESTRUCT_KMS_PROVIDER=aws|gcp) for production."
            )
        return LocalKMSProvider()
    if name == "aws":
        key_id = os.environ.get("CLAUDESTRUCT_AWS_KMS_KEY_ID", "").strip()
        if not key_id:
            raise KMSError(
                "AWS KMS provider requires CLAUDESTRUCT_AWS_KMS_KEY_ID "
                "(ARN, alias/<name>, or bare key id).",
            )
        region = os.environ.get("AWS_REGION", "us-east-1")
        return AwsKmsProvider(region_name=region, key_id=key_id)
    if name == "gcp":
        key_name = os.environ.get("CLAUDESTRUCT_GCP_KMS_KEY_NAME", "").strip()
        if not key_name:
            raise KMSError(
                "GCP KMS provider requires CLAUDESTRUCT_GCP_KMS_KEY_NAME "
                "(full resource path: projects/<p>/locations/<l>/"
                "keyRings/<r>/cryptoKeys/<k>).",
            )
        return GcpKmsProvider(key_name=key_name)
    raise KMSError(f"unknown CLAUDESTRUCT_KMS_PROVIDER: {name!r}")


# --- Field-level helpers ---------------------------------------------

def fresh_dek() -> bytes:
    """Mint a new 256-bit DEK. Use one per tenant; rotate per
    retention policy (typically yearly)."""
    return AESGCM.generate_key(bit_length=256)


def encrypt_field(dek: bytes, plaintext: bytes, *, aad: bytes = b"") -> bytes:
    """Encrypt a field under the DEK with a fresh nonce.

    Returns ``nonce || ciphertext || tag`` (the GCM tag is the last
    16 bytes of the AESGCM-emitted ciphertext). Caller stores the
    blob as a single ``BYTEA``; deserialization happens in
    :func:`decrypt_field`.

    ``aad`` is authenticated but not encrypted -- pass the field's
    column name + row primary key so a swapped blob fails decryption
    rather than silently appearing under the wrong row.
    """
    if len(dek) != DEK_BYTES:
        raise KMSError(f"DEK must be {DEK_BYTES} bytes, got {len(dek)}")
    nonce = py_secrets.token_bytes(NONCE_BYTES)
    ct = AESGCM(dek).encrypt(nonce, plaintext, associated_data=aad)
    return nonce + ct


def decrypt_field(dek: bytes, blob: bytes, *, aad: bytes = b"") -> bytes:
    """Inverse of :func:`encrypt_field`. Raises :class:`KMSError` on
    auth failure -- the GCM tag check is the integrity guarantee."""
    if len(blob) < NONCE_BYTES + 16:
        raise KMSError("ciphertext too short")
    nonce, ct = blob[:NONCE_BYTES], blob[NONCE_BYTES:]
    try:
        return AESGCM(dek).decrypt(nonce, ct, associated_data=aad)
    except Exception as exc:
        raise KMSError(f"decrypt auth failed: {exc.__class__.__name__}") from exc


def encode_b64(data: bytes) -> str:
    """URL-safe base64 with no padding -- matches what ``WrappedDEK``
    serialises to in the ``Subscription.wrapped_dek`` column."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def decode_b64(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)
