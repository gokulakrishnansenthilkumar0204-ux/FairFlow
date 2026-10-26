// Where the FairFlow API lives.
// '' = same origin (the default: the backend serves this frontend at http://localhost:8000).
// If you host the frontend elsewhere, set the API URL here, add the frontend's origin to
// CORS_ORIGINS, and set WEBAUTHN_ORIGIN / WEBAUTHN_RP_ID to the FRONTEND's origin/domain
// on the backend (passkeys are bound to the site the user sees in the address bar).
export const API_BASE = '';
