'use strict';

// ------------------------------------------------------------------ //
// Auth
// ------------------------------------------------------------------ //
const auth = {
  token: localStorage.getItem('auth_token'),
  user: JSON.parse(localStorage.getItem('auth_user') || 'null'),
};

function authHeaders() {
  return auth.token ? { 'Authorization': `Bearer ${auth.token}` } : {};
}

function doLogout() {
  localStorage.removeItem('auth_token');
  localStorage.removeItem('auth_user');
  location.href = '/';
}

async function apiFetch(url, options = {}) {
  const headers = { ...authHeaders(), ...(options.headers || {}) };
  const res = await fetch(url, { ...options, headers });
  if (res.status === 401 || res.status === 403) {
    doLogout();
    return;
  }
  return res;
}

// ------------------------------------------------------------------ //
// Init
// ------------------------------------------------------------------ //
document.addEventListener('DOMContentLoaded', () => {
  // Verificar que es admin
  if (!auth.token || !auth.user || auth.user.role !== 'admin') {
    location.href = '/';
    return;
  }
  document.getElementById('adminUserName').textContent = auth.user.name || auth.user.email;
  loadClients();
});

// ------------------------------------------------------------------ //
// Estado global
// ------------------------------------------------------------------ //
let _clients = [];
let _editingUserId = null;

// ------------------------------------------------------------------ //
// Clientes
// ------------------------------------------------------------------ //
async function loadClients() {
  try {
    const res = await apiFetch('/api/admin/clients');
    if (!res || !res.ok) return;
    _clients = await res.json();
    renderStats(_clients);
    renderClientsTable(_clients);
  } catch (e) {
    console.error('Error cargando clientes:', e);
  }
}

function renderStats(clients) {
  const total = clients.length;
  const activos = clients.filter(c => c.billing_status === 'activo').length;
  const suspendidos = clients.filter(c => c.billing_status === 'suspendido').length;
  const totalConsultas = clients.reduce((acc, c) => acc + (c.queries_used || 0), 0);

  document.getElementById('adminStats').innerHTML = `
    <div class="stat-card">
      <div class="stat-card-label">Total clientes</div>
      <div class="stat-card-value">${total}</div>
    </div>
    <div class="stat-card">
      <div class="stat-card-label">Activos</div>
      <div class="stat-card-value" style="color:var(--success)">${activos}</div>
    </div>
    <div class="stat-card">
      <div class="stat-card-label">Suspendidos</div>
      <div class="stat-card-value" style="color:var(--error)">${suspendidos}</div>
    </div>
    <div class="stat-card">
      <div class="stat-card-label">Consultas este mes</div>
      <div class="stat-card-value">${totalConsultas.toLocaleString('es-CL')}</div>
    </div>
  `;
}

function renderClientsTable(clients) {
  const container = document.getElementById('clientsTableBody');

  if (!clients.length) {
    container.innerHTML = `
      <div class="admin-empty">
        <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
          <path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/>
          <circle cx="9" cy="7" r="4"/>
        </svg>
        <p>No hay clientes registrados aún.</p>
      </div>`;
    return;
  }

  const rows = clients.map(c => {
    const pct = c.pct || 0;
    const pctDisplay = Math.round(pct * 100);
    const barClass = pct >= 1 ? 'danger' : pct >= 0.8 ? 'warning' : '';
    const statusClass = c.billing_status === 'activo' ? 'status-badge--activo' : 'status-badge--suspendido';
    const planClass = c.plan_id || 'base';
    const vps = c.tipo_vps === 'propio' ? 'Propio' : 'AgentIA';
    const resetDate = c.reset_date ? new Date(c.reset_date).toLocaleDateString('es-CL') : '—';

    return `
      <tr>
        <td>
          <div style="font-weight:600">${escapeHtml(c.name)}</div>
          <div style="font-size:12px;color:var(--text-muted)">${escapeHtml(c.email)}</div>
        </td>
        <td>${escapeHtml(c.company || '—')}</td>
        <td><span class="plan-badge ${planClass}">${escapeHtml(c.plan_name || c.plan_id)}</span></td>
        <td>
          <div style="display:flex;align-items:center;gap:6px;">
            <div class="progress-mini-wrap">
              <div class="progress-mini-fill ${barClass}" style="width:${Math.min(pctDisplay, 100)}%"></div>
            </div>
            <span style="font-size:12px;white-space:nowrap">${c.queries_used || 0} / ${c.queries_limit || 0}</span>
          </div>
          <div style="font-size:11px;color:var(--text-muted)">Reset: ${resetDate}</div>
        </td>
        <td><span class="status-badge plan-badge ${statusClass}">${c.billing_status || 'activo'}</span></td>
        <td style="font-size:12px">${vps}</td>
        <td>
          <button class="btn-action primary" onclick="openEditModal('${c.id}')">Editar</button>
        </td>
      </tr>
    `;
  }).join('');

  container.innerHTML = `
    <table class="admin-table">
      <thead>
        <tr>
          <th>Cliente</th>
          <th>Empresa</th>
          <th>Plan</th>
          <th>Consultas</th>
          <th>Estado</th>
          <th>VPS</th>
          <th>Acciones</th>
        </tr>
      </thead>
      <tbody>${rows}</tbody>
    </table>
  `;
}

// ------------------------------------------------------------------ //
// Modal: Crear cliente
// ------------------------------------------------------------------ //
function openCreateModal() {
  document.getElementById('createModal').style.display = 'flex';
  document.getElementById('createError').style.display = 'none';
  document.getElementById('createName').value = '';
  document.getElementById('createCompany').value = '';
  document.getElementById('createEmail').value = '';
  document.getElementById('createPlan').value = 'base';
  document.getElementById('createVps').value = 'agentia';
}

function closeCreateModal() {
  document.getElementById('createModal').style.display = 'none';
}

async function createClient() {
  const name = document.getElementById('createName').value.trim();
  const company = document.getElementById('createCompany').value.trim();
  const email = document.getElementById('createEmail').value.trim();
  const plan_id = document.getElementById('createPlan').value;
  const tipo_vps = document.getElementById('createVps').value;
  const errorEl = document.getElementById('createError');
  const btn = document.getElementById('createBtn');

  errorEl.style.display = 'none';

  if (!name || !email || !plan_id) {
    errorEl.textContent = 'Nombre, email y plan son obligatorios.';
    errorEl.style.display = 'block';
    return;
  }

  btn.disabled = true;
  btn.textContent = 'Creando...';

  try {
    const res = await apiFetch('/api/admin/clients', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name, company, email, plan_id, tipo_vps }),
    });
    const data = await res.json();
    if (!res.ok) {
      errorEl.textContent = data.detail || 'Error al crear el cliente';
      errorEl.style.display = 'block';
      return;
    }
    closeCreateModal();
    loadClients();
  } catch (e) {
    errorEl.textContent = 'Error de conexión';
    errorEl.style.display = 'block';
  } finally {
    btn.disabled = false;
    btn.textContent = 'Crear cliente';
  }
}

// ------------------------------------------------------------------ //
// Modal: Editar cliente
// ------------------------------------------------------------------ //
function openEditModal(userId) {
  const client = _clients.find(c => c.id === userId);
  if (!client) return;

  _editingUserId = userId;

  document.getElementById('editModal').style.display = 'flex';
  document.getElementById('editError').style.display = 'none';
  document.getElementById('editModalTitle').textContent = `${client.name} — ${client.company || ''}`;
  document.getElementById('editName').value = client.name || '';
  document.getElementById('editCompany').value = client.company || '';
  document.getElementById('editVps').value = client.tipo_vps || 'agentia';
  document.getElementById('editPlan').value = client.plan_id || 'base';
  document.getElementById('editExtraPacks').textContent = client.extra_packs || 0;
  document.getElementById('editResetDate').textContent = client.reset_date
    ? new Date(client.reset_date).toLocaleDateString('es-CL') : '—';

  const pct = client.pct || 0;
  const pctDisplay = Math.min(Math.round(pct * 100), 100);
  const barClass = pct >= 1 ? 'danger' : pct >= 0.8 ? 'warning' : '';
  const bar = document.getElementById('editProgressBar');
  bar.style.width = pctDisplay + '%';
  bar.className = 'progress-mini-fill ' + barClass;
  document.getElementById('editQuotaNumbers').textContent =
    `${client.queries_used || 0} / ${client.queries_limit || 0} (${pctDisplay}%)`;

  const statusBtn = document.getElementById('editStatusBtn');
  if (client.billing_status === 'suspendido') {
    statusBtn.textContent = 'Activar';
    statusBtn.className = 'btn-action success';
  } else {
    statusBtn.textContent = 'Suspender';
    statusBtn.className = 'btn-action danger';
  }

  const packBtn = document.getElementById('editAddPackBtn');
  packBtn.disabled = (client.extra_packs || 0) >= 2;
}

function closeEditModal() {
  document.getElementById('editModal').style.display = 'none';
  _editingUserId = null;
}

async function saveClient() {
  if (!_editingUserId) return;
  const errorEl = document.getElementById('editError');
  errorEl.style.display = 'none';

  const name = document.getElementById('editName').value.trim();
  const company = document.getElementById('editCompany').value.trim();
  const tipo_vps = document.getElementById('editVps').value;
  const plan_id = document.getElementById('editPlan').value;

  try {
    // Actualizar info
    await apiFetch(`/api/admin/clients/${_editingUserId}/info`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name, company, tipo_vps }),
    });

    // Cambiar plan si cambió
    const client = _clients.find(c => c.id === _editingUserId);
    if (client && client.plan_id !== plan_id) {
      const res = await apiFetch(`/api/admin/clients/${_editingUserId}/plan`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ plan_id }),
      });
      if (!res.ok) {
        const data = await res.json();
        errorEl.textContent = data.detail || 'Error cambiando plan';
        errorEl.style.display = 'block';
        return;
      }
    }

    closeEditModal();
    loadClients();
  } catch (e) {
    errorEl.textContent = 'Error de conexión';
    errorEl.style.display = 'block';
  }
}

async function addPackFromModal() {
  if (!_editingUserId) return;
  try {
    const res = await apiFetch(`/api/admin/clients/${_editingUserId}/extra-pack`, {
      method: 'POST',
    });
    const data = await res.json();
    if (!res.ok) {
      alert(data.detail || 'No se pudo agregar el paquete');
      return;
    }
    // Actualizar vista del modal
    const idx = _clients.findIndex(c => c.id === _editingUserId);
    if (idx >= 0) _clients[idx] = data;
    openEditModal(_editingUserId);
    renderClientsTable(_clients);
  } catch (e) {
    alert('Error de conexión');
  }
}

async function toggleStatusFromModal() {
  if (!_editingUserId) return;
  const client = _clients.find(c => c.id === _editingUserId);
  if (!client) return;
  const newStatus = client.billing_status === 'suspendido' ? 'activo' : 'suspendido';
  try {
    const res = await apiFetch(`/api/admin/clients/${_editingUserId}/status`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ billing_status: newStatus }),
    });
    const data = await res.json();
    if (!res.ok) {
      alert(data.detail || 'Error cambiando estado');
      return;
    }
    const idx = _clients.findIndex(c => c.id === _editingUserId);
    if (idx >= 0) _clients[idx] = data;
    openEditModal(_editingUserId);
    renderClientsTable(_clients);
  } catch (e) {
    alert('Error de conexión');
  }
}

// ------------------------------------------------------------------ //
// Vistas
// ------------------------------------------------------------------ //
function showView(view) {
  document.querySelectorAll('.admin-nav-item').forEach(el => el.classList.remove('active'));
  event.currentTarget.classList.add('active');
  if (view === 'clients') {
    document.getElementById('viewTitle').textContent = 'Clientes';
    document.getElementById('btnCreateClient').style.display = '';
  }
}

// ------------------------------------------------------------------ //
// Utils
// ------------------------------------------------------------------ //
function escapeHtml(str) {
  if (!str) return '';
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}
