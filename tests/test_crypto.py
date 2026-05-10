"""Tests for the CMEK envelope-encryption module (W8.6)."""
from __future__ import annotations

import pytest

pytest.importorskip("cryptography")
pytest.importorskip("fastapi")  # crypto module is server-only

from claudestruct.server import crypto as crypto_mod

# --- LocalKMSProvider -----------------------------------------------

def test_local_kms_round_trip(monkeypatch):
    monkeypatch.setenv("CLAUDESTRUCT_KEK_PASSPHRASE", "correct horse battery staple")
    p = crypto_mod.LocalKMSProvider()
    dek = crypto_mod.fresh_dek()
    wrapped = p.wrap(dek)
    assert wrapped.provider == "local"
    assert wrapped.key_id == "local"
    assert p.unwrap(wrapped) == dek


def test_local_kms_two_passphrases_dont_share_keyspace():
    """Different passphrases must produce un-cross-decryptable wraps.
    Otherwise a passphrase rotation wouldn't actually rotate keys."""
    p1 = crypto_mod.LocalKMSProvider(_kek=crypto_mod._kek_from_passphrase("alpha"))
    p2 = crypto_mod.LocalKMSProvider(_kek=crypto_mod._kek_from_passphrase("beta"))
    dek = crypto_mod.fresh_dek()
    wrapped = p1.wrap(dek)
    with pytest.raises(crypto_mod.KMSError):
        p2.unwrap(wrapped)


def test_local_kms_rejects_wrong_dek_length():
    p = crypto_mod.LocalKMSProvider(_kek=b"\x00" * 32)
    with pytest.raises(crypto_mod.KMSError, match="32 bytes"):
        p.wrap(b"\x00" * 16)  # AES-128 key length, not what we want


def test_local_kms_rejects_truncated_blob():
    p = crypto_mod.LocalKMSProvider(_kek=b"\x00" * 32)
    bad = crypto_mod.WrappedDEK(provider="local", key_id="local", wrapped_bytes=b"\x00")
    with pytest.raises(crypto_mod.KMSError, match="too short"):
        p.unwrap(bad)


def test_local_kms_rejects_wrong_provider_blob():
    p = crypto_mod.LocalKMSProvider(_kek=b"\x00" * 32)
    bad = crypto_mod.WrappedDEK(provider="aws", key_id="arn:...", wrapped_bytes=b"\x00" * 32)
    with pytest.raises(crypto_mod.KMSError, match="provider mismatch"):
        p.unwrap(bad)


def test_local_kms_random_kek_means_restart_invalidates_wrap():
    """Sanity: when the env passphrase is unset, every fresh
    LocalKMSProvider has its own random KEK, and the previous wrap
    is no longer unwrappable. Documents the dev-only escape hatch."""
    p1 = crypto_mod.LocalKMSProvider()
    p2 = crypto_mod.LocalKMSProvider()  # different per-process random KEK
    dek = crypto_mod.fresh_dek()
    wrapped = p1.wrap(dek)
    with pytest.raises(crypto_mod.KMSError):
        p2.unwrap(wrapped)


# --- default_provider -----------------------------------------------

def test_default_provider_local_requires_passphrase(monkeypatch):
    monkeypatch.delenv("CLAUDESTRUCT_KEK_PASSPHRASE", raising=False)
    monkeypatch.delenv("CLAUDESTRUCT_KMS_PROVIDER", raising=False)
    with pytest.raises(crypto_mod.KMSError, match="KEK_PASSPHRASE"):
        crypto_mod.default_provider()


def test_default_provider_local_with_passphrase(monkeypatch):
    monkeypatch.setenv("CLAUDESTRUCT_KEK_PASSPHRASE", "x" * 32)
    p = crypto_mod.default_provider()
    assert p.name == "local"


def test_default_provider_allow_random_kek_skips_check(monkeypatch):
    monkeypatch.delenv("CLAUDESTRUCT_KEK_PASSPHRASE", raising=False)
    p = crypto_mod.default_provider(allow_random_kek=True)
    assert p.name == "local"


def test_default_provider_aws_requires_key_id(monkeypatch):
    """`CLAUDESTRUCT_KMS_PROVIDER=aws` without a key id is a hard
    error — silently falling back to the local provider would mask
    a misconfigured prod deploy."""
    monkeypatch.setenv("CLAUDESTRUCT_KMS_PROVIDER", "aws")
    monkeypatch.delenv("CLAUDESTRUCT_AWS_KMS_KEY_ID", raising=False)
    with pytest.raises(crypto_mod.KMSError, match="CLAUDESTRUCT_AWS_KMS_KEY_ID"):
        crypto_mod.default_provider()


def test_default_provider_aws_returns_aws_provider(monkeypatch):
    monkeypatch.setenv("CLAUDESTRUCT_KMS_PROVIDER", "aws")
    monkeypatch.setenv(
        "CLAUDESTRUCT_AWS_KMS_KEY_ID",
        "arn:aws:kms:us-east-1:111111111111:key/abcd",
    )
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    p = crypto_mod.default_provider()
    assert p.name == "aws"
    assert p.region_name == "us-west-2"
    assert p.key_id.endswith("/abcd")


def test_default_provider_gcp_still_unimplemented(monkeypatch):
    """GCP KMS lands in a follow-up — the explicit NotImplementedError
    keeps the deploy script honest."""
    monkeypatch.setenv("CLAUDESTRUCT_KMS_PROVIDER", "gcp")
    with pytest.raises(NotImplementedError, match="GCP"):
        crypto_mod.default_provider()


def test_default_provider_unknown_name_raises(monkeypatch):
    monkeypatch.setenv("CLAUDESTRUCT_KMS_PROVIDER", "bogus")
    with pytest.raises(crypto_mod.KMSError, match="unknown"):
        crypto_mod.default_provider()


# --- AwsKmsProvider --------------------------------------------------


class _FakeKmsClient:
    """Mimics the subset of boto3's KMS client AwsKmsProvider uses.

    Stores `(key_id, plaintext) -> ciphertext` so encrypt + decrypt
    can round-trip without real crypto. The fake intentionally
    inverts the bytes for "ciphertext" so the test catches a
    `Plaintext`/`CiphertextBlob` swap.
    """

    def __init__(self, *, encrypt_raises=False, decrypt_raises=False):
        self._store: dict[bytes, tuple[str, bytes]] = {}
        self.encrypt_raises = encrypt_raises
        self.decrypt_raises = decrypt_raises
        self.calls: list[tuple[str, dict]] = []

    def encrypt(self, *, KeyId: str, Plaintext: bytes):
        self.calls.append(("encrypt", {"KeyId": KeyId, "Plaintext": Plaintext}))
        if self.encrypt_raises:
            raise RuntimeError("simulated AWS error")
        ct = bytes(b ^ 0xAA for b in Plaintext) + b"|" + KeyId.encode()
        self._store[ct] = (KeyId, bytes(Plaintext))
        return {"CiphertextBlob": ct, "KeyId": KeyId}

    def decrypt(self, *, CiphertextBlob: bytes, KeyId: str | None = None):
        self.calls.append(
            ("decrypt", {"CiphertextBlob": CiphertextBlob, "KeyId": KeyId}),
        )
        if self.decrypt_raises:
            raise RuntimeError("simulated AWS error")
        rec = self._store.get(CiphertextBlob)
        if rec is None:
            raise RuntimeError("InvalidCiphertextException")
        recorded_key, plaintext = rec
        # Real KMS doesn't require KeyId on decrypt but we threaded
        # it through for safety; assert it matches when supplied.
        if KeyId is not None and KeyId != recorded_key:
            raise RuntimeError("IncorrectKeyException")
        return {"Plaintext": plaintext, "KeyId": recorded_key}


def _aws_provider(client=None, **kw):
    return crypto_mod.AwsKmsProvider(
        region_name="us-east-1",
        key_id="arn:aws:kms:us-east-1:111111111111:key/abcd",
        client=client,
        **kw,
    )


def test_aws_kms_round_trip():
    fake = _FakeKmsClient()
    p = _aws_provider(client=fake)
    dek = b"\x01" * crypto_mod.DEK_BYTES
    wrapped = p.wrap(dek)
    assert wrapped.provider == "aws"
    assert wrapped.wrapped_bytes  # non-empty
    out = p.unwrap(wrapped)
    assert out == dek
    # Two calls: encrypt + decrypt.
    assert [c[0] for c in fake.calls] == ["encrypt", "decrypt"]


def test_aws_kms_records_resolved_key_id():
    """When `KeyId` returned by encrypt differs from what was sent
    (alias resolution), wrap records the resolved value so a later
    alias re-pointing doesn't break old ciphertext."""
    fake = _FakeKmsClient()
    p = _aws_provider(client=fake)
    dek = b"\x02" * crypto_mod.DEK_BYTES
    wrapped = p.wrap(dek)
    # Fake KMS echoes our KeyId — but the producer code uses
    # `resp.get("KeyId", self.key_id)`, so a real alias-resolved
    # value would be preserved. Smoke that the path doesn't drop.
    assert wrapped.key_id == p.key_id


def test_aws_kms_rejects_wrong_dek_length():
    fake = _FakeKmsClient()
    p = _aws_provider(client=fake)
    with pytest.raises(crypto_mod.KMSError, match="DEK must be"):
        p.wrap(b"\x00" * 8)


def test_aws_kms_wrap_translates_aws_error():
    """A boto3 / KMS auth failure must surface as KMSError, not
    propagate a botocore exception class."""
    fake = _FakeKmsClient(encrypt_raises=True)
    p = _aws_provider(client=fake)
    with pytest.raises(crypto_mod.KMSError, match="encrypt failed"):
        p.wrap(b"\x03" * crypto_mod.DEK_BYTES)


def test_aws_kms_unwrap_translates_aws_error():
    """All decrypt failure modes (auth, invalid ciphertext, wrong
    key) collapse to a uniform KMSError so an attacker probing
    decrypt with crafted blobs can't enumerate the failure mode."""
    fake = _FakeKmsClient()
    p = _aws_provider(client=fake)
    fake.decrypt_raises = True
    with pytest.raises(crypto_mod.KMSError, match="decrypt failed"):
        p.unwrap(crypto_mod.WrappedDEK(
            provider="aws", key_id=p.key_id, wrapped_bytes=b"garbage",
        ))


def test_aws_kms_unwrap_rejects_wrong_provider_blob():
    """A blob from the local provider must not unwrap on AWS."""
    fake = _FakeKmsClient()
    p = _aws_provider(client=fake)
    with pytest.raises(crypto_mod.KMSError, match="provider mismatch"):
        p.unwrap(crypto_mod.WrappedDEK(
            provider="local", key_id="local", wrapped_bytes=b"x" * 32,
        ))


def test_aws_kms_unwrap_rejects_empty_blob():
    fake = _FakeKmsClient()
    p = _aws_provider(client=fake)
    with pytest.raises(crypto_mod.KMSError, match="empty"):
        p.unwrap(crypto_mod.WrappedDEK(
            provider="aws", key_id=p.key_id, wrapped_bytes=b"",
        ))


def test_aws_kms_no_boto3_raises_kmserror(monkeypatch):
    """Without `client=` and without boto3 installed, the provider
    surfaces a clear install hint rather than an opaque ImportError."""
    p = crypto_mod.AwsKmsProvider(
        region_name="us-east-1",
        key_id="arn:aws:kms:us-east-1:111111111111:key/abcd",
    )
    import sys
    monkeypatch.setitem(sys.modules, "boto3", None)
    with pytest.raises(crypto_mod.KMSError, match="claudestruct\\[kms-aws\\]"):
        p.wrap(b"\x00" * crypto_mod.DEK_BYTES)


def test_aws_kms_unwrap_truncated_aws_response_is_error():
    """If KMS returns a Plaintext shorter than DEK_BYTES, the
    provider rejects rather than handing back a too-short DEK
    that would corrupt downstream ciphertext."""

    class _ShortPlaintextClient(_FakeKmsClient):
        def decrypt(self, *, CiphertextBlob, KeyId=None):
            return {"Plaintext": b"\x00" * 8, "KeyId": KeyId or ""}

    fake = _ShortPlaintextClient()
    p = _aws_provider(client=fake)
    with pytest.raises(crypto_mod.KMSError, match="unexpected plaintext length"):
        p.unwrap(crypto_mod.WrappedDEK(
            provider="aws", key_id=p.key_id, wrapped_bytes=b"any",
        ))


# --- Field-level helpers --------------------------------------------

def test_encrypt_decrypt_round_trip():
    dek = crypto_mod.fresh_dek()
    plaintext = b"the quick brown fox"
    blob = crypto_mod.encrypt_field(dek, plaintext, aad=b"users.email|42")
    assert blob != plaintext
    assert crypto_mod.decrypt_field(dek, blob, aad=b"users.email|42") == plaintext


def test_encrypt_two_calls_produce_different_ciphertext():
    """Random nonces -> two encryptions of the same plaintext under
    the same DEK must not collide. Otherwise an observer could correlate."""
    dek = crypto_mod.fresh_dek()
    a = crypto_mod.encrypt_field(dek, b"same input")
    b = crypto_mod.encrypt_field(dek, b"same input")
    assert a != b


def test_decrypt_with_wrong_aad_fails():
    """AAD-bound row identity: a blob originally written for one row
    must fail decryption when presented under another row's AAD."""
    dek = crypto_mod.fresh_dek()
    blob = crypto_mod.encrypt_field(dek, b"secret", aad=b"users.email|42")
    with pytest.raises(crypto_mod.KMSError, match="auth failed"):
        crypto_mod.decrypt_field(dek, blob, aad=b"users.email|99")


def test_decrypt_with_wrong_dek_fails():
    blob = crypto_mod.encrypt_field(crypto_mod.fresh_dek(), b"secret")
    with pytest.raises(crypto_mod.KMSError, match="auth failed"):
        crypto_mod.decrypt_field(crypto_mod.fresh_dek(), blob)


def test_decrypt_truncated_blob_fails():
    dek = crypto_mod.fresh_dek()
    with pytest.raises(crypto_mod.KMSError, match="too short"):
        crypto_mod.decrypt_field(dek, b"\x00" * 5)


def test_encrypt_field_rejects_wrong_dek_length():
    with pytest.raises(crypto_mod.KMSError, match="32 bytes"):
        crypto_mod.encrypt_field(b"\x00" * 16, b"hi")


# --- base64 helpers -------------------------------------------------

def test_b64_round_trip():
    for sample in [b"", b"\x00", b"\x00\x01\x02", b"hello world"]:
        assert crypto_mod.decode_b64(crypto_mod.encode_b64(sample)) == sample


def test_b64_url_safe_no_padding():
    """Ensure the encoder doesn't emit ``=`` padding -- it'd break
    URL-safe contexts and DB column round-trips that strip whitespace."""
    blob = b"\xff" * 31  # length that would normally pad
    assert "=" not in crypto_mod.encode_b64(blob)
