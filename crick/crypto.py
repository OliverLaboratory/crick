"""Hashing, keys, and signatures."""

import hashlib
import json

from ecdsa import BadSignatureError, SECP256k1, SigningKey, VerifyingKey


def sha256d(data: bytes) -> bytes:
    return hashlib.sha256(hashlib.sha256(data).digest()).digest()


def sha256d_hex(data: bytes) -> str:
    return sha256d(data).hex()


def canonical_json(obj) -> str:
    """Deterministic serialization used for all hashing and signing."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def address_from_pubkey(pubkey_hex: str) -> str:
    digest = hashlib.sha256(bytes.fromhex(pubkey_hex)).hexdigest()
    return "crk1" + digest[:40]


class Wallet:
    def __init__(self, signing_key: SigningKey):
        self._sk = signing_key
        self.pubkey = signing_key.get_verifying_key().to_string().hex()
        self.address = address_from_pubkey(self.pubkey)

    @classmethod
    def generate(cls) -> "Wallet":
        return cls(SigningKey.generate(curve=SECP256k1))

    @classmethod
    def from_hex(cls, private_key_hex: str) -> "Wallet":
        return cls(SigningKey.from_string(bytes.fromhex(private_key_hex), curve=SECP256k1))

    @property
    def private_key_hex(self) -> str:
        return self._sk.to_string().hex()

    def sign(self, message: bytes) -> str:
        return self._sk.sign_deterministic(message).hex()


def verify_signature(pubkey_hex: str, signature_hex: str, message: bytes) -> bool:
    try:
        vk = VerifyingKey.from_string(bytes.fromhex(pubkey_hex), curve=SECP256k1)
        return vk.verify(bytes.fromhex(signature_hex), message)
    except (BadSignatureError, ValueError):
        return False
