"""Envelope encryption: round trips, and the attacks it is supposed to stop."""
from __future__ import annotations

import os
import unittest

from app.core import crypto


class CryptoRoundTrip(unittest.TestCase):
    def setUp(self) -> None:
        self.master = os.urandom(32)
        self.camera_id = 4242

    def test_round_trip(self) -> None:
        sealed = crypto.seal_credential(self.master, self.camera_id, "admin", "P@ssw0rd-हिन्दी")
        user, pwd = crypto.open_credential(
            self.master,
            self.camera_id,
            sealed.username_enc,
            sealed.password_enc,
            sealed.wrapped_dek,
        )
        self.assertEqual(user, "admin")
        self.assertEqual(pwd, "P@ssw0rd-हिन्दी")

    def test_empty_password_round_trips(self) -> None:
        # Some cameras genuinely ship with a blank password. Encrypting an empty
        # string must not be a special case that silently stores nothing.
        sealed = crypto.seal_credential(self.master, 1, "admin", "")
        _, pwd = crypto.open_credential(
            self.master, 1, sealed.username_enc, sealed.password_enc, sealed.wrapped_dek
        )
        self.assertEqual(pwd, "")
        self.assertGreater(len(sealed.password_enc), 0)

    def test_ciphertext_is_versioned(self) -> None:
        sealed = crypto.seal_credential(self.master, 7, "u", "p")
        for blob in (sealed.username_enc, sealed.password_enc, sealed.wrapped_dek):
            self.assertEqual(blob[0], crypto.SCHEME_VERSION)

    def test_every_seal_uses_a_fresh_nonce(self) -> None:
        # Nonce reuse under a single key is the one thing that destroys AES-GCM
        # outright: it leaks the XOR of the plaintexts and the authentication key.
        # Same key, same plaintext, many times.
        dek = crypto.generate_dek()
        nonces = {
            crypto._seal(dek, b"same-plaintext", b"aad")[1:13] for _ in range(500)
        }
        self.assertEqual(len(nonces), 500)

    def test_each_camera_gets_a_distinct_dek(self) -> None:
        deks = {crypto.generate_dek() for _ in range(200)}
        self.assertEqual(len(deks), 200)


class CryptoAttacks(unittest.TestCase):
    def setUp(self) -> None:
        self.master = os.urandom(32)

    def test_wrong_master_key_fails(self) -> None:
        sealed = crypto.seal_credential(self.master, 1, "admin", "secret")
        with self.assertRaises(crypto.CryptoError):
            crypto.open_credential(
                os.urandom(32), 1, sealed.username_enc, sealed.password_enc, sealed.wrapped_dek
            )

    def test_credential_cannot_be_moved_between_cameras(self) -> None:
        """The insider row-swap attack.

        Someone with UPDATE on app.camera_credential copies camera 100's
        encrypted password onto camera 200, whose password they do not know, then
        asks the system to connect to 200 and reads the credential from a probe
        log. Binding the AAD to the camera id makes this fail at decryption.
        """
        sealed = crypto.seal_credential(self.master, 100, "admin", "camera-100-secret")
        with self.assertRaises(crypto.CryptoError):
            crypto.open_credential(
                self.master,
                200,  # <- the transplant
                sealed.username_enc,
                sealed.password_enc,
                sealed.wrapped_dek,
            )

    def test_username_ciphertext_cannot_be_read_as_a_password(self) -> None:
        """Field confusion: swapping the two columns must not decrypt."""
        sealed = crypto.seal_credential(self.master, 5, "operator", "hunter2")
        dek = crypto.unwrap_dek(self.master, sealed.wrapped_dek, 5)
        with self.assertRaises(crypto.CryptoError):
            crypto._open(dek, sealed.username_enc, crypto._aad(5, "password"))

    def test_bit_flip_is_detected(self) -> None:
        sealed = crypto.seal_credential(self.master, 9, "admin", "secret")
        blob = bytearray(sealed.password_enc)
        blob[-1] ^= 0x01  # flip one bit of the tag
        with self.assertRaises(crypto.CryptoError):
            crypto.open_credential(
                self.master, 9, sealed.username_enc, bytes(blob), sealed.wrapped_dek
            )

    def test_ciphertext_body_tamper_is_detected(self) -> None:
        sealed = crypto.seal_credential(self.master, 9, "admin", "secret")
        blob = bytearray(sealed.password_enc)
        blob[20] ^= 0xFF  # flip a bit in the ciphertext, not the tag
        with self.assertRaises(crypto.CryptoError):
            crypto.open_credential(
                self.master, 9, sealed.username_enc, bytes(blob), sealed.wrapped_dek
            )

    def test_version_downgrade_is_rejected(self) -> None:
        sealed = crypto.seal_credential(self.master, 3, "admin", "secret")
        blob = bytearray(sealed.password_enc)
        blob[0] = 99
        with self.assertRaises(crypto.CryptoError):
            crypto.open_credential(
                self.master, 3, sealed.username_enc, bytes(blob), sealed.wrapped_dek
            )

    def test_truncated_ciphertext_is_rejected_not_crashed(self) -> None:
        for bad in (b"", b"\x01", b"\x01" * 5, None):
            with self.assertRaises(crypto.CryptoError):
                crypto.unwrap_dek(self.master, bad, 1)  # type: ignore[arg-type]

    def test_wrong_length_key_is_rejected(self) -> None:
        with self.assertRaises(crypto.CryptoError):
            crypto.wrap_dek(os.urandom(16), crypto.generate_dek(), 1)

    def test_forged_wrapped_dek_of_wrong_length_is_rejected(self) -> None:
        """A DEK that unwraps successfully but is the wrong size must not be used."""
        forged = crypto._seal(self.master, os.urandom(16), crypto._aad(1, "dek"))
        with self.assertRaises(crypto.CryptoError):
            crypto.unwrap_dek(self.master, forged, 1)


class MasterKeyRotation(unittest.TestCase):
    def test_rewrap_preserves_field_ciphertext(self) -> None:
        old, new = os.urandom(32), os.urandom(32)
        sealed = crypto.seal_credential(old, 77, "admin", "rotate-me")

        rewrapped = crypto.rewrap_dek(old, new, sealed.wrapped_dek, 77)

        # The username/password ciphertext was never touched...
        user, pwd = crypto.open_credential(
            new, 77, sealed.username_enc, sealed.password_enc, rewrapped
        )
        self.assertEqual((user, pwd), ("admin", "rotate-me"))

        # ...and the old master key no longer opens the re-wrapped DEK.
        with self.assertRaises(crypto.CryptoError):
            crypto.open_credential(
                old, 77, sealed.username_enc, sealed.password_enc, rewrapped
            )

    def test_partially_rotated_fleet_stays_readable(self) -> None:
        """A rotation interrupted halfway must leave every row openable.

        This is the property that makes rotation safe to run on a live system:
        each row is wrapped under exactly one of the two keys, and during the
        window both are available.
        """
        old, new = os.urandom(32), os.urandom(32)
        rows = {cid: crypto.seal_credential(old, cid, f"u{cid}", f"p{cid}") for cid in range(10)}

        wrapped = {cid: s.wrapped_dek for cid, s in rows.items()}
        for cid in range(5):  # rotate the first half, then "crash"
            wrapped[cid] = crypto.rewrap_dek(old, new, wrapped[cid], cid)

        for cid, sealed in rows.items():
            for key in (new, old):
                try:
                    _, pwd = crypto.open_credential(
                        key, cid, sealed.username_enc, sealed.password_enc, wrapped[cid]
                    )
                except crypto.CryptoError:
                    continue
                self.assertEqual(pwd, f"p{cid}")
                break
            else:
                self.fail(f"camera {cid} could not be opened with either key")


if __name__ == "__main__":
    unittest.main()
