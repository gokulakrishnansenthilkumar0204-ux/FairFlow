/**
 * FairFlow Frontend Application
 * Simple, clean, readable vanilla JavaScript
 */

// ================= 1. CONFIGURATION & STATE =================
const API_BASE = window.location.origin.startsWith('http') ? window.location.origin : 'http://localhost:8000';

const state = {
  token: localStorage.getItem('fairflow_token') || null,
  user: JSON.parse(localStorage.getItem('fairflow_user') || 'null'),
  activeTab: null,
  demoMode: false,
  selectedVillageId: null,
  commoditiesCache: [],
  shopsCache: []
};

// Rich mock data for offline presentation and demo mode
const mockData = {
  shops: [
    { id: 1, name: "FPS Guindy", location: "Guindy, Chennai" },
    { id: 2, name: "FPS Tambaram", location: "Tambaram, Chengalpattu" }
  ],
  villages: [
    { id: 1, name: "Kotturpuram", shop_id: 1, last_distribution_date: "2026-09-10" },
    { id: 2, name: "Saidapet", shop_id: 1, last_distribution_date: "2026-08-15" },
    { id: 3, name: "Little Mount", shop_id: 1, last_distribution_date: null },
    { id: 4, name: "Mudichur", shop_id: 2, last_distribution_date: "2026-09-01" },
    { id: 5, name: "Perungalathur", shop_id: 2, last_distribution_date: null }
  ],
  commodities: [
    { id: 1, name: "Rice" },
    { id: 2, name: "Wheat" },
    { id: 3, name: "Sugar" },
    { id: 4, name: "Toor Dal" },
    { id: 5, name: "Palm Oil" }
  ],
  stock: [
    { id: 1, shop_id: 1, commodity_id: 1, commodity_name: "Rice", quantity: 500.000 },
    { id: 2, shop_id: 1, commodity_id: 2, commodity_name: "Wheat", quantity: 150.000 },
    { id: 3, shop_id: 1, commodity_id: 3, commodity_name: "Sugar", quantity: 90.000 },
    { id: 4, shop_id: 1, commodity_id: 4, commodity_name: "Toor Dal", quantity: 50.000 },
    { id: 5, shop_id: 1, commodity_id: 5, commodity_name: "Palm Oil", quantity: 60.000 }
  ],
  commodity_status: [
    { commodity_id: 1, status: "active", reason: "Good supply available" },
    { commodity_id: 2, status: "active", reason: null },
    { commodity_id: 3, status: "active", reason: null },
    { commodity_id: 4, status: "active", reason: null },
    { commodity_id: 5, status: "seasonally_suspended", reason: "Not supplied this season" }
  ],
  complaints: [
    { id: 1, beneficiary_id: 1, beneficiary_name: "beneficiary1", shop_id: 1, category: "quality", description: "Slight discoloration observed in raw rice bag batch #204.", status: "in_progress", resolution_note: "Batch sample sent to Civil Supplies quality lab.", created_at: "2026-10-12" },
    { id: 2, beneficiary_id: 1, beneficiary_name: "beneficiary1", shop_id: 1, category: "availability", description: "Sugar was temporarily out of stock on Saturday morning.", status: "resolved", resolution_note: "Restocked 90kg sugar on Monday 9 AM.", created_at: "2026-10-08" }
  ],
  notifications: [
    { id: 101, message_en: "Ration distribution completed for Kotturpuram. Your quota has been updated.", message_ta: "கோட்டூர்புரத்திற்கான ரேஷன் விநியோகம் நிறைவடைந்தது.", created_at: "2026-10-10 11:30" },
    { id: 102, message_en: "Complaint #2 status updated to 'resolved': Restocked 90kg sugar.", message_ta: "புகார் #2 நிலை 'தீர்க்கப்பட்டது' என மாற்றப்பட்டது.", created_at: "2026-10-08 14:15" }
  ],
  audit_logs: [
    { id: 45, action: "MARK_DISTRIBUTED", user_id: 2, shop_id: 1, created_at: "2026-10-14 10:20:00", details: "Distributed to Kotturpuram (5 commodities)" },
    { id: 44, action: "ADD_STOCK", user_id: 2, shop_id: 1, created_at: "2026-10-14 09:15:00", details: "Added 500kg Rice to Shop #1" },
    { id: 43, action: "SET_STATUS", user_id: 2, shop_id: 1, created_at: "2026-10-13 16:00:00", details: "Palm Oil marked seasonally_suspended" }
  ]
};

// ================= 2. API CLIENT HELPER =================
async function api(path, method = 'GET', body = null, isForm = false) {
  // If Demo Mode is active, handle with local mock data
  if (state.demoMode) {
    return handleMockApi(path, method, body);
  }

  const headers = {};
  if (state.token) {
    headers['Authorization'] = `Bearer ${state.token}`;
  }

  let requestBody = body;
  if (body) {
    if (isForm) {
      headers['Content-Type'] = 'application/x-www-form-urlencoded';
      requestBody = new URLSearchParams(body).toString();
    } else {
      headers['Content-Type'] = 'application/json';
      requestBody = JSON.stringify(body);
    }
  }

  try {
    const res = await fetch(`${API_BASE}${path}`, {
      method,
      headers,
      body: requestBody
    });

    const data = await res.json().catch(() => ({}));

    if (!res.ok) {
      // If 401 Unauthorized, log out user
      if (res.status === 401) {
        logout();
        showToast('Session expired. Please sign in again.', 'error');
        throw new Error('Unauthorized');
      }
      const msg = data.detail || data.message || `Request failed with error ${res.status}`;
      showToast(msg, 'error');
      throw new Error(msg);
    }

    return data;
  } catch (err) {
    if (err.message !== 'Unauthorized') {
      console.warn('API connection failed:', err.message);
    }
    throw err;
  }
}

// ================= 3. TOAST NOTIFICATIONS & MODALS =================
function showToast(message, type = 'info') {
  const container = document.getElementById('toast-container');
  const toast = document.createElement('div');
  toast.className = `toast toast-${type}`;
  toast.innerHTML = `<span>${type === 'success' ? '✓' : type === 'error' ? '⚠' : 'ℹ'}</span> <span>${message}</span>`;
  container.appendChild(toast);
  setTimeout(() => {
    toast.remove();
  }, 4000);
}

function showModal(modalId) {
  const el = document.getElementById(modalId);
  if (el) el.classList.remove('hidden');
}

function closeModal(modalId) {
  const el = document.getElementById(modalId);
  if (el) el.classList.add('hidden');
}

// Close modal when clicking outside box
window.addEventListener('click', (e) => {
  if (e.target.classList.contains('modal-overlay')) {
    e.target.classList.add('hidden');
  }
});

// ================= 4. AUTHENTICATION =================
function switchAuthTab(mode) {
  const isLogin = mode === 'login';
  document.getElementById('tab-btn-login').classList.toggle('active', isLogin);
  document.getElementById('tab-btn-signup').classList.toggle('active', !isLogin);
  document.getElementById('form-login').classList.toggle('hidden', !isLogin);
  document.getElementById('form-signup').classList.toggle('hidden', isLogin);
}

function quickFillLogin(username, password) {
  document.getElementById('login-username').value = username;
  document.getElementById('login-password').value = password;
  switchAuthTab('login');
  handleLogin(new Event('submit'));
}

async function handleLogin(e) {
  if (e && e.preventDefault) e.preventDefault();
  const username = document.getElementById('login-username').value.trim();
  const password = document.getElementById('login-password').value;

  if (!username || !password) {
    showToast('Please enter both username and password', 'error');
    return;
  }

  // Demo mode quick bypass
  if (state.demoMode) {
    let role = "officer";
    let shopId = 1;
    if (username.includes("admin")) { role = "admin"; shopId = null; }
    if (username.includes("beneficiary")) { role = "beneficiary"; shopId = 1; }

    state.user = { id: 1, username, role, shop_id: shopId };
    state.token = "mock-token-demo";
    saveSession();
    renderDashboard();
    showToast(`Signed in as ${role} (${username}) [Demo Mode]`, 'success');
    return;
  }

  try {
    const data = await api('/login', 'POST', { username, password }, true);
    state.token = data.access_token;

    // Fetch user profile from /me or fallback from token
    try {
      const me = await api('/me', 'GET');
      state.user = me;
    } catch {
      // Decode JWT payload if /me isn't present
      const payload = JSON.parse(atob(state.token.split('.')[1]));
      state.user = { username: payload.sub, role: payload.role, shop_id: 1 };
    }

    saveSession();
    renderDashboard();
    showToast(`Welcome back, ${state.user.username}!`, 'success');
  } catch (err) {
    console.error('Login error:', err);
  }
}

async function handleSignup(e) {
  e.preventDefault();
  const username = document.getElementById('signup-username').value.trim();
  const password = document.getElementById('signup-password').value;
  const shop_id = parseInt(document.getElementById('signup-shop').value);
  const phone = document.getElementById('signup-phone').value.trim() || null;

  try {
    await api('/signup', 'POST', { username, password, shop_id, phone });
    showToast('Beneficiary registered! You can now sign in.', 'success');
    document.getElementById('login-username').value = username;
    document.getElementById('login-password').value = password;
    switchAuthTab('login');
  } catch (err) {
    console.error('Signup error:', err);
  }
}

function saveSession() {
  localStorage.setItem('fairflow_token', state.token);
  localStorage.setItem('fairflow_user', JSON.stringify(state.user));
}

function logout() {
  state.token = null;
  state.user = null;
  state.activeTab = null;
  localStorage.removeItem('fairflow_token');
  localStorage.removeItem('fairflow_user');

  document.getElementById('user-profile-pill').classList.add('hidden');
  document.getElementById('app-nav').classList.add('hidden');
  document.getElementById('app-view').classList.add('hidden');
  document.getElementById('auth-view').classList.remove('hidden');

  document.getElementById('admin-panel').classList.add('hidden');
  document.getElementById('officer-panel').classList.add('hidden');
  document.getElementById('beneficiary-panel').classList.add('hidden');
}

// ================= 5. DASHBOARD & TAB NAVIGATION =================
const ROLE_TABS = {
  admin: [
    { id: 'admin-tab-overview', label: '📊 Overview' },
    { id: 'admin-tab-shops', label: '🏪 Shops' },
    { id: 'admin-tab-villages', label: '🏡 Villages' },
    { id: 'admin-tab-commodities', label: '📦 Commodities' },
    { id: 'admin-tab-users', label: '👥 Staff & Users' },
    { id: 'admin-tab-audit', label: '🛡️ Audit Logs' }
  ],
  officer: [
    { id: 'officer-tab-stock', label: '📦 Shop Stock' },
    { id: 'officer-tab-villages', label: '🚚 Village Distribution' },
    { id: 'officer-tab-forecast', label: '📈 ML Demand Forecast' },
    { id: 'officer-tab-biometric', label: '🪪 Biometric Verification' },
    { id: 'officer-tab-complaints', label: '💬 Complaints' }
  ],
  beneficiary: [
    { id: 'beneficiary-tab-stock', label: '🌾 Shop Stock' },
    { id: 'beneficiary-tab-readiness', label: '🚚 Village Schedule' },
    { id: 'beneficiary-tab-complaints', label: '✍️ Grievances' },
    { id: 'beneficiary-tab-notifications', label: '🔔 SMS Alerts' }
  ]
};

function renderDashboard() {
  if (!state.user) return;

  // Header update
  document.getElementById('auth-view').classList.add('hidden');
  document.getElementById('app-view').classList.remove('hidden');
  document.getElementById('app-nav').classList.remove('hidden');

  const profilePill = document.getElementById('user-profile-pill');
  profilePill.classList.remove('hidden');
  document.getElementById('header-user-role').innerText = state.user.role;
  document.getElementById('header-username').innerText = state.user.username;

  // Role panels
  const role = state.user.role;
  document.getElementById('admin-panel').classList.toggle('hidden', role !== 'admin');
  document.getElementById('officer-panel').classList.toggle('hidden', role !== 'officer');
  document.getElementById('beneficiary-panel').classList.toggle('hidden', role !== 'beneficiary');

  // Build Nav Tabs
  const container = document.getElementById('nav-tabs-container');
  container.innerHTML = '';
  const tabs = ROLE_TABS[role] || [];

  tabs.forEach((tab, index) => {
    const btn = document.createElement('button');
    btn.className = `nav-tab-item ${index === 0 ? 'active' : ''}`;
    btn.innerText = tab.label;
    btn.onclick = () => switchTab(tab.id, btn);
    container.appendChild(btn);
  });

  // Switch to first tab
  if (tabs.length > 0) {
    switchTab(tabs[0].id, container.children[0]);
  }

  // Preload master lists
  loadCommoditiesList();
  loadShopsList();
}

function switchTab(tabId, clickedBtn) {
  state.activeTab = tabId;

  // Update button active styles
  document.querySelectorAll('.nav-tab-item').forEach(b => b.classList.remove('active'));
  if (clickedBtn) clickedBtn.classList.add('active');

  // Update pane active styles
  document.querySelectorAll('.tab-pane').forEach(p => p.classList.remove('active'));
  const activePane = document.getElementById(tabId);
  if (activePane) activePane.classList.add('active');

  // Load data for specific tab
  if (tabId === 'admin-tab-overview') loadAdminOverview();
  else if (tabId === 'admin-tab-shops') loadAdminShops();
  else if (tabId === 'admin-tab-villages') loadAdminVillages();
  else if (tabId === 'admin-tab-commodities') loadAdminCommodities();
  else if (tabId === 'admin-tab-audit') loadAdminAuditLogs();
  else if (tabId === 'officer-tab-stock') loadOfficerStock();
  else if (tabId === 'officer-tab-villages') loadOfficerVillages();
  else if (tabId === 'officer-tab-forecast') loadOfficerForecast();
  else if (tabId === 'officer-tab-complaints') loadOfficerComplaints();
  else if (tabId === 'beneficiary-tab-stock') loadBeneficiaryStock();
  else if (tabId === 'beneficiary-tab-readiness') loadBeneficiaryVillages();
  else if (tabId === 'beneficiary-tab-complaints') loadBeneficiaryComplaints();
  else if (tabId === 'beneficiary-tab-notifications') loadBeneficiaryNotifications();
}

// ================= 6. SHARED MASTER DATA =================
async function loadCommoditiesList() {
  try {
    const list = await api('/commodities');
    state.commoditiesCache = list;
    populateCommodityDropdowns(list);
  } catch (err) {
    console.warn('Could not load commodities list:', err);
  }
}

async function loadShopsList() {
  try {
    const list = await api('/shops');
    state.shopsCache = list;
  } catch (err) {
    console.warn('Could not load shops list:', err);
  }
}

function populateCommodityDropdowns(commodities) {
  const dropdownIds = [
    'stock-commodity-select',
    'status-commodity-select',
    'hist-commodity-select',
    'req-commodity-select'
  ];

  dropdownIds.forEach(id => {
    const select = document.getElementById(id);
    if (!select) return;
    const currentVal = select.value;
    select.innerHTML = '<option value="">Select commodity...</option>';
    commodities.forEach(c => {
      const opt = document.createElement('option');
      opt.value = c.id;
      opt.innerText = c.name;
      select.appendChild(opt);
    });
    if (currentVal) select.value = currentVal;
  });
}

// ================= 7. ADMIN FUNCTIONS =================
async function loadAdminOverview() {
  try {
    const [shops, villages, commodities, audits] = await Promise.all([
      api('/shops').catch(() => []),
      api('/villages').catch(() => []),
      api('/commodities').catch(() => []),
      api('/audit-logs?limit=5').catch(() => [])
    ]);

    document.getElementById('admin-stat-shops').innerText = shops.length;
    document.getElementById('admin-stat-villages').innerText = villages.length;
    document.getElementById('admin-stat-commodities').innerText = commodities.length;
    document.getElementById('admin-stat-audits').innerText = audits.length > 0 ? `${audits.length}+` : '0';
  } catch (err) {
    console.error('Error loading admin overview:', err);
  }
}

async function loadAdminShops() {
  const tbody = document.getElementById('admin-shops-tbody');
  tbody.innerHTML = '<tr><td colspan="3" class="text-center">Loading shops...</td></tr>';
  try {
    const shops = await api('/shops');
    if (shops.length === 0) {
      tbody.innerHTML = '<tr><td colspan="3" class="text-center text-muted">No shops registered yet.</td></tr>';
      return;
    }
    tbody.innerHTML = shops.map(s => `
      <tr>
        <td><strong>#${s.id}</strong></td>
        <td>${escapeHtml(s.name)}</td>
        <td>${escapeHtml(s.location)}</td>
      </tr>
    `).join('');
  } catch (err) {
    tbody.innerHTML = `<tr><td colspan="3" class="text-center text-muted">Failed to load shops: ${err.message}</td></tr>`;
  }
}

async function handleCreateShop(e) {
  e.preventDefault();
  const name = document.getElementById('shop-name').value.trim();
  const location = document.getElementById('shop-location').value.trim();
  try {
    await api('/shops', 'POST', { name, location });
    showToast(`Shop "${name}" created successfully!`, 'success');
    closeModal('modal-add-shop');
    document.getElementById('shop-name').value = '';
    document.getElementById('shop-location').value = '';
    loadAdminShops();
  } catch (err) {
    console.error(err);
  }
}

async function loadAdminVillages() {
  const tbody = document.getElementById('admin-villages-tbody');
  tbody.innerHTML = '<tr><td colspan="4" class="text-center">Loading villages...</td></tr>';
  try {
    const villages = await api('/villages');
    if (villages.length === 0) {
      tbody.innerHTML = '<tr><td colspan="4" class="text-center text-muted">No villages registered yet.</td></tr>';
      return;
    }
    tbody.innerHTML = villages.map(v => `
      <tr>
        <td><strong>#${v.id}</strong></td>
        <td>${escapeHtml(v.name)}</td>
        <td>Shop #${v.shop_id}</td>
        <td>${v.last_distribution_date || '<span class="text-muted">Never distributed</span>'}</td>
      </tr>
    `).join('');
  } catch (err) {
    tbody.innerHTML = `<tr><td colspan="4" class="text-center text-muted">Failed to load villages: ${err.message}</td></tr>`;
  }
}

async function handleCreateVillage(e) {
  e.preventDefault();
  const name = document.getElementById('village-name').value.trim();
  const shop_id = parseInt(document.getElementById('village-shop-id').value);
  try {
    await api('/villages', 'POST', { name, shop_id });
    showToast(`Village "${name}" added!`, 'success');
    closeModal('modal-add-village');
    document.getElementById('village-name').value = '';
    loadAdminVillages();
  } catch (err) {
    console.error(err);
  }
}

async function loadAdminCommodities() {
  const tbody = document.getElementById('admin-commodities-tbody');
  tbody.innerHTML = '<tr><td colspan="2" class="text-center">Loading commodities...</td></tr>';
  try {
    const list = await api('/commodities');
    state.commoditiesCache = list;
    populateCommodityDropdowns(list);
    if (list.length === 0) {
      tbody.innerHTML = '<tr><td colspan="2" class="text-center text-muted">No commodities added yet.</td></tr>';
      return;
    }
    tbody.innerHTML = list.map(c => `
      <tr>
        <td><strong>#${c.id}</strong></td>
        <td>${escapeHtml(c.name)}</td>
      </tr>
    `).join('');
  } catch (err) {
    tbody.innerHTML = `<tr><td colspan="2" class="text-center text-muted">Failed to load: ${err.message}</td></tr>`;
  }
}

async function handleCreateCommodity(e) {
  e.preventDefault();
  const name = document.getElementById('commodity-name').value.trim();
  try {
    await api('/commodities', 'POST', { name });
    showToast(`Commodity "${name}" added!`, 'success');
    closeModal('modal-add-commodity');
    document.getElementById('commodity-name').value = '';
    loadAdminCommodities();
  } catch (err) {
    console.error(err);
  }
}

function toggleStaffShopRequirement(role) {
  const group = document.getElementById('group-staff-shop');
  const input = document.getElementById('staff-shop-id');
  if (role === 'admin') {
    group.classList.add('hidden');
    input.required = false;
  } else {
    group.classList.remove('hidden');
    input.required = true;
  }
}

async function handleCreateStaff(e) {
  e.preventDefault();
  const username = document.getElementById('staff-username').value.trim();
  const password = document.getElementById('staff-password').value;
  const role = document.getElementById('staff-role').value;
  const shop_id_raw = document.getElementById('staff-shop-id').value;
  const shop_id = shop_id_raw ? parseInt(shop_id_raw) : null;
  const phone = document.getElementById('staff-phone').value.trim() || null;

  try {
    await api('/admin/users', 'POST', { username, password, role, shop_id, phone });
    showToast(`User "${username}" created as ${role}!`, 'success');
    closeModal('modal-add-user');
    document.getElementById('staff-username').value = '';
    document.getElementById('staff-password').value = '';
  } catch (err) {
    console.error(err);
  }
}

async function loadAdminAuditLogs() {
  const tbody = document.getElementById('admin-audit-tbody');
  tbody.innerHTML = '<tr><td colspan="6" class="text-center">Loading audit log...</td></tr>';
  try {
    const logs = await api('/audit-logs?limit=50');
    if (logs.length === 0) {
      tbody.innerHTML = '<tr><td colspan="6" class="text-center text-muted">No audit events recorded yet.</td></tr>';
      return;
    }
    tbody.innerHTML = logs.map(a => `
      <tr>
        <td><strong>#${a.id}</strong></td>
        <td><small>${formatDate(a.created_at)}</small></td>
        <td><span class="badge badge-info">${escapeHtml(a.action)}</span></td>
        <td>Shop #${a.shop_id || '-'}</td>
        <td>User #${a.user_id || '-'}</td>
        <td><code>${escapeHtml(a.details || '-')}</code></td>
      </tr>
    `).join('');
  } catch (err) {
    tbody.innerHTML = `<tr><td colspan="6" class="text-center text-muted">Failed to load audit logs: ${err.message}</td></tr>`;
  }
}

// ================= 8. OFFICER FUNCTIONS =================
async function loadOfficerStock() {
  const tbody = document.getElementById('officer-stock-tbody');
  tbody.innerHTML = '<tr><td colspan="6" class="text-center">Loading stock...</td></tr>';
  const shopId = state.user.shop_id || 1;

  try {
    const [stockList, statusList, nextVillage] = await Promise.all([
      api('/stock'),
      api(`/commodity-status/${shopId}`).catch(() => []),
      api(`/shops/${shopId}/next-village`).catch(() => null)
    ]);

    // Update banner
    const bannerText = document.getElementById('next-village-name');
    if (nextVillage && nextVillage.village_name) {
      bannerText.innerHTML = `<strong>${escapeHtml(nextVillage.village_name)}</strong> (Village ID #${nextVillage.village_id}) - Ready for distribution!`;
    } else {
      bannerText.innerHTML = 'No village is currently ready for distribution.';
    }

    if (stockList.length === 0) {
      tbody.innerHTML = '<tr><td colspan="6" class="text-center text-muted">No stock items in this shop. Click "+ Receive New Stock" to add items.</td></tr>';
      return;
    }

    const statusMap = {};
    statusList.forEach(s => { statusMap[s.commodity_id] = s; });

    tbody.innerHTML = stockList.map(item => {
      const s = statusMap[item.commodity_id] || { status: 'active', reason: '' };
      const statusBadge = getStatusBadge(s.status);
      return `
        <tr>
          <td>#${item.id}</td>
          <td><strong>${escapeHtml(item.commodity_name)}</strong></td>
          <td><span style="font-size:1.1rem; font-weight:700;">${item.quantity}</span> <small class="text-muted">kg/L</small></td>
          <td>${statusBadge}</td>
          <td><small class="text-muted">${escapeHtml(s.reason || '-')}</small></td>
          <td>
            <div class="button-group">
              <button class="btn btn-outline-sm" onclick="openUpdateStockModal(${item.id}, '${escapeHtml(item.commodity_name)}', ${item.quantity})">Adjust</button>
              <button class="btn btn-danger-ghost btn-sm" onclick="handleDeleteStock(${item.id})">Delete</button>
            </div>
          </td>
        </tr>
      `;
    }).join('');
  } catch (err) {
    tbody.innerHTML = `<tr><td colspan="6" class="text-center text-muted">Failed to load stock: ${err.message}</td></tr>`;
  }
}

function getStatusBadge(status) {
  if (status === 'active') return '<span class="badge badge-success">Active</span>';
  if (status === 'seasonally_suspended') return '<span class="badge badge-warning">Seasonally Suspended</span>';
  if (status === 'out_of_stock') return '<span class="badge badge-danger">Out of Stock</span>';
  return `<span class="badge badge-neutral">${escapeHtml(status)}</span>`;
}

async function handleAddStock(e) {
  e.preventDefault();
  const commodity_id = parseInt(document.getElementById('stock-commodity-select').value);
  const quantity = parseFloat(document.getElementById('stock-quantity').value);
  const shop_id = state.user.shop_id || 1;

  try {
    await api('/stock', 'POST', { shop_id, commodity_id, quantity });
    showToast('Stock received and inventory updated!', 'success');
    closeModal('modal-add-stock');
    document.getElementById('stock-quantity').value = '';
    loadOfficerStock();
  } catch (err) {
    console.error(err);
  }
}

function openUpdateStockModal(stockId, name, currentQty) {
  document.getElementById('edit-stock-id').value = stockId;
  document.getElementById('edit-stock-commodity-name').innerText = `Adjust Quantity for ${name}:`;
  document.getElementById('edit-stock-quantity').value = currentQty;
  showModal('modal-update-stock');
}

async function handleUpdateStock(e) {
  e.preventDefault();
  const stockId = document.getElementById('edit-stock-id').value;
  const quantity = parseFloat(document.getElementById('edit-stock-quantity').value);

  try {
    await api(`/stock/${stockId}`, 'PUT', { quantity });
    showToast('Stock level updated!', 'success');
    closeModal('modal-update-stock');
    loadOfficerStock();
  } catch (err) {
    console.error(err);
  }
}

async function handleDeleteStock(stockId) {
  if (!confirm(`Are you sure you want to delete stock item #${stockId}?`)) return;
  try {
    await api(`/stock/${stockId}`, 'DELETE');
    showToast('Stock item deleted', 'info');
    loadOfficerStock();
  } catch (err) {
    console.error(err);
  }
}

async function handleSetCommodityStatus(e) {
  e.preventDefault();
  const commodity_id = parseInt(document.getElementById('status-commodity-select').value);
  const status = document.getElementById('status-value').value;
  const reason = document.getElementById('status-reason').value.trim() || null;
  const shop_id = state.user.shop_id || 1;

  try {
    await api('/commodity-status', 'POST', { shop_id, commodity_id, status, reason });
    showToast('Commodity status updated!', 'success');
    closeModal('modal-commodity-status');
    loadOfficerStock();
  } catch (err) {
    console.error(err);
  }
}

async function loadOfficerVillages() {
  const tbody = document.getElementById('officer-villages-tbody');
  tbody.innerHTML = '<tr><td colspan="4" class="text-center">Loading villages...</td></tr>';

  try {
    const villages = await api('/villages');
    if (villages.length === 0) {
      tbody.innerHTML = '<tr><td colspan="4" class="text-center text-muted">No villages assigned to this shop.</td></tr>';
      return;
    }

    tbody.innerHTML = villages.map(v => `
      <tr>
        <td><strong>${escapeHtml(v.name)}</strong> (ID #${v.id})</td>
        <td>${v.last_distribution_date ? formatDate(v.last_distribution_date) : '<span class="text-muted">Never distributed</span>'}</td>
        <td>
          <button class="btn btn-outline-sm" onclick="inspectVillageReadiness(${v.id}, '${escapeHtml(v.name)}')">Check Readiness</button>
        </td>
        <td>
          <div class="button-group">
            <button class="btn btn-sm btn-outline" onclick="openRequirementsModal(${v.id}, '${escapeHtml(v.name)}')">Quota Setup</button>
            <button class="btn btn-sm btn-success" onclick="quickMarkDistributed(${v.id}, '${escapeHtml(v.name)}')">Distribute</button>
          </div>
        </td>
      </tr>
    `).join('');
  } catch (err) {
    tbody.innerHTML = `<tr><td colspan="4" class="text-center text-muted">Failed to load villages: ${err.message}</td></tr>`;
  }
}

async function inspectVillageReadiness(villageId, villageName) {
  state.selectedVillageId = villageId;
  const card = document.getElementById('readiness-detail-card');
  const title = document.getElementById('readiness-village-title');
  const pill = document.getElementById('readiness-summary-pill');
  const breakdown = document.getElementById('readiness-items-breakdown');
  const markBtn = document.getElementById('btn-mark-distributed');

  title.innerText = `Readiness Check: ${villageName}`;
  card.classList.remove('hidden');
  breakdown.innerHTML = '<div class="text-muted p-2">Checking shop stock versus village requirements...</div>';

  try {
    const r = await api(`/villages/${villageId}/readiness`);
    if (r.is_ready) {
      pill.className = 'badge badge-lg badge-success mb-3';
      pill.innerText = '✓ READY FOR DISTRIBUTION';
      markBtn.disabled = false;
      markBtn.classList.remove('hidden');
    } else {
      pill.className = 'badge badge-lg badge-danger mb-3';
      pill.innerText = '⚠ NOT READY FOR DISTRIBUTION';
      markBtn.disabled = false; // still allow attempt to trigger backend validation check
    }

    let html = '';
    if (r.reason) {
      html += `<div class="banner banner-info mb-3">${escapeHtml(r.reason)}</div>`;
    }

    if (r.insufficient_stock && r.insufficient_stock.length > 0) {
      html += '<h4 style="color:var(--danger); margin-top:0.5rem;">Insufficient Stock:</h4><ul>';
      r.insufficient_stock.forEach(item => {
        html += `<li><strong>${escapeHtml(item.commodity_name)}</strong>: Required ${item.required_quantity} kg/L, but only ${item.available_quantity} kg/L available in shop.</li>`;
      });
      html += '</ul>';
    }

    if (r.missing_commodities && r.missing_commodities.length > 0) {
      html += '<h4 style="color:var(--danger); margin-top:0.5rem;">Missing Commodities (Zero Stock):</h4><ul>';
      r.missing_commodities.forEach(item => {
        html += `<li><strong>${escapeHtml(item.commodity_name)}</strong>: Required ${item.required_quantity} kg/L (Stock is 0).</li>`;
      });
      html += '</ul>';
    }

    if (r.seasonally_suspended && r.seasonally_suspended.length > 0) {
      html += '<h4 style="color:var(--warning); margin-top:0.5rem;">Seasonally Suspended Commodities:</h4><ul>';
      r.seasonally_suspended.forEach(item => {
        html += `<li><strong>${escapeHtml(item.commodity_name)}</strong>: Required ${item.required_quantity} kg/L (Temporarily suspended).</li>`;
      });
      html += '</ul>';
    }

    if (r.is_ready) {
      html += '<p style="color:var(--success); font-weight:500; margin-top:0.5rem;">All required commodities are in stock and ready to be loaded for distribution.</p>';
    }

    breakdown.innerHTML = html;
  } catch (err) {
    breakdown.innerHTML = `<div class="text-muted">Error checking readiness: ${err.message}</div>`;
  }
}

function closeReadinessDetail() {
  document.getElementById('readiness-detail-card').classList.add('hidden');
  state.selectedVillageId = null;
}

async function quickMarkDistributed(villageId, villageName) {
  state.selectedVillageId = villageId;
  await executeDistribution();
}

async function executeDistribution() {
  if (!state.selectedVillageId) return;
  if (!confirm(`Mark distribution complete for Village #${state.selectedVillageId}? This will atomically deduct stock and notify beneficiaries.`)) return;

  try {
    const res = await api('/villages/mark-distributed', 'POST', { village_id: state.selectedVillageId });
    showToast(`✓ Distribution completed! Stock deducted and SMS dispatched.`, 'success');
    closeReadinessDetail();
    loadOfficerVillages();
  } catch (err) {
    console.error(err);
  }
}

function openRequirementsModal(villageId, villageName) {
  document.getElementById('req-village-id').value = villageId;
  document.getElementById('req-modal-title').innerText = `Set Quota for ${villageName}`;
  showModal('modal-village-requirement');
}

async function handleUpsertVillageRequirement(e) {
  e.preventDefault();
  const village_id = document.getElementById('req-village-id').value;
  const commodity_id = parseInt(document.getElementById('req-commodity-select').value);
  const quantity = parseFloat(document.getElementById('req-quantity').value);

  try {
    await api(`/villages/${village_id}/requirements`, 'POST', { commodity_id, quantity });
    showToast('Village requirement quota saved!', 'success');
    closeModal('modal-village-requirement');
  } catch (err) {
    console.error(err);
  }
}

// ML Demand Forecasting
async function loadOfficerForecast() {
  const container = document.getElementById('forecast-grid');
  container.innerHTML = '<div class="card text-center p-4">Running linear regression demand forecasts...</div>';
  const shopId = state.user.shop_id || 1;

  try {
    const data = await api(`/forecast/${shopId}`);
    const forecasts = data.forecasts || [];

    if (forecasts.length === 0) {
      container.innerHTML = `
        <div class="card text-center p-4">
          <p>Not enough historical distribution data to train regression models.</p>
          <small class="text-muted">Need at least 2 distinct monthly distribution records per commodity.</small>
        </div>
      `;
      return;
    }

    container.innerHTML = forecasts.map(f => {
      const isRestock = f.recommendation === 'RESTOCK';
      const badgeClass = isRestock ? 'badge-danger' : 'badge-success';
      const trendText = f.trend_slope > 0 ? `+${f.trend_slope} kg/mo (Rising)` : `${f.trend_slope} kg/mo (Falling)`;

      const historyRows = (f.history || []).slice(-4).map(h => `
        <tr>
          <td>${h.month}</td>
          <td>${h.quantity} kg/L</td>
        </tr>
      `).join('');

      return `
        <div class="forecast-card">
          <div class="forecast-header">
            <div>
              <div class="forecast-commodity">${escapeHtml(f.commodity_name)}</div>
              <small class="text-muted">Model: Linear Regression</small>
            </div>
            <span class="badge ${badgeClass}">${f.recommendation}</span>
          </div>

          <div class="forecast-meta">
            <div>
              <div class="meta-item-label">Predicted Demand</div>
              <div class="meta-item-value">${f.predicted_next_month_demand} <small>kg/L</small></div>
            </div>
            <div>
              <div class="meta-item-label">Current Stock</div>
              <div class="meta-item-value">${f.current_stock} <small>kg/L</small></div>
            </div>
            <div>
              <div class="meta-item-label">Reorder Quota</div>
              <div class="meta-item-value" style="color:${isRestock ? 'var(--danger)' : 'var(--success)'};">${f.reorder_quantity} <small>kg/L</small></div>
            </div>
            <div>
              <div class="meta-item-label">Demand Trend</div>
              <div style="font-weight:600; font-size:0.95rem;">${trendText}</div>
            </div>
          </div>

          <div>
            <div class="meta-item-label">Recent Monthly Distributions:</div>
            <table class="forecast-history-table">
              <thead><tr><th>Month</th><th>Quantity</th></tr></thead>
              <tbody>${historyRows}</tbody>
            </table>
          </div>
        </div>
      `;
    }).join('');
  } catch (err) {
    container.innerHTML = `<div class="card text-center p-4 text-muted">Error calculating demand forecast: ${err.message}</div>`;
  }
}

async function handleAddForecastHistory(e) {
  e.preventDefault();
  const shop_id = state.user.shop_id || 1;
  const commodity_id = parseInt(document.getElementById('hist-commodity-select').value);
  const quantity = parseFloat(document.getElementById('hist-quantity').value);
  const distribution_date = document.getElementById('hist-date').value;

  try {
    await api('/forecast/history', 'POST', { shop_id, commodity_id, quantity, distribution_date });
    showToast('Historical distribution added!', 'success');
    closeModal('modal-add-history');
    loadOfficerForecast();
  } catch (err) {
    console.error(err);
  }
}

// Biometric verification
function fillMockBiometricToken(type) {
  const input = document.getElementById('bio-token');
  if (type === 'valid') {
    input.value = "mock_pass_" + Math.random().toString(36).substring(7);
  } else {
    input.value = "mock_fail_" + Math.random().toString(36).substring(7);
  }
}

async function handleBiometricVerify(e) {
  e.preventDefault();
  const beneficiary_id = parseInt(document.getElementById('bio-beneficiary-id').value);
  const capture_token = document.getElementById('bio-token').value.trim();
  const resultCard = document.getElementById('bio-result-content');

  resultCard.innerHTML = '<div class="text-center p-3">Verifying with Biometric Provider adapter...</div>';

  try {
    const res = await api(`/beneficiaries/${beneficiary_id}/biometric-verification`, 'POST', { capture_token });
    const isVerified = res.verified;

    resultCard.innerHTML = `
      <div style="padding:1rem;">
        <div class="badge badge-lg ${isVerified ? 'badge-success' : 'badge-danger'}" style="font-size:1.1rem; padding:0.5rem 1.25rem;">
          ${isVerified ? '✓ IDENTITY VERIFIED' : '✖ VERIFICATION FAILED'}
        </div>
        <div class="mt-3">
          <p><strong>Beneficiary ID:</strong> #${res.beneficiary_id}</p>
          <p><strong>Provider:</strong> ${res.provider} (Simulated: ${res.simulated})</p>
          <p><strong>Verification Ref:</strong> <code>${res.provider_reference}</code></p>
          <p><strong>Timestamp:</strong> ${formatDate(res.verified_at)}</p>
        </div>
      </div>
    `;

    if (isVerified) {
      showToast('Beneficiary identity verified successfully', 'success');
    } else {
      showToast('Biometric match failed!', 'error');
    }
  } catch (err) {
    resultCard.innerHTML = `<div class="text-danger p-3">Verification failed: ${err.message}</div>`;
  }
}

// Officer Complaints
async function loadOfficerComplaints() {
  const tbody = document.getElementById('officer-complaints-tbody');
  tbody.innerHTML = '<tr><td colspan="7" class="text-center">Loading complaints...</td></tr>';

  try {
    const complaints = await api('/complaints');
    if (complaints.length === 0) {
      tbody.innerHTML = '<tr><td colspan="7" class="text-center text-muted">No complaints filed for this shop.</td></tr>';
      return;
    }

    tbody.innerHTML = complaints.map(c => `
      <tr>
        <td><strong>#${c.id}</strong></td>
        <td>Beneficiary #${c.beneficiary_id}</td>
        <td><span class="badge badge-neutral">${escapeHtml(c.category)}</span></td>
        <td>${escapeHtml(c.description)}</td>
        <td>${getComplaintBadge(c.status)}</td>
        <td><small>${escapeHtml(c.resolution_note || '-')}</small></td>
        <td>
          <button class="btn btn-outline-sm" onclick="openResolveComplaintModal(${c.id}, '${c.status}', '${escapeHtml(c.resolution_note || '')}')">Resolve</button>
        </td>
      </tr>
    `).join('');
  } catch (err) {
    tbody.innerHTML = `<tr><td colspan="7" class="text-center text-muted">Failed to load complaints: ${err.message}</td></tr>`;
  }
}

function openResolveComplaintModal(id, currentStatus, currentNote) {
  document.getElementById('edit-complaint-id').value = id;
  document.getElementById('complaint-status-select').value = currentStatus || 'in_progress';
  document.getElementById('complaint-note').value = currentNote || '';
  showModal('modal-update-complaint');
}

async function handleUpdateComplaintStatus(e) {
  e.preventDefault();
  const id = document.getElementById('edit-complaint-id').value;
  const status = document.getElementById('complaint-status-select').value;
  const resolution_note = document.getElementById('complaint-note').value.trim() || null;

  try {
    await api(`/complaints/${id}/status`, 'PATCH', { status, resolution_note });
    showToast('Complaint status updated and beneficiary notified!', 'success');
    closeModal('modal-update-complaint');
    loadOfficerComplaints();
  } catch (err) {
    console.error(err);
  }
}

// ================= 9. BENEFICIARY FUNCTIONS =================
async function loadBeneficiaryStock() {
  const tbody = document.getElementById('beneficiary-stock-tbody');
  tbody.innerHTML = '<tr><td colspan="4" class="text-center">Loading stock...</td></tr>';
  const shopId = state.user.shop_id || 1;

  try {
    const [stockList, statusList] = await Promise.all([
      api('/stock'),
      api(`/commodity-status/${shopId}`).catch(() => [])
    ]);

    if (stockList.length === 0) {
      tbody.innerHTML = '<tr><td colspan="4" class="text-center text-muted">No stock reported for your assigned shop.</td></tr>';
      return;
    }

    const statusMap = {};
    statusList.forEach(s => { statusMap[s.commodity_id] = s; });

    tbody.innerHTML = stockList.map(item => {
      const s = statusMap[item.commodity_id] || { status: 'active', reason: '' };
      return `
        <tr>
          <td><strong>${escapeHtml(item.commodity_name)}</strong></td>
          <td><strong style="font-size:1.1rem;">${item.quantity}</strong> kg/L</td>
          <td>${getStatusBadge(s.status)}</td>
          <td><small class="text-muted">${escapeHtml(s.reason || 'Normal entitlement distribution')}</small></td>
        </tr>
      `;
    }).join('');
  } catch (err) {
    tbody.innerHTML = `<tr><td colspan="4" class="text-center text-muted">Failed to load stock: ${err.message}</td></tr>`;
  }
}

async function loadBeneficiaryVillages() {
  const tbody = document.getElementById('beneficiary-villages-tbody');
  tbody.innerHTML = '<tr><td colspan="3" class="text-center">Loading schedule...</td></tr>';

  try {
    const villages = await api('/villages');
    if (villages.length === 0) {
      tbody.innerHTML = '<tr><td colspan="3" class="text-center text-muted">No village info found.</td></tr>';
      return;
    }

    const rows = await Promise.all(villages.map(async v => {
      let readyBadge = '<span class="badge badge-neutral">Checking...</span>';
      try {
        const r = await api(`/villages/${v.id}/readiness`);
        readyBadge = r.is_ready ? '<span class="badge badge-success">✓ Stock Ready</span>' : '<span class="badge badge-warning">Awaiting Stock</span>';
      } catch {
        readyBadge = '<span class="badge badge-neutral">Pending</span>';
      }
      return `
        <tr>
          <td><strong>${escapeHtml(v.name)}</strong></td>
          <td>${v.last_distribution_date ? formatDate(v.last_distribution_date) : '<span class="text-muted">Not yet distributed</span>'}</td>
          <td>${readyBadge}</td>
        </tr>
      `;
    }));

    tbody.innerHTML = rows.join('');
  } catch (err) {
    tbody.innerHTML = `<tr><td colspan="3" class="text-center text-muted">Failed to load schedule: ${err.message}</td></tr>`;
  }
}

async function loadBeneficiaryComplaints() {
  const tbody = document.getElementById('beneficiary-complaints-tbody');
  tbody.innerHTML = '<tr><td colspan="5" class="text-center">Loading grievances...</td></tr>';

  try {
    const complaints = await api('/complaints');
    if (complaints.length === 0) {
      tbody.innerHTML = '<tr><td colspan="5" class="text-center text-muted">You have not submitted any complaints yet.</td></tr>';
      return;
    }

    tbody.innerHTML = complaints.map(c => `
      <tr>
        <td><strong>#${c.id}</strong></td>
        <td><span class="badge badge-neutral">${escapeHtml(c.category)}</span></td>
        <td>${escapeHtml(c.description)}</td>
        <td>${getComplaintBadge(c.status)}</td>
        <td>${c.resolution_note ? `<em>"${escapeHtml(c.resolution_note)}"</em>` : '<span class="text-muted">Under investigation</span>'}</td>
      </tr>
    `).join('');
  } catch (err) {
    tbody.innerHTML = `<tr><td colspan="5" class="text-center text-muted">Failed to load complaints: ${err.message}</td></tr>`;
  }
}

function getComplaintBadge(status) {
  if (status === 'resolved') return '<span class="badge badge-success">Resolved</span>';
  if (status === 'in_progress') return '<span class="badge badge-info">In Progress</span>';
  if (status === 'rejected') return '<span class="badge badge-danger">Rejected</span>';
  return `<span class="badge badge-warning">${escapeHtml(status || 'Pending')}</span>`;
}

async function handleCreateComplaint(e) {
  e.preventDefault();
  const category = document.getElementById('comp-category').value;
  const description = document.getElementById('comp-description').value.trim();

  try {
    await api('/complaints', 'POST', { category, description });
    showToast('Complaint submitted! You can track its status here.', 'success');
    closeModal('modal-add-complaint');
    document.getElementById('comp-description').value = '';
    loadBeneficiaryComplaints();
  } catch (err) {
    console.error(err);
  }
}

async function loadBeneficiaryNotifications() {
  const container = document.getElementById('notifications-list');
  container.innerHTML = '<div class="card text-center p-4">Loading SMS notifications...</div>';

  try {
    const list = await api('/notifications');
    if (list.length === 0) {
      container.innerHTML = '<div class="card text-center p-4 text-muted">No notifications received yet.</div>';
      return;
    }

    container.innerHTML = list.map(n => `
      <div class="notification-card">
        <div class="notif-header">
          <span>SMS Notification #${n.id}</span>
          <span>${formatDate(n.created_at)}</span>
        </div>
        <div class="notif-message-en">${escapeHtml(n.message_en)}</div>
        ${n.message_ta ? `<div class="notif-message-ta">${escapeHtml(n.message_ta)}</div>` : ''}
      </div>
    `).join('');
  } catch (err) {
    container.innerHTML = `<div class="card text-center p-4 text-muted">Failed to load alerts: ${err.message}</div>`;
  }
}

// ================= 10. MOCK API HANDLER FOR DEMO MODE =================
function handleMockApi(path, method, body) {
  return new Promise(resolve => {
    setTimeout(() => {
      // Mock endpoints
      if (path === '/shops') resolve(mockData.shops);
      else if (path === '/villages') resolve(mockData.villages);
      else if (path === '/commodities') resolve(mockData.commodities);
      else if (path === '/stock') resolve(mockData.stock);
      else if (path.startsWith('/commodity-status/')) resolve(mockData.commodity_status);
      else if (path.startsWith('/shops/') && path.endsWith('/next-village')) {
        resolve({ village_id: 1, village_name: "Kotturpuram", last_distribution_date: "2026-09-10" });
      }
      else if (path.startsWith('/villages/') && path.endsWith('/readiness')) {
        resolve({
          village_id: 1,
          village_name: "Kotturpuram",
          shop_id: 1,
          is_ready: true,
          missing_commodities: [],
          insufficient_stock: [],
          seasonally_suspended: [{ commodity_id: 5, commodity_name: "Palm Oil", required_quantity: 10 }]
        });
      }
      else if (path.startsWith('/forecast/')) {
        resolve({
          shop_id: 1,
          forecast_count: 3,
          forecasts: [
            {
              commodity_id: 1,
              commodity_name: "Rice",
              predicted_next_month_demand: 895.50,
              current_stock: 500.00,
              reorder_quantity: 395.50,
              recommendation: "RESTOCK",
              trend_slope: 35.40,
              history: [
                { month: "2026-05", quantity: 700 },
                { month: "2026-06", quantity: 740 },
                { month: "2026-07", quantity: 760 },
                { month: "2026-08", quantity: 810 },
                { month: "2026-09", quantity: 880 }
              ]
            },
            {
              commodity_id: 2,
              commodity_name: "Wheat",
              predicted_next_month_demand: 224.20,
              current_stock: 150.00,
              reorder_quantity: 74.20,
              recommendation: "RESTOCK",
              trend_slope: -6.20,
              history: [
                { month: "2026-06", quantity: 255 },
                { month: "2026-07", quantity: 240 },
                { month: "2026-08", quantity: 235 },
                { month: "2026-09", quantity: 230 }
              ]
            },
            {
              commodity_id: 3,
              commodity_name: "Sugar",
              predicted_next_month_demand: 158.00,
              current_stock: 190.00,
              reorder_quantity: 0.00,
              recommendation: "SUFFICIENT_STOCK",
              trend_slope: 2.10,
              history: [
                { month: "2026-07", quantity: 145 },
                { month: "2026-08", quantity: 155 },
                { month: "2026-09", quantity: 160 }
              ]
            }
          ]
        });
      }
      else if (path === '/complaints') resolve(mockData.complaints);
      else if (path === '/notifications') resolve(mockData.notifications);
      else if (path.startsWith('/audit-logs')) resolve(mockData.audit_logs);
      else if (path === '/villages/mark-distributed') {
        mockData.villages[0].last_distribution_date = new Date().toISOString().split('T')[0];
        resolve({ message: "Distribution marked completed" });
      }
      else if (path.includes('/biometric-verification')) {
        const isPass = (body && body.capture_token && !body.capture_token.includes('fail'));
        resolve({
          verification_id: 99,
          beneficiary_id: 1,
          verified: isPass,
          provider: "mock",
          provider_reference: "BIO-REF-" + Math.floor(Math.random() * 90000 + 10000),
          simulated: true,
          verified_at: new Date().toISOString()
        });
      }
      else {
        resolve({ status: "success" });
      }
    }, 150);
  });
}

// ================= 11. UTILITIES & INITIALIZATION =================
function escapeHtml(str) {
  if (str === null || str === undefined) return '';
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#039;');
}

function formatDate(dateStr) {
  if (!dateStr) return '-';
  try {
    const d = new Date(dateStr);
    return isNaN(d) ? dateStr : d.toLocaleDateString() + ' ' + d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  } catch {
    return dateStr;
  }
}

// Toggle Demo Mode
function setDemoMode(active) {
  state.demoMode = active;
  document.getElementById('demo-mode-state').innerText = active ? 'ON' : 'OFF';
  const pill = document.getElementById('backend-status-pill');
  const dot = document.getElementById('status-dot');
  const text = document.getElementById('status-text');

  if (active) {
    dot.className = 'status-dot demo';
    text.innerText = 'Demo Mode (Mock)';
    pill.style.background = '#fef3c7';
    showToast('Demo Mode enabled (offline mock data active)', 'info');
  } else {
    checkBackendHealth();
  }

  // Refresh current view if logged in
  if (state.user) {
    renderDashboard();
  }
}

// Health check to detect running API backend
async function checkBackendHealth() {
  const dot = document.getElementById('status-dot');
  const text = document.getElementById('status-text');
  const pill = document.getElementById('backend-status-pill');

  try {
    const res = await fetch(`${API_BASE}/docs`, { method: 'HEAD', mode: 'no-cors' });
    dot.className = 'status-dot online';
    text.innerText = 'API Connected';
    pill.style.background = '#d1fae5';
  } catch {
    // If backend isn't running, suggest demo mode
    dot.className = 'status-dot offline';
    text.innerText = 'API Offline';
    pill.style.background = '#fee2e2';
  }
}

// Event Listeners
document.getElementById('btn-logout').onclick = logout;
document.getElementById('btn-toggle-demo').onclick = () => setDemoMode(!state.demoMode);
document.getElementById('backend-status-pill').onclick = checkBackendHealth;

// Startup
window.addEventListener('DOMContentLoaded', () => {
  checkBackendHealth();

  if (state.token && state.user) {
    renderDashboard();
  } else {
    logout();
  }
});
