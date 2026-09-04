"""Envelope encryption for camera credentials.

Camera passwords are the crown jewels of this system. An attacker holding the
RTSP credentials for eighty thousand cameras does not need to breach anything
else — they can watch Gujarat directly. So they get treated accordingly.

**The scheme.** Two layers:

    CREDENTIAL_MASTER_KEY  (32 bytes, from the environment, never in the DB)
        └─ wraps ─▶  per-camera DEK  (32 random bytes, stored wrapped)
                         └─ encrypts ─▶  username, password

Why bother with a per-camera key instead of encrypting directly with the master
key? Three reasons that matter in practice:

* **Blast radius.** Recovering one DEK — say from a memory dump taken while one
  camera was being probed — exposes that one camera, not the fleet.
* **Rotation without re-encryption.** Rotating the master key means re-wrapping
  N small DEKs, not re-encrypting every field. Re-wrapping is a cheap,
  interruptible, resumable operation; bulk re-encryption is not.
* **Nonce hygiene.** AES-GCM fails catastrophically if a (key, nonce) pair is
  ever reused. Spreading plaintexts across many keys keeps the number of
  encryptions under any single key small, which keeps us far away from the
  birthday bound on random 96-bit nonces.

**Ciphertext is bound to its context.** Every encryption commits to the camera
id and the field name via GCM's additional authenticated data. Ciphertext is
therefore non-transplantable: an attacker with UPDATE on the credential table
cannot copy camera 42's encrypted password onto camera 7 and then ask the system
to connect — authentication of the AAD fails and decryption raises. This defends
against a privileged-insider row swap, which no amount of encryption strength
would otherwise stop.

**Format.** ``version(1) || nonce(12) || ciphertext || tag(16)``. The leading
version byte means a future algorithm change can be rolled out lazily, decrypting
old records with the old scheme while writing new ones with the new.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

# Bump only alongside a change to the wire format or primitive.
SCHEME_VERSION = 1

_NONCE_LEN = 12  # 96 bits: the size AES-GCM is designed for.
_KEY_LEN = 32  # AES-256.
_TAG_LEN = 16
_MIN_BLOB = 1 + _NONCE_LEN + _TAG_LEN


class CryptoError(Exception):
    """Encryption or decryption failed. Never carries plaintext or key material."""


@dataclass(frozen=True, slots=True)
class SealedCredential:
    """What gets written to ``app.camera_credential``."""

    username_enc: bytes
    password_enc: bytes
    wrapped_dek: bytes
    key_version: int


def _aad(camera_id: int, field: str) -> bytes:
    """Context that ciphertext is cryptographically bound to.

    Including the scheme version prevents a downgrade attack in which an
    attacker rewrites the version byte to steer us onto a weaker future scheme.
    """
    return f"sentinel:v{SCHEME_VERSION}:camera:{int(camera_id)}:field:{field}".encode()


def _seal(key: bytes, plaintext: bytes, aad: bytes) -> bytes:
    if len(key) != _KEY_LEN:
        raise CryptoError(f"key must be {_KEY_LEN} bytes, got {len(key)}")
    nonce = os.urandom(_NONCE_LEN)
    ct = AESGCM(key).encrypt(nonce, plaintext, aad)
    return bytes([SCHEME_VERSION]) + nonce + ct


def _open(key: bytes, blob: bytes, aad: bytes) -> bytes:
    if len(key) != _KEY_LEN:
        raise CryptoError(f"key must be {_KEY_LEN} bytes, got {len(key)}")
    if blob is None or len(blob) < _MIN_BLOB:
        raise CryptoError("ciphertext is truncated or empty")
    version = blob[0]
    if version != SCHEME_VERSION:
        raise CryptoError(f"unsupported ciphertext version {version}")
    nonce, ct = blob[1 : 1 + _NONCE_LEN], blob[1 + _NONCE_LEN :]
    try:
        return AESGCM(key).decrypt(nonce, ct, aad)
    except InvalidTag as exc:
        # Either the wrong key, or tampering, or ciphertext moved between
        # cameras/fields. We cannot tell which, and deliberately do not guess.
        raise CryptoError("authentication failed: wrong key or tampered ciphertext") from exc


def generate_dek() -> bytes:
    """A fresh data-encryption key. One per camera."""
    return os.urandom(_KEY_LEN)


def wrap_dek(master_key: bytes, dek: bytes, camera_id: int) -> bytes:
    """Encrypt a DEK under the master key, bound to its camera."""
    return _seal(master_key, dek, _aad(camera_id, "dek"))


def unwrap_dek(master_key: bytes, wrapped: bytes, camera_id: int) -> bytes:
    dek = _open(master_key, wrapped, _aad(camera_id, "dek"))
    if len(dek) != _KEY_LEN:
        raise CryptoError("unwrapped DEK has the wrong length")
    return dek


def seal_credential(
    master_key: bytes,
    camera_id: int,
    username: str,
    password: str,
    key_version: int = SCHEME_VERSION,
) -> SealedCredential:
    """Encrypt a camera's username and password for storage.

    A fresh DEK is minted per call, so re-saving a credential rotates its DEK for
    free.
    """
    dek = generate_dek()
    try:
        return SealedCredential(
            username_enc=_seal(dek, username.encode("utf-8"), _aad(camera_id, "username")),
            password_enc=_seal(dek, password.encode("utf-8"), _aad(camera_id, "password")),
            wrapped_dek=wrap_dek(master_key, dek, camera_id),
            key_version=key_version,
        )
    finally:
        del dek


def open_credential(
    master_key: bytes,
    camera_id: int,
    username_enc: bytes,
    password_enc: bytes,
    wrapped_dek: bytes,
) -> tuple[str, str]:
    """Recover a camera's username and password.

    Every call site must first pass the ``camera.credential_use`` check and write
    an audit row. The database enforces the former via
    ``app.get_camera_credential``; the latter is the caller's job.
    """
    dek = unwrap_dek(master_key, wrapped_dek, camera_id)
    try:
        username = _open(dek, username_enc, _aad(camera_id, "username")).decode("utf-8")
        password = _open(dek, password_enc, _aad(camera_id, "password")).decode("utf-8")
        return username, password
    finally:
        del dek


def rewrap_dek(
    old_master_key: bytes, new_master_key: bytes, wrapped: bytes, camera_id: int
) -> bytes:
    """Move a DEK from one master key to another without touching field ciphertext.

    This is the whole point of the envelope: master-key rotation is O(number of
    cameras) tiny operations, and each one is independently restartable, so a
    rotation interrupted halfway leaves the database consistent — every row is
    wrapped under exactly one of the two keys, and both are available during the
    rotation window.
    """
    dek = unwrap_dek(old_master_key, wrapped, camera_id)
    try:
        return wrap_dek(new_master_key, dek, camera_id)
    finally:
        del dek
