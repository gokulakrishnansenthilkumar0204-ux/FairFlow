"""Beneficiary identity verification at the Fair Price Shop counter.

An adapter is a function  (beneficiary_id, capture_token) -> VerificationResult.
`capture_token` is the opaque, already-encrypted payload a certified fingerprint
device / PoS SDK hands to the application; FairFlow forwards it to the provider
and never stores, logs or inspects it. Only the outcome is stored.

Status: NO real provider is integrated. Only the MOCK adapter exists, for local
development and tests. It does not read or match any fingerprint.

To integrate a real, authorised provider, add an adapter to ADAPTERS that calls
that provider's documented API with its issued credentials (all from env vars)
and returns its transaction reference. See README "Biometric verification".
"""

import os
import uuid
from dataclasses import dataclass

from fastapi import HTTPException

MOCK_MATCH_TOKEN = "MOCK-MATCH"  # the only capture value the mock treats as a match


@dataclass(frozen=True)
class VerificationResult:
    verified: bool
    provider: str
    reference: str
    simulated: bool


def mock_adapter(beneficiary_id: int, capture_token: str) -> VerificationResult:
    """SIMULATION ONLY - not biometric verification. Matches iff token == MOCK_MATCH_TOKEN."""
    return VerificationResult(
        verified=capture_token == MOCK_MATCH_TOKEN,
        provider="mock",
        reference=f"MOCK-{uuid.uuid4().hex[:16]}",
        simulated=True,
    )


ADAPTERS = {"mock": mock_adapter}


def get_biometric_adapter():
    name = os.getenv("BIOMETRIC_PROVIDER", "disabled")
    if name == "mock" and os.getenv("APP_ENV") == "production":
        raise HTTPException(503, "Mock biometric adapter is not allowed in production")
    if name not in ADAPTERS:
        raise HTTPException(503, "Biometric verification provider is not configured")
    return ADAPTERS[name]
