"""A minimal software WebAuthn authenticator for tests.

It produces the same data a real platform authenticator (Touch ID, Android
fingerprint, Windows Hello) returns after a successful local fingerprint check:
a real P-256 key pair, a "none" attestation and ECDSA-signed assertions. The
server verifies these with py_webauthn exactly as it does for real devices.
"""

import hashlib
import json
import os
import struct

import cbor2
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url

ORIGIN = "http://localhost:8000"
UP, UV, AT = 0x01, 0x04, 0x40


class SoftAuthenticator:
    def __init__(self, rp_id: str = "localhost", origin: str = ORIGIN, user_verified: bool = True):
        self.rp_id, self.origin, self.user_verified = rp_id, origin, user_verified
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.credential_id = os.urandom(32)
        self.user_handle = b""
        self.counter = 0

    def _flags(self, extra: int = 0) -> int:
        return UP | (UV if self.user_verified else 0) | extra

    def _client_data(self, kind: str, challenge: str, origin: str | None) -> bytes:
        return json.dumps({"type": kind, "challenge": challenge, "origin": origin or self.origin,
                           "crossOrigin": False}).encode()

    def create(self, options: dict, origin: str | None = None) -> dict:
        """navigator.credentials.create() equivalent."""
        self.user_handle = base64url_to_bytes(options["user"]["id"])
        numbers = self.key.public_key().public_numbers()
        cose_key = cbor2.dumps({1: 2, 3: -7, -1: 1,
                                -2: numbers.x.to_bytes(32, "big"), -3: numbers.y.to_bytes(32, "big")})
        auth_data = (hashlib.sha256(self.rp_id.encode()).digest() + bytes([self._flags(AT)])
                     + struct.pack(">I", self.counter) + bytes(16)
                     + struct.pack(">H", len(self.credential_id)) + self.credential_id + cose_key)
        attestation = cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": auth_data})
        cid = bytes_to_base64url(self.credential_id)
        return {
            "id": cid, "rawId": cid, "type": "public-key",
            "response": {
                "clientDataJSON": bytes_to_base64url(
                    self._client_data("webauthn.create", options["challenge"], origin)),
                "attestationObject": bytes_to_base64url(attestation),
                "transports": ["internal"],
            },
            "authenticatorAttachment": "platform", "clientExtensionResults": {},
        }

    def get(self, options: dict, origin: str | None = None) -> dict:
        """navigator.credentials.get() equivalent (after a successful fingerprint)."""
        self.counter += 1
        auth_data = (hashlib.sha256(self.rp_id.encode()).digest() + bytes([self._flags()])
                     + struct.pack(">I", self.counter))
        client_data = self._client_data("webauthn.get", options["challenge"], origin)
        signature = self.key.sign(auth_data + hashlib.sha256(client_data).digest(),
                                  ec.ECDSA(hashes.SHA256()))
        cid = bytes_to_base64url(self.credential_id)
        return {
            "id": cid, "rawId": cid, "type": "public-key",
            "response": {
                "clientDataJSON": bytes_to_base64url(client_data),
                "authenticatorData": bytes_to_base64url(auth_data),
                "signature": bytes_to_base64url(signature),
                "userHandle": bytes_to_base64url(self.user_handle),
            },
            "authenticatorAttachment": "platform", "clientExtensionResults": {},
        }
