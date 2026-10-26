import { api, session } from './api.js';
import { activate, checkSupport, signIn, signUp } from './passkey.js';
import { busy, field, fingerprintSvg, h, icon, select, toast } from './ui.js';
import { NAV, renderPage } from './views.js';

const root = document.getElementById('app');
const state = { config: null, user: null };

window.addEventListener('fairflow:signed-out', (e) => {
  state.user = null;
  if (e.detail) toast(e.detail, 'error');
  showSignIn();
});
window.addEventListener('hashchange', () => { if (state.user) renderShell(); });
window.addEventListener('fairflow:user-updated', () => { if (state.user) renderShell(); });

async function boot() {
  try { state.config = await api('/config', { auth: false }); } catch { state.config = {}; }
  const saved = session.read();
  if (saved?.token) {
    try {
      state.user = await api('/me');
      return renderShell();
    } catch { session.clear(); }
  }
  showSignIn();
}

function startSession(result, message) {
  session.save(result);
  state.user = result.user;
  // replaceState does not fire hashchange, so the shell renders exactly once.
  history.replaceState(null, '', `#/${NAV[state.user.role][0].id}`);
  renderShell();
  toast(message);
}

// ---------- Sign-in screen ----------
function showSignIn() {
  document.title = 'Sign in · FairFlow';
  const panel = h('div', { class: 'auth-panel' });
  root.replaceChildren(h('div', { class: 'auth' }, brand(), panel));

  const tabs = [
    ['signin', 'Sign in', signInForm],
    ['signup', 'New beneficiary', signUpForm],
    ['activate', 'Activate account', activateForm],  // staff first sign-in, or a new phone
  ];
  const body = h('div');
  const tabButtons = tabs.map(([id, label, render]) => h('button', {
    class: 'tab', role: 'tab', id: `tab-${id}`, 'aria-selected': 'false',
    onclick: () => choose(id, render),
  }, label));
  function choose(id, render) {
    tabButtons.forEach((b) => b.setAttribute('aria-selected', String(b.id === `tab-${id}`)));
    body.replaceChildren(render());
  }
  const support = h('div', { class: 'support-note', role: 'status' }, 'Checking this device…');
  checkSupport().then((s) => { support.className = `support-note ${s.level}`; support.textContent = s.message; });
  panel.append(h('div', { class: 'tabs', role: 'tablist' }, tabButtons), body, support);
  choose('signin', signInForm);
}

function brand() {
  return h('section', { class: 'auth-brand' },
    h('div', { class: 'wordmark' }, 'FairFlow'),
    h('div', {},
      h('h1', {}, 'Every family’s ration, accounted for.'),
      h('p', { class: 'lede' }, 'Stock, village rounds and complaints for Fair Price Shops. Sign in with your fingerprint: no password to remember or share.')),
    h('div', { class: 'auth-facts' },
      h('p', {}, h('strong', {}, 'Your fingerprint stays on your phone or laptop. '), 'FairFlow only receives proof that your device recognised you.'),
      h('p', {}, h('strong', {}, 'First time? '), 'Beneficiaries register under “New beneficiary”. Staff use the activation code from the administrator. After that, you only touch the fingerprint sensor.'),
      h('p', {}, h('strong', {}, 'Lost your phone? '), 'Tell your shop officer or the administrator straight away. They switch off the lost phone and give you a code for the new one.')),
    h('svg', { class: 'grain', viewBox: '0 0 24 24', 'aria-hidden': 'true' },
      h('path', { d: 'M12 23V9M12 9c-3-1-4-4-4-7 3 0 4 4 4 7zM12 9c3-1 4-4 4-7-3 0-4 4-4 7zM12 15c-3-1-6-3-6-6 3 0 6 3 6 6zM12 15c3-1 6-3 6-6-3 0-6 3-6 6z', fill: 'none', stroke: '#fff', 'stroke-width': '0.8' })));
}

function sensorButton(label, onPress) {
  const status = h('p', { class: 'sensor-status', role: 'status' }, label);
  const sensor = h('button', { class: 'sensor', type: 'button', 'aria-label': label }, fingerprintSvg());
  sensor.addEventListener('click', async () => {
    if (sensor.classList.contains('waiting')) return;
    sensor.classList.add('waiting');
    status.textContent = 'Touch the fingerprint sensor on your device…';
    try { await onPress(); } catch (err) {
      status.textContent = label;
      toast(err.message, 'error');
    } finally { sensor.classList.remove('waiting'); }
  });
  return h('div', { class: 'sensor-wrap' }, sensor, status);
}

function signInForm() {
  // Fingerprint only: no username, no password. The device offers its FairFlow passkey.
  return h('div', { class: 'form-grid' },
    h('div', {}, h('h2', {}, 'Sign in with your fingerprint'),
      h('p', { class: 'muted' }, 'Press the button, then touch your device’s fingerprint sensor.')),
    sensorButton('Sign in with fingerprint', async () => {
      const result = await signIn();
      startSession(result, `Signed in as ${result.user.username}`);
    }),
    h('p', { class: 'muted small' }, 'Not registered on this device yet? Choose “New beneficiary”, or “Activate account” if you have a code.'));
}

function signUpForm() {
  const shops = h('div', {}, select('shop_id', [], { placeholder: 'Loading shops…' }));
  api('/public/shops', { auth: false }).then((list) => {
    shops.replaceChildren(list.length
      ? select('shop_id', list.map((s) => [s.id, `${s.name}, ${s.location}`]), { placeholder: 'Choose your ration shop' })
      : h('p', { class: 'muted' }, 'No shops have been set up yet. Ask the administrator to add your shop first.'));
  }).catch((err) => shops.replaceChildren(h('p', { class: 'muted' }, err.message)));

  const button = h('button', { class: 'btn btn-primary', type: 'submit', dataset: { busy: 'Waiting for your fingerprint…' } }, 'Register my fingerprint');
  const form = h('form', { class: 'form-grid' },
    h('div', {}, h('h2', {}, 'Register as a beneficiary'),
      h('p', { class: 'muted' }, 'Your device will ask for your fingerprint to create a passkey for FairFlow.')),
    field('Username', h('input', { name: 'username', required: true, minlength: 3, maxlength: 50, autocomplete: 'username', pattern: '[A-Za-z0-9._\\-]{3,50}' }), 'Letters, numbers, dot, dash or underscore.'),
    field('Ration shop', shops),
    field('Mobile number', h('input', { name: 'phone', type: 'tel', inputmode: 'tel', pattern: '\\+?[0-9]{10,15}', placeholder: '+91 98765 43210' }), 'For SMS updates about distribution and complaints. Optional.'),
    button);
  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    const v = Object.fromEntries(new FormData(form));
    try {
      const result = await busy(button, () => signUp({ username: v.username.trim(), shop_id: Number(v.shop_id), phone: v.phone.replace(/\s/g, '') }));
      startSession(result, result.user.approved === false
        ? 'Fingerprint registered. Your shop officer will approve your account.'
        : 'Fingerprint registered. Welcome to FairFlow.');
    } catch (err) { toast(err.message, 'error'); }
  });
  return form;
}

function activateForm() {
  const button = h('button', { class: 'btn btn-primary', type: 'submit', dataset: { busy: 'Waiting for your fingerprint…' } }, 'Register my fingerprint');
  const form = h('form', { class: 'form-grid' },
    h('div', {}, h('h2', {}, 'Activate your account'),
      h('p', { class: 'muted' }, 'Use the one-time activation code from your administrator or shop officer. It works once and expires.')),
    field('Username', h('input', { name: 'username', required: true, autocomplete: 'username' })),
    field('Activation code', h('input', { name: 'activation_code', required: true, placeholder: 'ABCDE-FGHJK', autocomplete: 'one-time-code', minlength: 6, maxlength: 20 })),
    button);
  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    const v = Object.fromEntries(new FormData(form));
    try {
      const result = await busy(button, () => activate({ username: v.username.trim(), activation_code: v.activation_code.trim() }));
      startSession(result, 'Fingerprint registered. Your account is active.');
    } catch (err) { toast(err.message, 'error'); }
  });
  return form;
}

// ---------- Signed-in shell ----------
function renderShell() {
  const items = NAV[state.user.role];
  const current = location.hash.replace(/^#\//, '').split('/')[0];
  const active = items.find((i) => i.id === current) || items[0];
  if (active.id !== current) history.replaceState(null, '', `#/${active.id}`);
  document.title = `${active.label} · FairFlow`;

  const rail = h('aside', { class: 'rail' },
    h('div', { class: 'wordmark' }, 'FairFlow'),
    h('button', { class: 'btn btn-quiet btn-small menu-toggle', 'aria-expanded': 'false', onclick: (e) => {
      const open = rail.classList.toggle('open');
      e.currentTarget.setAttribute('aria-expanded', String(open));
    } }, icon('menu'), 'Menu'),
    h('nav', { 'aria-label': 'Main' }, items.map((i) => h('a', {
      href: `#/${i.id}`, 'aria-current': i.id === active.id ? 'page' : null,
      // Same hash = no hashchange event: tapping the current page refreshes it.
      onclick: () => { if (location.hash === `#/${i.id}`) renderShell(); },
    }, icon(i.icon), i.label))),
    h('div', { class: 'who' },
      h('strong', {}, state.user.username),
      h('span', { class: 'muted small' }, roleLabel(state.user)),
      h('button', { class: 'btn btn-quiet btn-small', onclick: signOut }, 'Sign out')));

  const main = h('main', { id: 'main', tabindex: '-1' });
  root.replaceChildren(h('div', { class: 'shell' }, rail, main));
  renderPage(active.id, main, { user: state.user, config: state.config });
}

function roleLabel(user) {
  const role = { admin: 'Administrator', officer: 'Shop officer', beneficiary: 'Beneficiary' }[user.role];
  const label = user.shop_id ? `${role}, shop #${user.shop_id}` : role;
  return user.approved === false ? `${label} · awaiting approval` : label;
}

function signOut() {
  session.clear();
  state.user = null;
  history.replaceState(null, '', '#/');
  showSignIn();
  toast('Signed out');
}

boot();
