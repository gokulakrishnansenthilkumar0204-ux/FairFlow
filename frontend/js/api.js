import { API_BASE } from './config.js';

const KEY = 'fairflow.session';

// Session lives in sessionStorage: it ends when the tab is closed.
export const session = {
  read() {
    try { return JSON.parse(sessionStorage.getItem(KEY)) || null; } catch { return null; }
  },
  save(data) {
    try { sessionStorage.setItem(KEY, JSON.stringify({ token: data.access_token, user: data.user })); } catch { /* private mode */ }
  },
  clear() {
    try { sessionStorage.removeItem(KEY); } catch { /* ignore */ }
  },
};

export class ApiError extends Error {
  constructor(status, message) { super(message); this.status = status; }
}

function describe(detail, status) {
  if (typeof detail === 'string') return detail;
  if (Array.isArray(detail)) {  // FastAPI validation errors
    return detail.map((d) => {
      const field = (d.loc || []).filter((p) => p !== 'body').join(' ');
      return field ? `${field}: ${d.msg}` : d.msg;
    }).join('; ');
  }
  if (status === 0) return 'Cannot reach the FairFlow server. Check that it is running.';
  return `Request failed (${status})`;
}

export async function api(path, { method = 'GET', body, auth = true } = {}) {
  const headers = { Accept: 'application/json' };
  const current = session.read();
  if (auth && current?.token) headers.Authorization = `Bearer ${current.token}`;
  if (body !== undefined) headers['Content-Type'] = 'application/json';

  let res;
  try {
    res = await fetch(`${API_BASE}${path}`, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
  } catch {
    throw new ApiError(0, describe(null, 0));
  }
  const data = await res.json().catch(() => null);
  if (!res.ok) {
    if (res.status === 401 && auth) {
      session.clear();
      window.dispatchEvent(new CustomEvent('fairflow:signed-out', { detail: 'Your session ended. Sign in with your fingerprint again.' }));
    }
    throw new ApiError(res.status, describe(data?.detail, res.status));
  }
  return data;
}

export const post = (path, body, opts = {}) => api(path, { ...opts, method: 'POST', body });
export const put = (path, body) => api(path, { method: 'PUT', body });
export const patch = (path, body) => api(path, { method: 'PATCH', body });
export const del = (path) => api(path, { method: 'DELETE' });
