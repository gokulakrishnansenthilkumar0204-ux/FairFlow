// Small DOM helpers. Text is always set with textContent (never innerHTML) so data
// from the server cannot inject markup or scripts.

export function h(tag, attrs = {}, ...children) {
  const svgTags = new Set(['svg', 'path', 'line', 'polyline', 'circle', 'g', 'text', 'rect']);
  const el = svgTags.has(tag) ? document.createElementNS('http://www.w3.org/2000/svg', tag) : document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === undefined || value === null || value === false) continue;
    if (key.startsWith('on') && typeof value === 'function') el.addEventListener(key.slice(2), value);
    else if (key === 'class') el.setAttribute('class', value);
    else if (key === 'dataset') Object.assign(el.dataset, value);
    else if (key === 'style' && typeof value === 'object') Object.assign(el.style, value);
    else if (value === true) el.setAttribute(key, '');
    else el.setAttribute(key, value);
  }
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}

const ICONS = {
  home: 'M3 11l9-7 9 7v9a1 1 0 0 1-1 1h-5v-6h-6v6H4a1 1 0 0 1-1-1z',
  shop: 'M4 9h16l-1-5H5zM5 9v11h14V9M9 20v-6h6v6',
  sack: 'M8 4h8l-1 3c3 2 5 6 5 9a4 4 0 0 1-4 4H8a4 4 0 0 1-4-4c0-3 2-7 5-9z',
  village: 'M3 20h18M5 20V10l5-4 5 4v10M15 20v-7l3-2 3 2v7M8 20v-4h4v4',
  chart: 'M4 20V4M4 20h16M8 16l4-5 3 3 5-7',
  chat: 'M4 5h16v11H9l-5 4z',
  people: 'M9 11a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM3 20c0-3 3-5 6-5s6 2 6 5M16 11a3 3 0 1 0 0-6M18 15c2 .5 3 2.5 3 5',
  log: 'M6 3h9l4 4v14H6zM9 9h6M9 13h6M9 17h4',
  key: 'M15 7a4 4 0 1 1-3.5 6L5 19.5V22h3v-2h2v-2h2l1.2-1.2A4 4 0 0 1 15 7z',
  bell: 'M6 16V11a6 6 0 1 1 12 0v5l2 2H4zM10 20a2 2 0 0 0 4 0',
  grain: 'M12 21V9M12 9c-3-1-4-4-4-6 3 0 4 3 4 6zM12 9c3-1 4-4 4-6-3 0-4 3-4 6zM12 14c-3-1-5-3-5-5 3 0 5 2 5 5zM12 14c3-1 5-3 5-5-3 0-5 2-5 5z',
  menu: 'M4 7h16M4 12h16M4 17h16',
};

export function icon(name) {
  return h('svg', { viewBox: '0 0 24 24', 'aria-hidden': 'true' }, h('path', { d: ICONS[name] || ICONS.home }));
}

// Fingerprint ridges (decorative; the real check happens on the device).
export function fingerprintSvg() {
  const ridges = [
    'M30 52c0-11 8-20 18-20s18 9 18 20',
    'M24 56c0-16 11-28 24-28s24 12 24 28',
    'M36 66c-1-4-2-9-2-14 0-8 6-14 14-14s14 6 14 14c0 6-1 12-3 17',
    'M42 72c-1-6-2-12-2-20 0-4 4-8 8-8s8 4 8 8c0 8-1 15-4 22',
    'M48 52c0 9-1 17-4 24',
    'M18 46c4-14 16-24 30-24s26 10 30 24',
    'M28 74c-3-6-4-14-4-22',
    'M72 60c0 6-1 11-3 16',
  ];
  return h('svg', { viewBox: '0 0 96 96', 'aria-hidden': 'true' }, ridges.map((d) => h('path', { d })));
}

// ---------- Feedback ----------
export function toast(message, type = 'info') {
  const holder = document.querySelector('.toasts');
  const el = h('div', { class: `toast ${type === 'error' ? 'error' : ''}`, role: type === 'error' ? 'alert' : 'status' }, message);
  holder.append(el);
  setTimeout(() => el.remove(), type === 'error' ? 7000 : 4000);
}

export async function busy(button, work) {
  const original = [...button.childNodes];
  button.disabled = true;
  button.replaceChildren(h('span', { class: 'spinner', 'aria-hidden': 'true' }), button.dataset.busy || 'Working…');
  try { return await work(); } finally {
    button.disabled = false;
    button.replaceChildren(...original);
  }
}

export function loading(rows = 4) {
  return h('div', { class: 'loading-rows', 'aria-busy': 'true', 'aria-label': 'Loading' },
    Array.from({ length: rows }, (_, i) => h('div', { class: 'skeleton', style: { width: `${90 - i * 12}%` } })));
}

export function empty(text, action) {
  return h('div', { class: 'empty' }, h('p', {}, text), action || null);
}

export function errorBox(err, retry) {
  return h('div', { class: 'empty', role: 'alert' },
    h('p', {}, err.message || String(err)),
    retry ? h('button', { class: 'btn btn-quiet btn-small', onclick: retry }, 'Try again') : null);
}

// ---------- Dialogs ----------
export function openDialog(title, content, { onClose } = {}) {
  const dialog = h('dialog', { 'aria-labelledby': 'dialog-title' },
    h('div', { class: 'dialog-body' }, h('h2', { id: 'dialog-title' }, title), content));
  dialog.addEventListener('close', () => { dialog.remove(); onClose?.(); });
  document.body.append(dialog);
  dialog.showModal();
  return dialog;
}

export function confirmDialog(title, message, actionLabel, { danger = false } = {}) {
  return new Promise((resolve) => {
    let answer = false;
    const ok = h('button', { class: `btn ${danger ? 'btn-danger' : 'btn-primary'}`, onclick: () => { answer = true; dialog.close(); } }, actionLabel);
    const dialog = openDialog(title, [
      typeof message === 'string' ? h('p', {}, message) : message,
      h('div', { class: 'form-actions' }, h('button', { class: 'btn btn-quiet', onclick: () => dialog.close() }, 'Cancel'), ok),
    ], { onClose: () => resolve(answer) });
    ok.focus();
  });
}

// A form inside a dialog; `submit(values)` runs the API call, the dialog closes on success.
export function formDialog(title, intro, fields, submitLabel, submit) {
  const form = h('form', { class: 'form-grid' }, intro ? h('p', { class: 'muted' }, intro) : null, fields);
  const button = h('button', { class: 'btn btn-primary', type: 'submit', dataset: { busy: 'Saving…' } }, submitLabel);
  form.append(h('div', { class: 'form-actions' },
    h('button', { class: 'btn btn-quiet', type: 'button', onclick: () => dialog.close() }, 'Cancel'), button));
  const dialog = openDialog(title, form);
  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    try {
      await busy(button, () => submit(Object.fromEntries(new FormData(form))));
      dialog.close();
    } catch (err) { toast(err.message, 'error'); }
  });
  form.querySelector('input, select, textarea')?.focus();
  return dialog;
}

let fieldCount = 0;
// Label + control, linked with for/id and the hint with aria-describedby.
export function field(label, control, hint) {
  if (!['INPUT', 'SELECT', 'TEXTAREA'].includes(control.tagName)) {  // a wrapper that is filled later
    return h('label', { class: 'field' }, h('span', {}, label), control, hint ? h('span', { class: 'hint' }, hint) : null);
  }
  control.id ||= `field-${++fieldCount}`;
  let hintEl = null;
  if (hint) {
    hintEl = h('span', { class: 'hint', id: `${control.id}-hint` }, hint);
    control.setAttribute('aria-describedby', hintEl.id);
  }
  return h('div', { class: 'field' }, h('label', { for: control.id }, label), control, hintEl);
}

export function select(name, options, { value, required = true, placeholder } = {}) {
  return h('select', { name, required },
    placeholder ? h('option', { value: '' }, placeholder) : null,
    options.map(([v, text]) => h('option', { value: v, selected: String(v) === String(value) }, text)));
}

// ---------- Formatting ----------
const dateFmt = new Intl.DateTimeFormat('en-IN', { day: 'numeric', month: 'short', year: 'numeric' });
const dateTimeFmt = new Intl.DateTimeFormat('en-IN', { day: 'numeric', month: 'short', hour: 'numeric', minute: '2-digit' });
export const fmtDate = (v) => (v ? dateFmt.format(new Date(v.length === 10 ? `${v}T00:00:00` : v)) : '—');
export const fmtDateTime = (v) => (v ? dateTimeFmt.format(new Date(v)) : '—');
export const fmtQty = (v) => (v === null || v === undefined ? '—' : Number(v).toLocaleString('en-IN', { maximumFractionDigits: 3 }));

export function badge(text, tone = '') {
  return h('span', { class: `badge ${tone}` }, text);
}

const STATUS_TONE = {
  active: ['Available', 'good'], seasonally_suspended: ['Seasonally suspended', 'warn'], out_of_stock: ['Out of stock', 'bad'],
  open: ['Open', 'warn'], in_progress: ['In progress', ''], resolved: ['Resolved', 'good'], rejected: ['Rejected', 'bad'],
  simulated: ['Simulated, not sent', ''], accepted: ['Sent', 'good'], failed: ['Failed', 'bad'],
  skipped_no_phone: ['No phone number', ''], disabled: ['SMS off', ''],
};
export function statusBadge(status) {
  const [text, tone] = STATUS_TONE[status] || [status, ''];
  return badge(text, tone);
}

// Stock bar: what the shop has against what its villages need for one round.
export function stockBar(available, needed) {
  const pct = needed > 0 ? Math.min(100, (available / needed) * 100) : (available > 0 ? 100 : 0);
  const tone = available <= 0 ? 'out' : needed > 0 && available < needed ? 'low' : '';
  return h('div', { class: 'bar', title: `${fmtQty(available)} in stock, ${fmtQty(needed)} needed for one round` },
    h('div', { class: 'bar-track' }, h('div', { class: `bar-fill ${tone}`, style: { width: `${Math.max(pct, available > 0 ? 3 : 0)}%` } })),
    h('div', { class: 'bar-meta' }, h('span', {}, `${fmtQty(available)} in stock`), h('span', {}, needed ? `${fmtQty(needed)} needed` : 'not required')));
}

export function table(columns, rows, emptyText = 'Nothing here yet.') {
  if (!rows.length) return empty(emptyText);
  return h('div', { class: 'table-wrap' }, h('table', {},
    h('thead', {}, h('tr', {}, columns.map((c) => h('th', { class: c.num ? 'num' : '', scope: 'col' }, c.label)))),
    h('tbody', {}, rows.map((row) => h('tr', {}, columns.map((c) => h('td', { class: c.num ? 'num' : '' }, c.render(row))))))));
}

export function panel(title, actions, ...content) {
  return h('section', { class: 'panel' },
    title || actions ? h('div', { class: 'panel-head' }, title ? h('h2', {}, title) : h('span'), actions || null) : null,
    content);
}
