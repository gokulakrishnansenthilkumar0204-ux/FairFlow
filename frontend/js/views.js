import { api, del, patch, post, put } from './api.js';
import { addThisDevice } from './passkey.js';
import {
  badge, busy, confirmDialog, empty, errorBox, field, fmtDate, fmtDateTime, fmtQty, formDialog,
  h, loading, openDialog, panel, select, statusBadge, stockBar, table, toast,
} from './ui.js';

export const NAV = {
  admin: [
    { id: 'overview', label: 'Overview', icon: 'home' },
    { id: 'places', label: 'Shops & villages', icon: 'shop' },
    { id: 'commodities', label: 'Commodities', icon: 'grain' },
    { id: 'stock', label: 'Stock', icon: 'sack' },
    { id: 'people', label: 'People & access', icon: 'people' },
    { id: 'forecast', label: 'Demand forecast', icon: 'chart' },
    { id: 'complaints', label: 'Complaints', icon: 'chat' },
    { id: 'activity', label: 'Activity log', icon: 'log' },
    { id: 'security', label: 'My passkeys', icon: 'key' },
  ],
  officer: [
    { id: 'today', label: 'Today', icon: 'home' },
    { id: 'stock', label: 'Stock', icon: 'sack' },
    { id: 'villages', label: 'Villages', icon: 'village' },
    { id: 'forecast', label: 'Demand forecast', icon: 'chart' },
    { id: 'complaints', label: 'Complaints', icon: 'chat' },
    { id: 'beneficiaries', label: 'Beneficiaries', icon: 'people' },
    { id: 'security', label: 'My passkeys', icon: 'key' },
  ],
  beneficiary: [
    { id: 'home', label: 'My ration shop', icon: 'shop' },
    { id: 'complaints', label: 'Complaints', icon: 'chat' },
    { id: 'messages', label: 'Messages', icon: 'bell' },
    { id: 'security', label: 'My passkeys', icon: 'key' },
  ],
};

export function renderPage(id, main, ctx) {
  const page = PAGES[`${ctx.user.role}:${id}`] || PAGES[id];
  const run = async () => {
    main.replaceChildren(loading(6));
    try {
      main.replaceChildren(...[].concat(await page({ ...ctx, refresh: run })).filter(Boolean));
    } catch (err) {
      main.replaceChildren(errorBox(err, run));
    }
  };
  run();
}

const head = (title, intro, action) => h('header', { class: 'page-head' },
  h('div', {}, h('h1', {}, title), intro ? h('p', {}, intro) : null), action || null);

const byId = (list) => Object.fromEntries(list.map((x) => [x.id, x]));

// Run an action from a button with a spinner, toast the result, refresh the page.
const action = (label, run, { tone = 'btn-quiet', small = true, done, refresh } = {}) => {
  const button = h('button', { class: `btn ${tone} ${small ? 'btn-small' : ''}`, type: 'button' }, label);
  button.addEventListener('click', async () => {
    try {
      const result = await busy(button, run);
      if (result === false) return;
      if (done) toast(done);
      refresh?.();
    } catch (err) { toast(err.message, 'error'); }
  });
  return button;
};

// Inline form inside a panel; resets and refreshes on success.
function inlineForm(fields, submitLabel, submit, { busyLabel = 'Saving…' } = {}) {
  const button = h('button', { class: 'btn btn-primary', type: 'submit', dataset: { busy: busyLabel } }, submitLabel);
  const form = h('form', { class: 'form-row' }, fields, h('div', {}, button));
  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    try {
      await busy(button, () => submit(Object.fromEntries(new FormData(form)), form));
    } catch (err) { toast(err.message, 'error'); }
  });
  return form;
}

// ---------- Shared shop data ----------
async function loadShop(shopId) {
  const [stock, statuses, villages, commodities, shops] = await Promise.all([
    api('/stock'), api(`/commodity-status/${shopId}`), api('/villages'), api('/commodities'), api('/shops'),
  ]);
  const shopVillages = villages.filter((v) => v.shop_id === shopId);
  const overview = await api(`/shops/${shopId}/village-overview`);  // one request for all villages
  const overviewById = byId(overview.map((o) => ({ ...o, id: o.village_id })));
  const requirements = shopVillages.map((v) => overviewById[v.id]?.requirements || []);
  const readiness = shopVillages.map((v) => overviewById[v.id] || { is_ready: false, configured: false });
  const needed = {};  // total for one full round of the shop's villages
  requirements.flat().forEach((r) => { needed[r.commodity_id] = (needed[r.commodity_id] || 0) + Number(r.quantity); });
  return {
    stock: stock.filter((s) => s.shop_id === shopId),
    status: Object.fromEntries(statuses.map((s) => [s.commodity_id, s])),
    villages: shopVillages, requirements, readiness, needed, commodities, commodityById: byId(commodities),
    shop: shops.find((s) => s.id === shopId) || { name: `Shop #${shopId}` },
  };
}

function activationCodeDialog(info) {
  const copy = h('button', { class: 'btn btn-quiet', type: 'button', onclick: async () => {
    try { await navigator.clipboard.writeText(info.activation_code); toast('Code copied'); } catch { toast('Copy failed. Select the code and copy it.', 'error'); }
  } }, 'Copy code');
  const dialog = openDialog(`Activation code for ${info.username}`, [
    h('p', {}, 'Give this code to the person. On their own phone or laptop they open FairFlow, choose “Activate account”, enter their username and this code, and touch the fingerprint sensor.'),
    h('div', { class: 'code-reveal', 'aria-label': 'Activation code' }, info.activation_code),
    h('p', { class: 'small' }, `Works once. Expires ${fmtDateTime(info.expires_at || info.activation_expires_at)}. It is shown only now; FairFlow stores only a scrambled copy.`),
    h('div', { class: 'form-actions' }, copy, h('button', { class: 'btn btn-primary', onclick: () => dialog.close() }, 'Done')),
  ]);
}

// New device: one-time code. Creating it always switches off the person's current devices.
function issueCode(user, refresh) {
  formDialog(`New activation code for ${user.username}`,
    'For a new or replacement phone. Any earlier unused code for this person stops working.',
    [h('p', { class: 'small' }, user.passkey_count
      ? 'This person’s current devices will be switched off and their open sessions ended as soon as the code is created.'
      : 'This person has no registered device yet.')],
    'Create code', async () => {
      const info = await post(`/users/${user.id}/activation-code`, {});
      setTimeout(() => activationCodeDialog(info), 0);  // after this dialog has closed
      if (info.revoked_passkeys) toast(`${info.revoked_passkeys} old device${info.revoked_passkeys === 1 ? '' : 's'} switched off`);
      refresh?.();
    });
  return false;
}

const accountBadges = (u) => [
  u.is_active === false ? badge('Disabled', 'bad') : null,
  u.role === 'beneficiary' && u.approved === false ? badge('Awaiting approval', 'warn') : null,
  u.passkey_count ? badge(`Fingerprint on ${u.passkey_count} device${u.passkey_count > 1 ? 's' : ''}`, 'good') : badge('Fingerprint not registered', 'warn'),
];

// Approve / new code / switch off devices / disable or enable, for one account.
function accountActions(u, me, refresh) {
  if (u.id === me.id) return h('span', { class: 'muted small' }, 'You');
  const out = [];
  if (u.role === 'beneficiary' && u.approved === false && u.is_active !== false) {
    out.push(action('Approve', () => post(`/users/${u.id}/approve`), { tone: 'btn-primary', done: `${u.username} approved`, refresh }));
  }
  if (u.is_active !== false) out.push(action('New activation code', () => issueCode(u, refresh)));
  if (u.passkey_count && u.is_active !== false) {
    out.push(action('Switch off devices', async () => {
      const ok = await confirmDialog(`Switch off ${u.username}’s devices?`, 'Every phone or laptop registered to this person stops signing in, and their open sessions end. Give them a new activation code to register a new device.', 'Switch off devices', { danger: true });
      if (!ok) return false;
      await del(`/users/${u.id}/passkeys`);
    }, { tone: 'btn-danger', done: 'Devices switched off', refresh }));
  }
  if (u.is_active === false && me.role !== 'admin') {
    out.push(h('span', { class: 'muted small' }, 'Only an administrator can enable this account'));
  } else out.push(u.is_active === false
    ? action('Enable', () => post(`/users/${u.id}/reactivate`), { done: `${u.username} can sign in again`, refresh })
    : action('Disable', async () => {
      const ok = await confirmDialog(`Disable ${u.username}?`, 'They are signed out now and cannot sign in until the account is enabled again. Nothing is deleted.', 'Disable account', { danger: true });
      if (!ok) return false;
      await post(`/users/${u.id}/deactivate`);
    }, { tone: 'btn-danger', done: `${u.username} disabled`, refresh }));
  return h('div', { class: 'actions' }, out);
}

// ---------- Readiness & distribution ----------
async function readinessDialog(village, { canDistribute, refresh }) {
  const r = await api(`/villages/${village.id}/readiness`);
  const rows = [
    ...r.seasonally_suspended.map((c) => [c, 'warn', 'Seasonally suspended']),
    ...r.missing_commodities.map((c) => [c, 'bad', 'Not in stock']),
    ...r.insufficient_stock.map((c) => [c, 'bad', `Only ${fmtQty(c.available_quantity)} in stock`]),
  ];
  const content = [
    r.is_ready
      ? h('p', {}, `Every required commodity is in stock. Distributing will take the quantities below out of stock and record today’s round.`)
      : h('p', {}, r.reason || 'This village cannot be served yet:'),
  ];
  if (rows.length) {
    content.push(h('ul', { class: 'checklist' }, rows.map(([c, tone, text]) => h('li', { class: tone },
      h('span', {}, c.commodity_name), h('span', {}, `needs ${fmtQty(c.required_quantity)} · ${text}`)))));
  }
  if (r.is_ready) {
    const reqs = await api(`/villages/${village.id}/requirements`);
    content.push(h('ul', { class: 'checklist' }, reqs.map((q) => h('li', {}, h('span', {}, q.commodity_name), h('span', { class: 'num' }, fmtQty(q.quantity))))));
  }
  const buttons = h('div', { class: 'form-actions' }, h('button', { class: 'btn btn-quiet', onclick: () => dialog.close() }, 'Close'));
  if (canDistribute && r.is_ready) {
    const go = h('button', { class: 'btn btn-primary', dataset: { busy: 'Recording…' } }, `Distribute to ${village.name}`);
    go.addEventListener('click', async () => {
      try {
        const res = await busy(go, () => post('/villages/mark-distributed', { village_id: village.id }));
        dialog.close();
        toast(`Distributed to ${res.village_name}. Stock updated.`);
        refresh?.();
      } catch (err) { toast(err.message, 'error'); }
    });
    buttons.append(go);
  }
  content.push(buttons);
  const dialog = openDialog(`${village.name}: ${r.is_ready ? 'ready to distribute' : 'not ready'}`, content);
}

function requirementsDialog(village, commodities, current, refresh) {
  const now = Object.fromEntries(current.map((r) => [r.commodity_id, r.quantity]));
  const inputs = commodities.map((c) => field(c.name,
    h('input', { name: String(c.id), type: 'number', min: '0', step: '0.001', value: now[c.id] ?? '', placeholder: 'Not required' })));
  formDialog(`What ${village.name} needs per round`,
    'Quantity of each commodity the village receives in one distribution. Clear a box (or enter 0) to stop giving that commodity.',
    h('div', { class: 'form-row' }, inputs), 'Save quantities', async (values) => {
      const changes = Object.entries(values).filter(([id, v]) => v !== '' && Number(v) > 0 && Number(v) !== Number(now[id]));
      const removals = Object.entries(values).filter(([id, v]) => (v.trim() === '' || Number(v) === 0) && now[id] !== undefined);
      for (const [id, v] of changes) await post(`/villages/${village.id}/requirements`, { commodity_id: Number(id), quantity: Number(v) });
      for (const [id] of removals) await del(`/villages/${village.id}/requirements/${id}`);
      const total = changes.length + removals.length;
      toast(total ? `Saved ${total} change${total === 1 ? '' : 's'} for ${village.name}` : 'No changes');
      refresh();
    });
}

// ---------- Forecast chart ----------
const monthName = (ym, opts = { month: 'short' }) => {
  const [y, m] = ym.split('-').map(Number);
  return new Date(y, m - 1).toLocaleString('en-IN', opts);
};
const monthLabel = (p) => (p.partial ? `${monthName(p.month)} (so far)` : monthName(p.month));

function forecastChart(f) {
  const W = 640, H = 260, L = 52, R = 28, T = 24, B = 36;
  const values = f.history.map((p) => p.quantity);
  const max = Math.max(...values, f.predicted_next_month_demand, f.current_stock) * 1.15 || 1;
  const n = values.length;
  const x = (i) => L + (i * (W - L - R)) / n;
  const y = (v) => T + (H - T - B) * (1 - v / max);
  const ticks = [...new Set([0, 0.25, 0.5, 0.75, 1].map((t) => (max >= 20 ? Math.round(max * t) : Number((max * t).toFixed(2)))))];
  // Trend line and forecast use complete months only; the running month is a lone hollow dot.
  const done = f.history.map((p, i) => [p, i]).filter(([p]) => !p.partial).map(([, i]) => i);
  const last = done.at(-1);
  const pts = done.map((i) => `${x(i)},${y(values[i])}`).join(' ');
  return h('svg', { class: 'chart', viewBox: `0 0 ${W} ${H}`, role: 'img',
    'aria-label': `${f.commodity_name}: monthly distribution and forecast of ${fmtQty(f.predicted_next_month_demand)} for ${monthName(f.forecast_month, { month: 'long', year: 'numeric' })}` },
  h('g', { class: 'grid' }, ticks.map((t) => h('line', { x1: L, x2: W - R, y1: y(t), y2: y(t) }))),
  h('g', { class: 'axis' },
    ticks.map((t) => h('text', { x: L - 8, y: y(t) + 4, 'text-anchor': 'end' }, fmtQty(t))),
    f.history.map((p, i) => h('text', { x: x(i), y: H - 12, 'text-anchor': 'middle' }, monthLabel(p))),
    h('text', { x: x(n), y: H - 12, 'text-anchor': 'middle' }, monthName(f.forecast_month))),
  h('polyline', { class: 'series', points: pts }),
  h('line', { class: 'forecast', x1: x(last), y1: y(values[last]), x2: x(n), y2: y(f.predicted_next_month_demand) }),
  values.map((v, i) => h('circle', { class: f.history[i].partial ? 'dot partial' : 'dot', cx: x(i), cy: y(v), r: 4 })),
  h('circle', { class: 'forecast-dot', cx: x(n), cy: y(f.predicted_next_month_demand), r: 6 }),
  h('text', { class: 'value-label', x: x(n), y: y(f.predicted_next_month_demand) - 12, 'text-anchor': 'end' }, fmtQty(f.predicted_next_month_demand)));
}

const RECOMMENDATION = {
  RESTOCK: ['Restock', 'warn'], SUFFICIENT_STOCK: ['Enough stock', 'good'], STOCK_MATCHES_FORECAST: ['Stock matches forecast', 'good'],
};

async function forecastPage({ user, refresh }) {
  let shopId = user.shop_id;
  let shopPicker = null;
  if (user.role === 'admin') {
    const shops = await api('/shops');
    if (!shops.length) return [head('Demand forecast'), panel(null, null, empty('Add a shop first.'))];
    shopId = Number(sessionStorage.getItem('fairflow.forecastShop')) || shops[0].id;
    if (!shops.some((s) => s.id === shopId)) shopId = shops[0].id;
    shopPicker = field('Shop', select('shop', shops.map((s) => [s.id, s.name]), { value: shopId }));
    shopPicker.querySelector('select').addEventListener('change', (e) => {
      try { sessionStorage.setItem('fairflow.forecastShop', e.target.value); } catch { /* ignore */ }
      refresh();
    });
  }
  const [data, commodities] = await Promise.all([api(`/forecast/${shopId}`), api('/commodities')]);
  const chartHolder = h('div');
  const list = h('div', { class: 'forecast-list' });
  const show = (f, button) => {
    list.querySelectorAll('[aria-pressed]').forEach((b) => b.setAttribute('aria-pressed', 'false'));
    button.setAttribute('aria-pressed', 'true');
    chartHolder.replaceChildren(
      h('div', { class: 'panel-head' }, h('h2', {}, f.commodity_name), badge(...RECOMMENDATION[f.recommendation])),
      forecastChart(f),
      h('div', { class: 'stats' },
        stat(fmtQty(f.predicted_next_month_demand), `forecast for ${monthName(f.forecast_month, { month: 'long' })}`),
        stat(fmtQty(f.current_stock), 'in stock now'),
        stat(fmtQty(f.reorder_quantity), 'to reorder')),
      h('p', { class: 'muted small' }, `Linear trend over ${f.months_used} complete months (${f.trend_slope >= 0 ? '+' : ''}${fmtQty(f.trend_slope)} per month), up to last month. Months with no distribution count as zero. The current month is shown (hollow dot) but not used until it ends. This is a simple trend estimate, not a validated prediction.`));
  };
  data.forecasts.forEach((f, i) => {
    const b = h('button', { class: 'forecast-item', type: 'button', 'aria-pressed': 'false' },
      h('strong', {}, f.commodity_name), badge(...RECOMMENDATION[f.recommendation]),
      h('span', { class: 'muted small' }, `Forecast ${fmtQty(f.predicted_next_month_demand)} · stock ${fmtQty(f.current_stock)}`));
    b.addEventListener('click', () => show(f, b));
    list.append(b);
    if (i === 0) queueMicrotask(() => show(f, b));
  });

  const out = [head('Demand forecast', 'Next month’s demand from past distributions, and how much to reorder.', shopPicker)];
  if (!data.forecasts.length) {
    out.push(panel(null, null, empty('No commodity has at least two complete months of distribution history yet. History builds up as villages are served; officers can also add past records below.')));
  } else {
    out.push(h('div', { class: 'grid-2' }, h('section', { class: 'panel' }, chartHolder), panel('Commodities', null, list)));
  }
  if (user.role === 'officer') {
    out.push(panel('Add a past distribution record', null,
      h('p', { class: 'muted', style: { marginBottom: '1rem' } }, 'For loading history from before FairFlow. Normal distributions are recorded automatically.'),
      inlineForm([
        field('Commodity', select('commodity_id', commodities.map((c) => [c.id, c.name]), { placeholder: 'Choose' })),
        field('Quantity', h('input', { name: 'quantity', type: 'number', min: '0.001', step: '0.001', required: true })),
        field('Date', h('input', { name: 'distribution_date', type: 'date', required: true, max: localToday() })),
      ], 'Add record', async (v) => {
        await post('/forecast/history', { shop_id: shopId, commodity_id: Number(v.commodity_id), quantity: Number(v.quantity), distribution_date: v.distribution_date });
        toast('Record added');
        refresh();
      })));
  }
  return out;
}

const stat = (value, name) => h('div', { class: 'stat' }, h('div', { class: 'value num' }, value), h('div', { class: 'name' }, name));

// ---------- Complaints (staff) ----------
const CATEGORY = { quality: 'Quality', quantity: 'Short quantity', availability: 'Not available', behaviour: 'Staff behaviour', other: 'Other' };

async function staffComplaints({ user, refresh }) {
  const [complaints, users] = await Promise.all([api('/complaints'), api('/users')]);
  const people = byId(users);
  const filter = sessionStorage.getItem('fairflow.complaintFilter') || 'active';
  const shown = complaints.filter((c) => (filter === 'active' ? ['open', 'in_progress'].includes(c.status) : filter === 'closed' ? ['resolved', 'rejected'].includes(c.status) : true));
  const tabs = h('div', { class: 'tabs', role: 'tablist' }, [['active', 'Needs action'], ['closed', 'Closed'], ['all', 'All']].map(([id, label]) =>
    h('button', { class: 'tab', role: 'tab', 'aria-selected': String(id === filter), onclick: () => {
      try { sessionStorage.setItem('fairflow.complaintFilter', id); } catch { /* ignore */ }
      refresh();
    } }, label)));

  const update = (c) => formDialog(`Complaint #${c.id}`, c.description, [
    field('New status', select('status', [['in_progress', 'In progress'], ['resolved', 'Resolved'], ['rejected', 'Rejected']], { value: c.status === 'open' ? 'in_progress' : c.status })),
    field('Note to the beneficiary', h('textarea', { name: 'resolution_note', maxlength: 1000 }), 'Required when resolving or rejecting. The beneficiary sees it under their complaint; the SMS tells them the new status.'),
  ], 'Update complaint', async (v) => {
    await patch(`/complaints/${c.id}/status`, { status: v.status, resolution_note: v.resolution_note || null });
    toast(`Complaint #${c.id} updated`);
    refresh();
  });

  return [
    head('Complaints', user.role === 'officer' ? 'Complaints from beneficiaries of your shop.' : 'Complaints across all shops.'),
    panel(null, tabs, shown.length ? h('ul', { class: 'timeline' }, shown.map((c) => h('li', {},
      statusBadge(c.status),
      h('div', { class: 'body' },
        h('strong', {}, `${CATEGORY[c.category] || c.category} · #${c.id}`),
        h('p', {}, c.description),
        c.resolution_note ? h('p', { class: 'muted small' }, `Note: ${c.resolution_note}`) : null,
        h('span', { class: 'muted small' }, `${people[c.beneficiary_id]?.username || `Beneficiary #${c.beneficiary_id}`} · shop #${c.shop_id} · ${fmtDateTime(c.created_at)}`)),
      ['open', 'in_progress'].includes(c.status) ? h('button', { class: 'btn btn-quiet btn-small', onclick: () => update(c) }, 'Update') : h('span')))) : empty(filter === 'active' ? 'No complaints need action.' : 'No complaints here.')),
  ];
}

// ---------- Passkeys (all roles) ----------
async function securityPage({ refresh }) {
  const keys = await api('/auth/passkeys');
  return [
    head('My passkeys', 'Each passkey lets one of your devices sign you in with its fingerprint sensor.',
      action('Add this device', async () => { await addThisDevice(); }, { tone: 'btn-primary', small: false, done: 'This device can now sign you in', refresh })),
    panel(null, null, table([
      { label: 'Device', render: (k) => h('strong', {}, k.device_label) },
      { label: 'Added', render: (k) => fmtDate(k.created_at) },
      { label: 'Last used', render: (k) => (k.last_used_at ? fmtDateTime(k.last_used_at) : 'Not yet') },
      { label: '', render: (k) => h('div', { class: 'actions' }, keys.length > 1
        ? action('Remove', async () => {
          const ok = await confirmDialog(`Remove ${k.device_label}?`, 'That device will no longer be able to sign in to your account.', 'Remove passkey', { danger: true });
          if (!ok) return false;
          await del(`/auth/passkeys/${k.id}`);
        }, { tone: 'btn-danger', done: 'Passkey removed', refresh })
        : h('span', { class: 'muted small' }, 'Only passkey')) },
    ], keys, 'No passkeys yet.')),
    panel('What FairFlow stores', null, h('p', {},
      'Your fingerprint is checked by your own device and never leaves it. FairFlow keeps only a public key for each device, which can check a sign-in but cannot be used to sign in. If you lose a device, remove it here from another device. If you have no other device, ask your shop officer or the administrator: they switch the lost one off and give you a code for the new one.')),
  ];
}

// ---------- Admin pages ----------
async function adminOverview() {
  const [shops, villages, commodities, users, complaints, logs] = await Promise.all([
    api('/shops'), api('/villages'), api('/commodities'), api('/users'), api('/complaints'), api('/audit-logs?limit=8'),
  ]);
  const people = byId(users);
  const notActivated = users.filter((u) => !u.passkey_count && u.is_active !== false).length;
  const pending = users.filter((u) => u.role === 'beneficiary' && u.approved === false && u.is_active !== false).length;
  return [
    head('Overview', 'The state of every Fair Price Shop in FairFlow.'),
    h('section', { class: 'panel' }, h('div', { class: 'stats' },
      stat(shops.length, 'shops'), stat(villages.length, 'villages'), stat(commodities.length, 'commodities'),
      stat(users.length, 'people'), stat(complaints.filter((c) => ['open', 'in_progress'].includes(c.status)).length, 'open complaints'))),
    notActivated ? h('div', { class: 'banner', role: 'status' }, `${notActivated} ${notActivated === 1 ? 'person has' : 'people have'} not registered a fingerprint yet. Issue activation codes under People & access.`) : null,
    pending ? h('div', { class: 'banner', role: 'status' }, `${pending} new beneficiar${pending === 1 ? 'y is' : 'ies are'} waiting for approval by their shop officer (or by you, under People & access).`) : null,
    h('div', { class: 'grid-2' },
      panel('Shops', h('a', { href: '#/places', class: 'btn btn-quiet btn-small' }, 'Manage'), table([
        { label: 'Shop', render: (s) => h('strong', {}, s.name) },
        { label: 'Location', render: (s) => s.location },
        { label: 'Villages', num: true, render: (s) => villages.filter((v) => v.shop_id === s.id).length },
      ], shops, 'No shops yet.')),
      panel('Recent activity', h('a', { href: '#/activity', class: 'btn btn-quiet btn-small' }, 'Full log'), logs.length
        ? h('ul', { class: 'timeline' }, logs.map((l) => h('li', {}, h('span', { class: 'when' }, fmtDateTime(l.created_at)),
          h('div', { class: 'body' }, h('span', {}, describeAction(l)), h('span', { class: 'muted small' }, people[l.actor_id]?.username || 'system')), h('span'))))
        : empty('Nothing has happened yet.'))),
  ];
}

// Today in the browser's own time zone (toISOString would give the UTC date).
const localToday = () => { const d = new Date(); return new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 10); };

const ACTIONS = {
  'user.created': 'Account created', 'shop.created': 'Shop added', 'village.created': 'Village added',
  'commodity.created': 'Commodity added', 'stock.added': 'Stock added', 'stock.updated': 'Stock corrected',
  'stock.deleted': 'Stock removed', 'commodity_status.set': 'Commodity status changed',
  'village_requirement.set': 'Village quantity set', 'distribution.completed': 'Village served',
  'history.added': 'Past record added', 'beneficiary.verification': 'Beneficiary verified at counter',
  'complaint.created': 'Complaint filed', 'complaint.status_changed': 'Complaint updated',
  'auth.login': 'Signed in with fingerprint', 'auth.passkey_added': 'Passkey registered',
  'auth.passkey_removed': 'Passkey removed', 'auth.activation_code_issued': 'Activation code issued',
  'auth.passkeys_revoked': 'Devices switched off', 'user.approved': 'Account approved',
  'user.unapproved': 'Approval withdrawn', 'user.deactivated': 'Account disabled',
  'user.reactivated': 'Account enabled', 'user.updated': 'Account role or shop changed',
  'village_requirement.removed': 'Village quantity removed',
};
const describeAction = (l) => `${ACTIONS[l.action] || l.action}${l.entity_id ? ` (${l.entity} #${l.entity_id})` : ''}`;

async function adminPlaces({ refresh }) {
  const [shops, villages, commodities] = await Promise.all([api('/shops'), api('/villages'), api('/commodities')]);
  const shopById = byId(shops);
  return [
    head('Shops & villages', 'Each village is served by one shop. Set what each village receives per round.'),
    panel('Shops', null,
      table([
        { label: 'Shop', render: (s) => h('strong', {}, s.name) },
        { label: 'Location', render: (s) => s.location },
        { label: 'ID', num: true, render: (s) => s.id },
      ], shops, 'No shops yet. Add the first one below.'),
      h('div', { style: { marginTop: '1rem' } }, inlineForm([
        field('Shop name', h('input', { name: 'name', required: true, maxlength: 100, placeholder: 'FPS Guindy' })),
        field('Location', h('input', { name: 'location', required: true, maxlength: 200, placeholder: 'Guindy, Chennai' })),
      ], 'Add shop', async (v) => { await post('/shops', v); toast(`Added ${v.name}`); refresh(); }))),
    panel('Villages', null,
      table([
        { label: 'Village', render: (v) => h('strong', {}, v.name) },
        { label: 'Shop', render: (v) => shopById[v.shop_id]?.name || `#${v.shop_id}` },
        { label: 'Last served', render: (v) => fmtDate(v.last_distribution_date) },
        { label: '', render: (v) => h('div', { class: 'actions' },
          action('Quantities', async () => requirementsDialog(v, commodities, await api(`/villages/${v.id}/requirements`), refresh)),
          action('Readiness', () => readinessDialog(v, { canDistribute: false }))) },
      ], villages, 'No villages yet.'),
      shops.length ? h('div', { style: { marginTop: '1rem' } }, inlineForm([
        field('Village name', h('input', { name: 'name', required: true, maxlength: 100 })),
        field('Served by', select('shop_id', shops.map((s) => [s.id, s.name]), { placeholder: 'Choose shop' })),
      ], 'Add village', async (v) => { await post('/villages', { name: v.name, shop_id: Number(v.shop_id) }); toast(`Added ${v.name}`); refresh(); })) : null),
  ];
}

async function adminCommodities({ refresh }) {
  const commodities = await api('/commodities');
  return [
    head('Commodities', 'Items distributed through the shops.'),
    panel(null, null,
      table([{ label: 'Commodity', render: (c) => h('strong', {}, c.name) }, { label: 'ID', num: true, render: (c) => c.id }], commodities, 'No commodities yet.'),
      h('div', { style: { marginTop: '1rem' } }, inlineForm([
        field('Name', h('input', { name: 'name', required: true, maxlength: 100, placeholder: 'Toor dal' })),
      ], 'Add commodity', async (v) => { await post('/commodities', v); toast(`Added ${v.name}`); refresh(); }))),
  ];
}

async function adminStock({ refresh }) {
  const [stock, shops, commodities] = await Promise.all([api('/stock'), api('/shops'), api('/commodities')]);
  const c = byId(commodities);
  return [
    head('Stock', 'What every shop holds. Officers add and correct stock for their own shop.'),
    ...shops.map((s) => panel(s.name, null, table([
      { label: 'Commodity', render: (r) => h('strong', {}, c[r.commodity_id]?.name || `#${r.commodity_id}`) },
      { label: 'Quantity', num: true, render: (r) => h('span', { class: 'num' }, fmtQty(r.quantity)) },
      { label: '', render: (r) => h('div', { class: 'actions' }, action('Remove', async () => {
        const ok = await confirmDialog(`Remove ${c[r.commodity_id]?.name} from ${s.name}?`, 'This deletes the stock record. Use it only to fix a wrong entry.', 'Remove stock', { danger: true });
        if (!ok) return false;
        await del(`/stock/${r.id}`);
      }, { tone: 'btn-danger', done: 'Stock removed', refresh })) },
    ], stock.filter((r) => r.shop_id === s.id), 'No stock recorded.'))),
  ];
}

async function adminPeople({ user: me, refresh }) {
  const [users, shops] = await Promise.all([api('/users'), api('/shops')]);
  const shopById = byId(shops);
  const roleSelect = select('role', [['officer', 'Shop officer'], ['admin', 'Administrator']]);
  const shopSelect = select('shop_id', shops.map((s) => [s.id, s.name]), { placeholder: 'Choose shop', required: false });
  const ROLE = { admin: 'Administrator', officer: 'Shop officer', beneficiary: 'Beneficiary' };
  const editRole = (u) => formDialog(`Role and shop for ${u.username}`, 'They are signed out and sign in again with their fingerprint to get the new access.', [
    field('Role', select('role', Object.entries(ROLE), { value: u.role })),
    field('Shop', select('shop_id', shops.map((s) => [s.id, s.name]), { value: u.shop_id, placeholder: 'No shop', required: false }), 'Officers and beneficiaries need a shop; administrators have none.'),
  ], 'Save', async (v) => {
    await patch(`/users/${u.id}`, { role: v.role, shop_id: v.role === 'admin' || !v.shop_id ? null : Number(v.shop_id) });
    toast(`${u.username} updated`);
    refresh();
  });
  return [
    head('People & access', 'Staff sign in with their fingerprint. New staff get a one-time activation code to register it.'),
    panel('Add staff', null, inlineForm([
      field('Username', h('input', { name: 'username', required: true, minlength: 3, maxlength: 50 })),
      field('Role', roleSelect),
      field('Shop', shopSelect, 'Required for officers'),
      field('Mobile', h('input', { name: 'phone', type: 'tel', pattern: '\\+?[0-9]{10,15}' })),
    ], 'Create account', async (v, form) => {
      const created = await post('/admin/users', {
        username: v.username.trim(), role: v.role, shop_id: v.role === 'officer' && v.shop_id ? Number(v.shop_id) : null, phone: v.phone || null,
      });
      form.reset();
      activationCodeDialog({ username: created.username, activation_code: created.activation_code, expires_at: created.activation_expires_at });
      refresh();
    })),
    panel('Everyone', null, table([
      { label: 'Username', render: (u) => h('strong', {}, u.username) },
      { label: 'Role', render: (u) => [ROLE[u.role], u.id !== me.id ? h('button', { class: 'btn btn-quiet btn-small', onclick: () => editRole(u) }, 'Change') : null] },
      { label: 'Shop', render: (u) => (u.shop_id ? shopById[u.shop_id]?.name || `#${u.shop_id}` : '—') },
      { label: 'Status', render: (u) => h('div', { class: 'badges' }, accountBadges(u)) },
      { label: '', render: (u) => accountActions(u, me, refresh) },
    ], users)),
  ];
}

async function adminActivity() {
  const [logs, users] = await Promise.all([api('/audit-logs?limit=300'), api('/users')]);
  const people = byId(users);
  return [
    head('Activity log', 'Every change and sign-in, newest first. Passwords, codes and tokens are never recorded.'),
    panel(null, null, table([
      { label: 'When', render: (l) => fmtDateTime(l.created_at) },
      { label: 'Who', render: (l) => people[l.actor_id]?.username || 'system' },
      { label: 'What', render: (l) => describeAction(l) },
      { label: 'Details', render: (l) => h('span', { class: 'muted small' }, Object.entries(l.details || {}).map(([k, v]) => `${k}: ${v}`).join(', ')) },
    ], logs, 'No activity yet.')),
  ];
}

// ---------- Officer pages ----------
async function officerToday({ user, refresh }) {
  const [shop, next, complaints] = await Promise.all([
    loadShop(user.shop_id), api(`/shops/${user.shop_id}/next-village`), api('/complaints'),
  ]);
  const openComplaints = complaints.filter((c) => ['open', 'in_progress'].includes(c.status));
  const nextVillage = next.village_id ? shop.villages.find((v) => v.id === next.village_id) : null;
  const short = shop.commodities.filter((c) => shop.needed[c.id] && (shop.stock.find((s) => s.commodity_id === c.id)?.quantity || 0) < shop.needed[c.id]);

  const callout = h('section', { class: 'callout' },
    h('span', { class: 'label' }, 'Next village to serve'),
    nextVillage
      ? [h('div', { class: 'big' }, nextVillage.name),
        h('p', {}, nextVillage.last_distribution_date ? `Last served ${fmtDate(nextVillage.last_distribution_date)}` : 'Not served yet'),
        h('button', { class: 'btn btn-primary', onclick: () => readinessDialog(nextVillage, { canDistribute: true, refresh }).catch((e) => toast(e.message, 'error')) }, 'Review and distribute')]
      : [h('div', { class: 'big' }, 'None ready'), h('p', {}, 'No village has all its commodities in stock. Check stock and villages.')]);

  return [
    head('Today', `${shop.shop.name}: what to do at the counter.`),
    h('div', { class: 'grid-2' },
      callout,
      panel('Needs attention', null, h('ul', { class: 'checklist' },
        short.length ? short.map((c) => h('li', { class: 'warn' }, h('span', {}, c.name), h('span', {}, `short for a full round`))) : h('li', {}, h('span', {}, 'Stock covers a full round of villages'), badge('OK', 'good')),
        Object.values(shop.status).filter((s) => s.status !== 'active').map((s) => h('li', { class: 'warn' }, h('span', {}, shop.commodityById[s.commodity_id]?.name), statusBadge(s.status))),
        h('li', { class: openComplaints.length ? 'warn' : '' }, h('a', { href: '#/complaints' }, `${openComplaints.length} open complaint${openComplaints.length === 1 ? '' : 's'}`))))),
    panel('Stock against one full round', h('a', { href: '#/stock', class: 'btn btn-quiet btn-small' }, 'Manage stock'), table([
      { label: 'Commodity', render: (c) => h('strong', {}, c.name) },
      { label: 'Stock vs need', render: (c) => stockBar(Number(shop.stock.find((s) => s.commodity_id === c.id)?.quantity || 0), shop.needed[c.id] || 0) },
      { label: 'Status', render: (c) => statusBadge(shop.status[c.id]?.status || 'active') },
    ], shop.commodities)),
  ];
}

async function officerStock({ user, refresh }) {
  const shop = await loadShop(user.shop_id);
  const rows = shop.stock.map((s) => ({ ...s, name: shop.commodityById[s.commodity_id]?.name }));
  const adjust = (r) => formDialog(`Correct ${r.name} stock`, 'Set the exact quantity counted in the shop.', [
    field('Quantity in stock', h('input', { name: 'quantity', type: 'number', min: '0.001', step: '0.001', required: true, value: r.quantity })),
  ], 'Save quantity', async (v) => { await put(`/stock/${r.id}`, { quantity: Number(v.quantity) }); toast(`${r.name} set to ${fmtQty(v.quantity)}`); refresh(); });
  const setStatus = (c) => formDialog(`${c.name} availability`, null, [
    field('Status', select('status', [['active', 'Available'], ['seasonally_suspended', 'Seasonally suspended'], ['out_of_stock', 'Out of stock']], { value: shop.status[c.id]?.status || 'active' })),
    field('Reason', h('input', { name: 'reason', maxlength: 500, value: shop.status[c.id]?.reason || '' }), 'Required unless available. Beneficiaries can see it.'),
  ], 'Save status', async (v) => {
    await post('/commodity-status', { shop_id: user.shop_id, commodity_id: c.id, status: v.status, reason: v.status === 'active' ? null : v.reason || null });
    toast(`${c.name} status saved`);
    refresh();
  });

  return [
    head('Stock', 'Receive deliveries, correct counts and mark items unavailable.'),
    panel('Receive a delivery', null, inlineForm([
      field('Commodity', select('commodity_id', shop.commodities.map((c) => [c.id, c.name]), { placeholder: 'Choose' })),
      field('Quantity received', h('input', { name: 'quantity', type: 'number', min: '0.001', step: '0.001', required: true })),
    ], 'Add to stock', async (v, form) => {
      await post('/stock', { shop_id: user.shop_id, commodity_id: Number(v.commodity_id), quantity: Number(v.quantity) });
      form.reset();
      toast('Stock added');
      refresh();
    })),
    panel('In the shop', null, table([
      { label: 'Commodity', render: (c) => h('strong', {}, c.name) },
      { label: 'Stock vs need', render: (c) => stockBar(Number(shop.stock.find((s) => s.commodity_id === c.id)?.quantity || 0), shop.needed[c.id] || 0) },
      { label: 'Status', render: (c) => statusBadge(shop.status[c.id]?.status || 'active') },
      { label: '', render: (c) => {
        const row = rows.find((r) => r.commodity_id === c.id);
        return h('div', { class: 'actions' },
          row ? h('button', { class: 'btn btn-quiet btn-small', onclick: () => adjust(row) }, 'Correct') : null,
          h('button', { class: 'btn btn-quiet btn-small', onclick: () => setStatus(c) }, 'Availability'),
          row ? action('Remove', async () => {
            const ok = await confirmDialog(`Remove ${c.name} stock?`, 'This deletes the stock record. Use it only to fix a wrong entry.', 'Remove stock', { danger: true });
            if (!ok) return false;
            await del(`/stock/${row.id}`);
          }, { tone: 'btn-danger', done: 'Stock removed', refresh }) : null);
      } },
    ], shop.commodities, 'No commodities exist yet. Ask the administrator to add them.')),
  ];
}

async function officerVillages({ user, refresh }) {
  const shop = await loadShop(user.shop_id);
  const readiness = shop.readiness;
  return [
    head('Villages', 'Villages served by your shop, in the order they are due.'),
    panel(null, null, table([
      { label: 'Village', render: (v) => h('strong', {}, v.name) },
      { label: 'Last served', render: (v) => fmtDate(v.last_distribution_date) },
      { label: 'Commodities', num: true, render: (v) => shop.requirements[shop.villages.indexOf(v)].length },
      { label: 'Readiness', render: (v) => {
        const r = readiness[shop.villages.indexOf(v)];
        if (r.is_ready) return badge('Ready', 'good');
        return badge(r.configured ? 'Not ready' : 'No quantities set', r.configured ? 'warn' : '');
      } },
      { label: '', render: (v) => h('div', { class: 'actions' },
        h('button', { class: 'btn btn-quiet btn-small', onclick: () => requirementsDialog(v, shop.commodities, shop.requirements[shop.villages.indexOf(v)], refresh) }, 'Quantities'),
        action(readiness[shop.villages.indexOf(v)].is_ready ? 'Distribute' : 'Check', () => readinessDialog(v, { canDistribute: true, refresh }),
          { tone: readiness[shop.villages.indexOf(v)].is_ready ? 'btn-primary' : 'btn-quiet' })) },
    ], [...shop.villages].sort((a, b) => (a.last_distribution_date || '') .localeCompare(b.last_distribution_date || '')), 'No villages are assigned to your shop yet.')),
  ];
}

async function officerBeneficiaries({ user, config, refresh }) {
  const users = await api('/users');
  const out = [
    head('Beneficiaries', 'People registered at your shop. Approve new registrations after checking their ration card. If someone loses their phone, switch it off and give them a new activation code.'),
    users.some((u) => u.approved === false && u.is_active !== false)
      ? h('div', { class: 'banner', role: 'status' }, 'New registrations are waiting for your approval. They cannot see shop details or file complaints until you approve them.') : null,
    panel(null, null, table([
      { label: 'Username', render: (u) => h('strong', {}, u.username) },
      { label: 'Mobile', render: (u) => u.phone || '—' },
      { label: 'Status', render: (u) => h('div', { class: 'badges' }, accountBadges(u)) },
      { label: '', render: (u) => accountActions(u, user, refresh) },
    ], [...users].sort((a, b) => Number(a.approved !== false) - Number(b.approved !== false)), 'No beneficiaries have registered at your shop yet.')),
  ];
  if (config.biometric_provider && config.biometric_provider !== 'disabled') {
    const result = h('div', { role: 'status' });
    out.push(panel('Verify a beneficiary at the counter', null,
      config.biometric_provider === 'mock'
        ? h('div', { class: 'banner' }, 'Simulation only: no real biometric provider is connected. Results are labelled as simulated and prove nothing about identity.')
        : null,
      inlineForm([
        field('Beneficiary', select('beneficiary_id', users.filter((u) => u.is_active !== false).map((u) => [u.id, u.username]), { placeholder: 'Choose' })),
        field('Capture from the shop device', h('input', { name: 'capture_token', required: true, maxlength: 8192 }), 'Filled in by the certified fingerprint device.'),
      ], 'Verify', async (v) => {
        const r = await post(`/beneficiaries/${v.beneficiary_id}/biometric-verification`, { capture_token: v.capture_token });
        result.replaceChildren(h('p', {}, badge(r.verified ? 'Match' : 'No match', r.verified ? 'good' : 'bad'), ` ${r.simulated ? '(simulated) ' : ''}reference ${r.provider_reference}`));
      }), result));
  }
  return out;
}

// ---------- Beneficiary pages ----------
// The officer may approve while the person is signed in: re-check before showing the notice.
async function stillPending(user) {
  if (user.approved !== false) return false;
  const fresh = await api('/me');
  Object.assign(user, fresh);  // ctx.user is the app's signed-in user object
  if (fresh.approved !== false) window.dispatchEvent(new CustomEvent('fairflow:user-updated'));  // redraw menu label
  return fresh.approved === false;
}

const awaitingApproval = (title) => [head(title), panel(null, null, empty('Your fingerprint is registered. Your shop officer still needs to approve your account (usually after checking your ration card). Then you will see your shop’s stock, the next distribution and be able to file complaints.'))];

async function beneficiaryHome({ user }) {
  if (await stillPending(user)) return awaitingApproval('My ration shop');
  if (!user.shop_id) return [head('My ration shop'), panel(null, null, empty('Your account is not linked to a shop yet. Ask your shop officer.'))];
  const [shops, stock, statuses, villages, commodities, next] = await Promise.all([
    api('/shops'), api('/stock'), api(`/commodity-status/${user.shop_id}`), api('/villages'), api('/commodities'),
    api(`/shops/${user.shop_id}/next-village`),
  ]);
  const shop = shops[0];
  const status = Object.fromEntries(statuses.map((s) => [s.commodity_id, s]));
  const qty = Object.fromEntries(stock.map((s) => [s.commodity_id, s.quantity]));
  return [
    head(shop?.name || 'My ration shop', shop?.location),
    h('div', { class: 'grid-2' },
      h('section', { class: 'callout' },
        h('span', { class: 'label' }, 'Next distribution'),
        h('div', { class: 'big' }, next.village_name || 'Not scheduled'),
        h('p', {}, next.village_name ? 'This village is next in line at your shop.' : 'No village is ready yet. Check back later.')),
      panel('Villages served', null, h('ul', { class: 'checklist' }, villages.map((v) => h('li', {},
        h('span', {}, v.name), h('span', { class: 'muted' }, v.last_distribution_date ? `served ${fmtDate(v.last_distribution_date)}` : 'not served yet')))))),
    panel('What the shop has', null, table([
      { label: 'Commodity', render: (c) => h('strong', {}, c.name) },
      { label: 'Availability', render: (c) => [statusBadge(status[c.id]?.status || (qty[c.id] ? 'active' : 'out_of_stock')),
        status[c.id]?.status && status[c.id].status !== 'active' && status[c.id].reason ? h('span', { class: 'reason' }, status[c.id].reason) : null] },
      { label: 'In stock', num: true, render: (c) => h('span', { class: 'num' }, fmtQty(qty[c.id] || 0)) },
    ], commodities)),
  ];
}

async function beneficiaryComplaints({ user, refresh }) {
  if (await stillPending(user)) return awaitingApproval('Complaints');
  const complaints = await api('/complaints');
  return [
    head('Complaints', 'Tell your shop officer about short quantity, poor quality or other problems. You will get an SMS when it is updated.'),
    panel('New complaint', null, h('form', { class: 'form-grid', onsubmit: async (e) => {
      e.preventDefault();
      const form = e.currentTarget;
      const button = form.querySelector('button[type=submit]');
      const v = Object.fromEntries(new FormData(form));
      try {
        await busy(button, () => post('/complaints', v));
        toast('Complaint sent to your shop officer');
        refresh();
      } catch (err) { toast(err.message, 'error'); }
    } },
    field('What is it about?', select('category', Object.entries(CATEGORY), { placeholder: 'Choose' })),
    field('Describe the problem', h('textarea', { name: 'description', required: true, minlength: 5, maxlength: 1000 })),
    h('div', { class: 'form-actions' }, h('button', { class: 'btn btn-primary', type: 'submit', dataset: { busy: 'Sending…' } }, 'Send complaint')))),
    panel('My complaints', null, complaints.length ? h('ul', { class: 'timeline' }, complaints.map((c) => h('li', {},
      statusBadge(c.status),
      h('div', { class: 'body' }, h('strong', {}, CATEGORY[c.category] || c.category), h('p', {}, c.description),
        c.resolution_note ? h('p', { class: 'muted small' }, `Officer’s note: ${c.resolution_note}`) : null),
      h('span', { class: 'when' }, fmtDate(c.created_at))))) : empty('You have not filed any complaints.')),
  ];
}

async function beneficiaryMessages() {
  const messages = await api('/notifications');
  return [
    head('Messages', 'SMS updates sent to your mobile number, in English and Tamil.'),
    panel(null, null, messages.length ? h('ul', { class: 'timeline' }, messages.map((m) => h('li', {},
      h('span', { class: 'when' }, fmtDateTime(m.created_at)),
      h('p', { class: 'message' }, m.message),
      statusBadge(m.status)))) : empty('No messages yet. You will see distribution and complaint updates here.')),
  ];
}

const PAGES = {
  'admin:overview': adminOverview,
  'admin:places': adminPlaces,
  'admin:commodities': adminCommodities,
  'admin:stock': adminStock,
  'admin:people': adminPeople,
  'admin:activity': adminActivity,
  'admin:complaints': staffComplaints,
  'officer:today': officerToday,
  'officer:stock': officerStock,
  'officer:villages': officerVillages,
  'officer:beneficiaries': officerBeneficiaries,
  'officer:complaints': staffComplaints,
  'beneficiary:home': beneficiaryHome,
  'beneficiary:complaints': beneficiaryComplaints,
  'beneficiary:messages': beneficiaryMessages,
  forecast: forecastPage,
  security: securityPage,
};
