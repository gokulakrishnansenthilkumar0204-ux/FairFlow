// Fingerprint sign-in with WebAuthn passkeys.
// The browser asks the device's own authenticator (Touch ID, Android fingerprint,
// Windows Hello) to verify the user. The fingerprint never leaves the device; the
// server only ever receives a public key (once) and signatures over its challenges.
import { post } from './api.js';

const b64urlToBuf = (s) => {
  const pad = '='.repeat((4 - (s.length % 4)) % 4);
  const bin = atob((s + pad).replace(/-/g, '+').replace(/_/g, '/'));
  return Uint8Array.from(bin, (c) => c.charCodeAt(0)).buffer;
};
const bufToB64url = (buf) => {
  let bin = '';
  new Uint8Array(buf).forEach((b) => { bin += String.fromCharCode(b); });
  return btoa(bin).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
};

export async function checkSupport() {
  if (!window.isSecureContext) {
    return { ok: false, level: 'bad', message: 'Fingerprint sign-in needs a secure connection. Open FairFlow over HTTPS, or at http://localhost:8000 on this computer.' };
  }
  if (!window.PublicKeyCredential || !navigator.credentials) {
    return { ok: false, level: 'bad', message: 'This browser does not support passkeys. Use a current version of Chrome, Edge, Safari or Firefox.' };
  }
  let platform = false;
  try { platform = await PublicKeyCredential.isUserVerifyingPlatformAuthenticatorAvailable(); } catch { /* treat as unknown */ }
  return platform
    ? { ok: true, level: 'ok', message: 'Fingerprint sensor ready on this device.' }
    : { ok: true, level: 'warn', message: 'No fingerprint sensor was found on this device. Set up Touch ID, Windows Hello or a phone fingerprint lock first; your browser may also offer to use a phone nearby.' };
}

function creationOptions(json) {
  if (PublicKeyCredential.parseCreationOptionsFromJSON) return PublicKeyCredential.parseCreationOptionsFromJSON(json);
  return {
    ...json,
    challenge: b64urlToBuf(json.challenge),
    user: { ...json.user, id: b64urlToBuf(json.user.id) },
    excludeCredentials: (json.excludeCredentials || []).map((c) => ({ ...c, id: b64urlToBuf(c.id) })),
  };
}

function requestOptions(json) {
  if (PublicKeyCredential.parseRequestOptionsFromJSON) return PublicKeyCredential.parseRequestOptionsFromJSON(json);
  return {
    ...json,
    challenge: b64urlToBuf(json.challenge),
    allowCredentials: (json.allowCredentials || []).map((c) => ({ ...c, id: b64urlToBuf(c.id) })),
  };
}

function toJSON(cred) {
  const r = cred.response;
  const out = {
    id: cred.id, rawId: bufToB64url(cred.rawId), type: cred.type,
    authenticatorAttachment: cred.authenticatorAttachment || undefined,
    clientExtensionResults: cred.getClientExtensionResults ? cred.getClientExtensionResults() : {},
    response: { clientDataJSON: bufToB64url(r.clientDataJSON) },
  };
  if (r.attestationObject) {
    out.response.attestationObject = bufToB64url(r.attestationObject);
    out.response.transports = r.getTransports ? r.getTransports() : [];
  } else {
    out.response.authenticatorData = bufToB64url(r.authenticatorData);
    out.response.signature = bufToB64url(r.signature);
    if (r.userHandle) out.response.userHandle = bufToB64url(r.userHandle);
  }
  return out;
}

function friendly(err) {
  switch (err?.name) {
    case 'NotAllowedError': return new Error('The fingerprint check was cancelled or timed out. Try again and touch the sensor when asked.');
    case 'InvalidStateError': return new Error('This device already has a passkey for this account. Sign in instead.');
    case 'SecurityError': return new Error('This address is not allowed for passkeys. Open FairFlow at the address configured on the server (WEBAUTHN_RP_ID / WEBAUTHN_ORIGIN).');
    case 'NotSupportedError': return new Error('This device cannot create a passkey. Make sure a fingerprint or screen lock is set up.');
    case 'AbortError': return new Error('The fingerprint request was interrupted. Try again.');
    default: return err instanceof Error ? err : new Error(String(err));
  }
}

async function create(optionsJson) {
  try {
    return toJSON(await navigator.credentials.create({ publicKey: creationOptions(optionsJson) }));
  } catch (err) { throw friendly(err); }
}

async function get(optionsJson) {
  try {
    return toJSON(await navigator.credentials.get({ publicKey: requestOptions(optionsJson), mediation: 'optional' }));
  } catch (err) {
    if (err?.name === 'NotAllowedError') {
      throw new Error('No FairFlow fingerprint was used. If this device is new, register first ("New beneficiary" or "Activate account"); otherwise try again and touch the sensor.');
    }
    throw friendly(err);
  }
}

export function deviceLabel() {
  const ua = navigator.userAgent;
  if (/iPhone/.test(ua)) return 'iPhone';
  if (/iPad/.test(ua)) return 'iPad';
  if (/Android/.test(ua)) return 'Android phone';
  if (/Mac OS X/.test(ua)) return 'Mac';
  if (/Windows/.test(ua)) return 'Windows PC';
  if (/Linux/.test(ua)) return 'Linux computer';
  return 'This device';
}

// Fingerprint only: the request names no account, the device picks its FairFlow passkey.
export async function signIn() {
  const options = await post('/auth/passkey/login/options', {}, { auth: false });
  const credential = await get(options);
  return post('/auth/passkey/login/verify', { credential }, { auth: false });
}

export async function signUp({ username, shop_id, phone }) {
  const options = await post('/auth/passkey/signup/options', { username, shop_id, phone: phone || null }, { auth: false });
  const credential = await create(options);
  return post('/auth/passkey/signup/verify', { credential, device_label: deviceLabel() }, { auth: false });
}

export async function activate({ username, activation_code }) {
  const options = await post('/auth/passkey/enroll/options', { username, activation_code }, { auth: false });
  const credential = await create(options);
  return post('/auth/passkey/enroll/verify', { credential, device_label: deviceLabel() }, { auth: false });
}

export async function addThisDevice() {
  const options = await post('/auth/passkey/add/options');
  const credential = await create(options);
  return post('/auth/passkey/add/verify', { credential, device_label: deviceLabel() });
}
