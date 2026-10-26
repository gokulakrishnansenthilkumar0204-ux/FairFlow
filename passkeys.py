

import base64
import hashlib
import json
import os
import re
import secrets
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse
from pydantic import AfterValidator, BaseModel, Field
from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url
from webauthn.helpers.cose import COSEAlgorithmIdentifier
from webauthn.helpers.exceptions import InvalidAuthenticationResponse, InvalidRegistrationResponse
from webauthn.helpers.structs import (
    AuthenticatorAttachment,
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from auth import create_access_token, get_current_user, public_user
from db import call
from ratelimit import limit


def _csv(name: str, default: str = "") -> list[str]:
    return [v.strip() for v in os.getenv(name, default).split(",") if v.strip()]


def android_origin(cert_sha256: str) -> str:
    """WebAuthn origin an Android app reports: android:apk-key-hash:<base64url(SHA-256 of signing cert)>."""
    raw = bytes.fromhex(cert_sha256.replace(":", "").strip())
    if len(raw) != 32:
        raise RuntimeError(f"ANDROID_APP_SHA256_CERT_FINGERPRINTS entry is not a SHA-256 fingerprint: {cert_sha256!r}")
    return "android:apk-key-hash:" + base64.urlsafe_b64encode(raw).decode().rstrip("=")


RP_ID = os.getenv("WEBAUTHN_RP_ID", "localhost")
RP_NAME = os.getenv("WEBAUTHN_RP_NAME", "FairFlow")
ANDROID_PACKAGE = os.getenv("ANDROID_APP_PACKAGE", "").strip()
ANDROID_CERT_FINGERPRINTS = _csv("ANDROID_APP_SHA256_CERT_FINGERPRINTS")
IOS_APP_IDS = _csv("IOS_APP_IDS")  # TEAMID.bundle.id
# Web origins plus the origin of every signing key of the Android app.
ORIGINS = _csv("WEBAUTHN_ORIGIN", "http://localhost:8000") + [android_origin(f) for f in ANDROID_CERT_FINGERPRINTS]
CHALLENGE_TTL_SECONDS = 180
MAX_PENDING_SIGNUPS = int(os.getenv("MAX_PENDING_SIGNUPS_PER_SHOP", "200"))
ACTIVATION_TTL_HOURS = int(os.getenv("ACTIVATION_CODE_TTL_HOURS", "24"))
_ALGORITHMS = [COSEAlgorithmIdentifier.ECDSA_SHA_256, COSEAlgorithmIdentifier.EDDSA,
               COSEAlgorithmIdentifier.RSASSA_PKCS1_v1_5_SHA_256]
_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no 0/O/1/I/L
_USERNAME = re.compile(r"^[A-Za-z0-9._-]{3,50}$")

router = APIRouter(tags=["Fingerprint sign-in (passkeys)"])
User = Annotated[dict, Depends(get_current_user)]


def _valid_username(value: str) -> str:
    value = value.strip()
    if not _USERNAME.match(value):
        raise ValueError("Username must be 3-50 characters: letters, digits, dot, dash or underscore")
    return value


# New usernames: trimmed, 3-50 safe characters. Matching is case-insensitive in the DB.
Username = Annotated[str, Field(min_length=3, max_length=60), AfterValidator(_valid_username)]
Phone = Annotated[str | None, Field(default=None, pattern=r"^\+?[0-9]{10,15}$")]


# ---------- Activation codes ----------

def new_activation_code() -> str:
    raw = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(10))
    return f"{raw[:5]}-{raw[5:]}"


def hash_code(code: str) -> str:
    normalized = code.upper().replace("-", "").replace(" ", "")
    return hashlib.sha256(normalized.encode()).hexdigest()


def issue_activation_code(actor_id: int | None, user_id: int) -> dict:
    """Issue a code; any devices the account already has are switched off in the same transaction."""
    code = new_activation_code()
    row = call("ff_issue_activation", actor_id, user_id, hash_code(code),
               ACTIVATION_TTL_HOURS * 3600, one=True)
    return {"username": row["username"], "activation_code": code, "expires_at": row["expires_at"],
            "revoked_passkeys": row["revoked"]}


# ---------- Schemas ----------

class SignupOptionsIn(BaseModel):
    username: Username
    shop_id: int = Field(gt=0)
    phone: Phone = None


class EnrollOptionsIn(BaseModel):
    username: str = Field(min_length=3, max_length=60)
    activation_code: str = Field(min_length=6, max_length=20)


class LoginOptionsIn(BaseModel):
    # Accepted for compatibility with older clients and ignored: sign-in is
    # usernameless, so the response never depends on (or reveals) an account.
    username: str | None = Field(default=None, max_length=60)


class VerifyIn(BaseModel):
    credential: dict[str, Any]
    device_label: str | None = Field(default=None, max_length=60)


class ActivationCodeIn(BaseModel):
    # Accepted for compatibility and ignored: issuing a code always removes existing
    # passkeys and ends open sessions, so a code cannot add a hidden extra device.
    revoke_existing: bool = True


class UserUpdate(BaseModel):
    role: str = Field(pattern=r"^(admin|officer|beneficiary)$")
    shop_id: int | None = Field(default=None, gt=0)


# ---------- Helpers ----------

def _new_handle() -> str:
    return bytes_to_base64url(secrets.token_bytes(32))


def _store_challenge(challenge: bytes, purpose: str, user_id: int | None = None, details: dict | None = None):
    call("ff_create_auth_challenge", bytes_to_base64url(challenge), purpose, user_id,
         json.dumps(details or {}), CHALLENGE_TTL_SECONDS)


def _consume_challenge(credential: dict, purpose: str) -> dict:
    """Read the challenge the device signed and consume our stored copy (single use)."""
    try:
        client_data = json.loads(base64url_to_bytes(credential["response"]["clientDataJSON"]))
        challenge = client_data["challenge"]
        assert isinstance(challenge, str) and 0 < len(challenge) < 200
    except Exception:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Malformed passkey response")
    return call("ff_consume_auth_challenge", challenge, purpose, one=True)


def _registration_options(username: str, handle: str, existing: list[dict]):
    return generate_registration_options(
        rp_id=RP_ID,
        rp_name=RP_NAME,
        user_name=username,
        user_display_name=username,
        user_id=base64url_to_bytes(handle),
        timeout=120_000,
        authenticator_selection=AuthenticatorSelectionCriteria(
            authenticator_attachment=AuthenticatorAttachment.PLATFORM,  # the device's built-in sensor
            # Discoverable credential: later sign-ins need only the fingerprint, no username.
            resident_key=ResidentKeyRequirement.REQUIRED,
            user_verification=UserVerificationRequirement.REQUIRED,
        ),
        exclude_credentials=[PublicKeyCredentialDescriptor(id=base64url_to_bytes(p["credential_id"]))
                             for p in existing],
        supported_pub_key_algs=_ALGORITHMS,
    )


def _verify_registration(credential: dict, challenge: str) -> dict:
    try:
        verified = verify_registration_response(
            credential=credential,
            expected_challenge=base64url_to_bytes(challenge),
            expected_rp_id=RP_ID,
            expected_origin=ORIGINS,
            require_user_verification=True,
            supported_pub_key_algs=_ALGORITHMS,
        )
    except (InvalidRegistrationResponse, KeyError, ValueError, TypeError) as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Fingerprint registration failed: {exc}")
    transports = credential.get("response", {}).get("transports") or []
    return {
        "credential_id": bytes_to_base64url(verified.credential_id),
        "public_key": verified.credential_public_key,
        "sign_count": verified.sign_count,
        "transports": [t for t in transports if isinstance(t, str)][:6],
    }


def _session(user: dict) -> dict:
    if not user.get("is_active", True):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "This account is disabled. Contact your administrator.")
    return {"access_token": create_access_token(user), "token_type": "bearer", "user": public_user(user)}


def _options_json(options) -> dict:
    return json.loads(options_to_json(options))


# ---------- 1a. First time, beneficiary: self-registration with fingerprint ----------

@router.post("/auth/passkey/signup/options", dependencies=[Depends(limit("auth-options")), Depends(limit("signup"))])
def signup_options(item: SignupOptionsIn):
    if call("ff_get_user_by_username", item.username, one=True):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Username already taken")
    if not call("ff_list_shops", item.shop_id, one=True):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Shop not found")
    handle = _new_handle()
    options = _registration_options(item.username, handle, [])
    _store_challenge(options.challenge, "signup", details=item.model_dump() | {"handle": handle})
    return _options_json(options)


@router.post("/auth/passkey/signup/verify", status_code=status.HTTP_201_CREATED,
             dependencies=[Depends(limit("auth-verify")), Depends(limit("signup"))])
def signup_verify(item: VerifyIn):
    challenge = _consume_challenge(item.credential, "signup")
    cred = _verify_registration(item.credential, challenge["challenge"])
    d = challenge["details"]
    user = call("ff_signup_with_passkey", d["username"], d["shop_id"], d.get("phone"), d["handle"],
                cred["credential_id"], cred["public_key"], cred["sign_count"], cred["transports"],
                item.device_label, MAX_PENDING_SIGNUPS, one=True)
    return _session(call("ff_list_users", None, None, user["id"], one=True))


# ---------- 1b. First time, staff (or any new/replacement device): activation code ----------

@router.post("/auth/passkey/enroll/options", dependencies=[Depends(limit("auth-options"))])
def enroll_options(item: EnrollOptionsIn):
    user = call("ff_check_enrollment_code", item.username.strip(), hash_code(item.activation_code), one=True)
    if not user:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid or expired activation code")
    handle = call("ff_get_or_set_webauthn_handle", user["id"], _new_handle(), one=True)["webauthn_handle"]
    options = _registration_options(user["username"], handle, call("ff_list_passkeys", user["id"]))
    _store_challenge(options.challenge, "enroll", user["id"],
                     {"code_hash": hash_code(item.activation_code)})
    return _options_json(options)


@router.post("/auth/passkey/enroll/verify", status_code=status.HTTP_201_CREATED,
             dependencies=[Depends(limit("auth-verify"))])
def enroll_verify(item: VerifyIn):
    challenge = _consume_challenge(item.credential, "enroll")
    cred = _verify_registration(item.credential, challenge["challenge"])
    call("ff_add_passkey", None, challenge["user_id"], cred["credential_id"], cred["public_key"],
         cred["sign_count"], cred["transports"], item.device_label, challenge["details"]["code_hash"])
    return _session(call("ff_list_users", None, None, challenge["user_id"], one=True))


# ---------- Signed-in user adds another device ----------

@router.post("/auth/passkey/add/options")
def add_options(current_user: User):
    handle = call("ff_get_or_set_webauthn_handle", current_user["id"], _new_handle(), one=True)["webauthn_handle"]
    options = _registration_options(current_user["username"], handle,
                                    call("ff_list_passkeys", current_user["id"]))
    _store_challenge(options.challenge, "add", current_user["id"])
    return _options_json(options)


@router.post("/auth/passkey/add/verify", status_code=status.HTTP_201_CREATED)
def add_verify(item: VerifyIn, current_user: User):
    challenge = _consume_challenge(item.credential, "add")
    if challenge["user_id"] != current_user["id"]:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "This request belongs to another account")
    cred = _verify_registration(item.credential, challenge["challenge"])
    return call("ff_add_passkey", current_user["id"], current_user["id"], cred["credential_id"],
                cred["public_key"], cred["sign_count"], cred["transports"], item.device_label, None, one=True)


# ---------- 2. Every sign-in: fingerprint only ----------

@router.post("/auth/passkey/login/options", dependencies=[Depends(limit("auth-options"))])
def login_options(item: LoginOptionsIn | None = None):
    # Usernameless: no allowCredentials. The device shows its FairFlow passkey(s) and the
    # response is identical for everyone, so it reveals nothing about which accounts exist.
    options = generate_authentication_options(
        rp_id=RP_ID, timeout=120_000, allow_credentials=[],
        user_verification=UserVerificationRequirement.REQUIRED,
    )
    _store_challenge(options.challenge, "login")
    return _options_json(options)


@router.post("/auth/passkey/login/verify", dependencies=[Depends(limit("auth-verify"))])
def login_verify(item: VerifyIn):
    challenge = _consume_challenge(item.credential, "login")
    credential_id = item.credential.get("id")
    passkey = call("ff_get_passkey_for_login", credential_id, one=True) if isinstance(credential_id, str) else None
    if not passkey:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED,
                            "This fingerprint is not registered with FairFlow on this device. "
                            "Register first, or ask for an activation code.")
    user_handle = item.credential.get("response", {}).get("userHandle")
    if user_handle and user_handle != passkey["webauthn_handle"]:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Passkey does not match this account")
    try:
        verified = verify_authentication_response(
            credential=item.credential,
            expected_challenge=base64url_to_bytes(challenge["challenge"]),
            expected_rp_id=RP_ID,
            expected_origin=ORIGINS,
            credential_public_key=bytes(passkey["public_key"]),
            credential_current_sign_count=passkey["sign_count"],
            require_user_verification=True,
        )
    except (InvalidAuthenticationResponse, KeyError, ValueError, TypeError) as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, f"Fingerprint sign-in failed: {exc}")
    if not passkey["is_active"]:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "This account is disabled. Contact your administrator.")
    call("ff_record_passkey_login", passkey["passkey_id"], verified.new_sign_count)
    return _session({"id": passkey["user_id"], "username": passkey["username"], "role": passkey["role"],
                     "shop_id": passkey["shop_id"], "is_active": passkey["is_active"],
                     "approved": passkey["approved"], "token_version": passkey["token_version"]})


# ---------- Manage own passkeys ----------

@router.get("/auth/passkeys")
def list_my_passkeys(current_user: User):
    return [{k: p[k] for k in ("id", "device_label", "transports", "created_at", "last_used_at")}
            for p in call("ff_list_passkeys", current_user["id"])]


@router.delete("/auth/passkeys/{passkey_id}")
def delete_my_passkey(passkey_id: int, current_user: User):
    call("ff_delete_passkey", current_user["id"], passkey_id)
    return {"message": "Passkey removed"}


# ---------- Users & access (admin; officers for beneficiaries of their shop) ----------

def _public_listing(row: dict) -> dict:
    return {k: v for k, v in row.items() if k != "token_version"}


def _target_for(current_user: dict, user_id: int, *, allow_self: bool = False) -> dict:
    """The account `current_user` may manage: admin = anyone; officer = beneficiaries of own shop."""
    target = call("ff_list_users", None, None, user_id, one=True)
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    if current_user["role"] == "officer":
        if target["role"] != "beneficiary" or target["shop_id"] != current_user["shop_id"]:
            raise HTTPException(status.HTTP_403_FORBIDDEN,
                                "Officers can only manage beneficiaries of their shop")
    elif current_user["role"] != "admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not authorized to manage accounts")
    if not allow_self and target["id"] == current_user["id"]:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "You cannot do this to your own account; ask another administrator")
    return target


@router.get("/users")
def list_users(current_user: User):
    if current_user["role"] == "admin":
        return [_public_listing(u) for u in call("ff_list_users", None, None, None)]
    if current_user["role"] == "officer":
        return [_public_listing(u) for u in call("ff_list_users", current_user["shop_id"], "beneficiary", None)]
    raise HTTPException(status.HTTP_403_FORBIDDEN, "Not authorized to list users")


@router.post("/users/{user_id}/activation-code", status_code=status.HTTP_201_CREATED)
def create_activation_code(user_id: int, current_user: User, item: ActivationCodeIn | None = None):
    """One-time code (shown once, valid ACTIVATION_CODE_TTL_HOURS) to register a fingerprint
    on a new device. The account's existing devices stop working at once."""
    target = _target_for(current_user, user_id, allow_self=False)
    if not target["is_active"]:
        raise HTTPException(status.HTTP_409_CONFLICT, "This account is disabled. Re-enable it first.")
    return issue_activation_code(current_user["id"], user_id)


@router.delete("/users/{user_id}/passkeys")
def revoke_user_passkeys(user_id: int, current_user: User):
    """Lost or stolen device: remove all of the person's passkeys and end their sessions."""
    _target_for(current_user, user_id)
    revoked = call("ff_revoke_passkeys", current_user["id"], user_id, one=True)["revoked"]
    return {"message": f"Removed {revoked} passkey(s); open sessions were signed out", "revoked": revoked}


@router.post("/users/{user_id}/approve")
def approve_beneficiary(user_id: int, current_user: User):
    target = _target_for(current_user, user_id)
    if target["role"] != "beneficiary":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Only beneficiaries need approval")
    return call("ff_set_beneficiary_approval", current_user["id"], user_id, True, one=True)


@router.post("/users/{user_id}/deactivate")
def deactivate_user(user_id: int, current_user: User):
    _target_for(current_user, user_id)
    return call("ff_set_user_active", current_user["id"], user_id, False, one=True)


@router.post("/users/{user_id}/reactivate")
def reactivate_user(user_id: int, current_user: User):
    # Only an administrator lifts a disabled account, so an officer cannot undo an admin's decision.
    if current_user["role"] != "admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only an administrator can re-enable a disabled account")
    _target_for(current_user, user_id)
    return call("ff_set_user_active", current_user["id"], user_id, True, one=True)


@router.patch("/users/{user_id}")
def update_user(user_id: int, item: UserUpdate, current_user: User):
    """Admin: change someone's role or shop. Their open sessions end."""
    if current_user["role"] != "admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only admin can change roles or shops")
    _target_for(current_user, user_id)
    return call("ff_update_user", current_user["id"], user_id, item.role, item.shop_id, one=True)


# ---------- Mobile app association files ----------

@router.get("/.well-known/assetlinks.json", include_in_schema=False)
def android_asset_links():
    """Lets the Android app (ANDROID_APP_PACKAGE, signed with the listed certificates)
    create and use passkeys for WEBAUTHN_RP_ID."""
    if not (ANDROID_PACKAGE and ANDROID_CERT_FINGERPRINTS):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Android app is not configured")
    return [{
        "relation": ["delegate_permission/common.handle_all_urls",
                     "delegate_permission/common.get_login_creds"],
        "target": {"namespace": "android_app", "package_name": ANDROID_PACKAGE,
                   "sha256_cert_fingerprints": [f.upper() for f in ANDROID_CERT_FINGERPRINTS]},
    }]


@router.get("/.well-known/apple-app-site-association", include_in_schema=False)
def apple_app_site_association():
    """Lets the iOS app (IOS_APP_IDS = TEAMID.bundle.id) use passkeys for WEBAUTHN_RP_ID."""
    if not IOS_APP_IDS:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "iOS app is not configured")
    return JSONResponse({"webcredentials": {"apps": IOS_APP_IDS}})
