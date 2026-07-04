"""
Security layer for LANCOM.

Design (deliberately simple over clever - a subtly-broken custom protocol
is worse than a plain one that's actually correct):

- Every device generates a persistent X25519 identity keypair on first
  launch. The device's id is SHA-256(public_key)[:16] - identity is
  self-certifying, so nothing on the LAN can claim someone else's id
  without also having their private key.
- Two devices that know each other's public key (from a discovery HELLO)
  derive a shared symmetric key via X25519 ECDH + HKDF. This key is
  static per pair (same every time, not renegotiated per session), so
  this does NOT provide forward secrecy: compromise of either device's
  long-term private key can retroactively decrypt captured traffic
  between that pair. It DOES stop passive eavesdropping and active
  tampering/spoofing by any other device on the LAN.
- All messaging/signaling/file-transfer traffic is authenticated
  encryption (ChaCha20-Poly1305) using that per-pair key - there is no
  plaintext fallback for these channels.
- Call audio uses a separate key per call, derived from the same shared
  secret plus a random per-call nonce exchanged during signaling, so
  audio keys aren't reused across calls even though the underlying
  identity keys are static.
- Sensitive local storage fields (message text, filenames) are encrypted
  at rest with a key derived from the device's own identity key -
  never transmitted, purely local protection if the SQLite file is
  pulled off the device.
"""

import hashlib
import os

from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey, X25519PublicKey,
)
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

NONCE_LEN = 12


class Identity:
    """This device's persistent keypair. peer_id is derived from the
    public key, so it's self-certifying - no separate signing step needed."""

    def __init__(self, private_key: X25519PrivateKey):
        self.private_key = private_key
        self.public_key = private_key.public_key()
        self.public_bytes = self.public_key.public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        self.peer_id = peer_id_for_pubkey(self.public_bytes)

    @classmethod
    def load_or_create(cls, path):
        if os.path.exists(path):
            with open(path, "rb") as f:
                raw = f.read()
            if len(raw) == 32:
                return cls(X25519PrivateKey.from_private_bytes(raw))
        key = X25519PrivateKey.generate()
        raw = key.private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption(),
        )
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(raw)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return cls(key)

    def shared_key_with(self, peer_pubkey_bytes, context=b"lancom-transport"):
        peer_pub = X25519PublicKey.from_public_bytes(peer_pubkey_bytes)
        shared_secret = self.private_key.exchange(peer_pub)
        return HKDF(
            algorithm=hashes.SHA256(), length=32, salt=None, info=context,
        ).derive(shared_secret)

    def storage_key(self):
        """Local-only key, never transmitted - domain-separated from the
        transport key via the HKDF info parameter so reusing the identity
        key material here is safe."""
        raw = self.private_key.private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption(),
        )
        return HKDF(
            algorithm=hashes.SHA256(), length=32, salt=None,
            info=b"lancom-local-storage",
        ).derive(raw)


def peer_id_for_pubkey(pubkey_bytes):
    return hashlib.sha256(pubkey_bytes).hexdigest()[:16]


def fingerprint(pubkey_bytes):
    """Short human-comparable string, like a Signal/SSH safety number, so
    two people can optionally verify they're really talking to each other
    rather than trusting first-contact blindly."""
    digest = hashlib.sha256(pubkey_bytes).hexdigest()
    return " ".join(digest[i:i + 4] for i in range(0, 16, 4)).upper()


def encrypt(key, plaintext: bytes) -> bytes:
    nonce = os.urandom(NONCE_LEN)
    ct = ChaCha20Poly1305(key).encrypt(nonce, plaintext, None)
    return nonce + ct


def decrypt(key, blob: bytes) -> bytes:
    nonce, ct = blob[:NONCE_LEN], blob[NONCE_LEN:]
    return ChaCha20Poly1305(key).decrypt(nonce, ct, None)


def encrypt_with_nonce(key, nonce: bytes, plaintext: bytes) -> bytes:
    """For counter-based nonces (e.g. file chunk index) where the caller
    guarantees uniqueness within this key - used with a fresh per-transfer
    key so a restarting counter never collides across transfers."""
    return ChaCha20Poly1305(key).encrypt(nonce, plaintext, None)


def decrypt_with_nonce(key, nonce: bytes, ciphertext: bytes) -> bytes:
    return ChaCha20Poly1305(key).decrypt(nonce, ciphertext, None)
