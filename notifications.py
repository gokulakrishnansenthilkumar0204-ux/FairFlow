"""Bilingual (English + Tamil) SMS notifications with a pluggable provider.

A provider is a function (phone, text) -> (status, reference). Only `mock`
(records "simulated", sends nothing) and `disabled` exist; no real SMS is sent.
A real gateway needs an account, sender ID and DLT-registered templates from an
SMS provider - add it to PROVIDERS, reading credentials from env vars.

notify() is best-effort and never raises: it runs after the business
transaction has committed, so a notification failure cannot undo it.
"""

import logging
import os
import uuid

from db import call

log = logging.getLogger("fairflow.notifications")

TEMPLATES = {
    "distribution_completed": {
        "en": "FairFlow: Ration distribution for {village} was completed on {date}.",
        "ta": "FairFlow: {village} கிராமத்திற்கான ரேஷன் விநியோகம் {date} அன்று நிறைவடைந்தது.",
    },
    "complaint_status": {
        "en": "FairFlow: Your complaint #{complaint_id} is now {status}.",
        "ta": "FairFlow: உங்கள் புகார் #{complaint_id} இன் நிலை: {status}.",
    },
}


def render(event: str, **fields) -> str:
    template = TEMPLATES[event]
    return f"{template['en'].format(**fields)}\n{template['ta'].format(**fields)}"


def mock_provider(phone: str, text: str) -> tuple[str, str]:
    """SIMULATION ONLY - nothing is sent."""
    return "simulated", f"MOCK-{uuid.uuid4().hex[:16]}"


def disabled_provider(phone: str, text: str) -> tuple[str, None]:
    return "disabled", None


PROVIDERS = {"mock": mock_provider, "disabled": disabled_provider}


def get_provider():
    return PROVIDERS[os.getenv("SMS_PROVIDER", "disabled")]


def notify(event: str, *, shop_id: int | None = None, user_id: int | None = None, **fields) -> None:
    try:
        text = render(event, **fields)
        provider = get_provider()
        for recipient in call("ff_notification_recipients", shop_id, user_id):
            status, reference = "skipped_no_phone", None
            if recipient["phone"]:
                try:
                    status, reference = provider(recipient["phone"], text)
                except Exception as exc:  # provider outage must not break anything
                    status = "failed"
                    log.warning("SMS provider error for %s: %s", event, type(exc).__name__)
            call("ff_log_notification", recipient["user_id"], event, text, status, reference)
    except Exception as exc:
        log.error("Notification processing failed for %s: %s", event, type(exc).__name__)
