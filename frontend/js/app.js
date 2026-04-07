/**
 * Agente Aduanas Chile — Frontend JS
 * Vanilla JS, sin dependencias externas.
 */

'use strict';

// ================================================================
// Autenticación
// ================================================================
const auth = {
  token: localStorage.getItem('auth_token'),
  user: JSON.parse(localStorage.getItem('auth_user') || 'null'),
};

function authHeaders() {
  return auth.token ? { 'Authorization': `Bearer ${auth.token}` } : {};
}

function isLoggedIn() {
  return !!auth.token && !!auth.user;
}

function saveAuth(token, user) {
  auth.token = token;
  auth.user = user;
  localStorage.setItem('auth_token', token);
  localStorage.setItem('auth_user', JSON.stringify(user));
}

function clearAuth() {
  auth.token = null;
  auth.user = null;
  localStorage.removeItem('auth_token');
  localStorage.removeItem('auth_user');
}

function showLoginForm() {
  document.getElementById('demoForm').style.display = 'none';
  document.getElementById('loginForm').style.display = 'block';
  document.getElementById('loginError').style.display = 'none';
}

function showDemoForm() {
  document.getElementById('loginForm').style.display = 'none';
  document.getElementById('demoForm').style.display = 'block';
  document.getElementById('demoError').style.display = 'none';
  document.getElementById('demoSuccess').style.display = 'none';
}

async function doLogin() {
  const email = document.getElementById('loginEmail').value.trim();
  const password = document.getElementById('loginPassword').value;
  const errorEl = document.getElementById('loginError');
  const btn = document.getElementById('loginBtn');

  errorEl.style.display = 'none';
  if (!email || !password) {
    errorEl.textContent = 'Ingresa tu correo y contraseña.';
    errorEl.style.display = 'block';
    return;
  }

  btn.disabled = true;
  btn.textContent = 'Ingresando...';

  try {
    const res = await fetch('/api/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email, password }),
    });
    const data = await res.json();
    if (!res.ok) {
      errorEl.textContent = data.detail || 'Error al iniciar sesión.';
      errorEl.style.display = 'block';
      return;
    }
    saveAuth(data.access_token, data.user);
    document.getElementById('loginScreen').style.display = 'none';
    initApp();
  } catch (e) {
    errorEl.textContent = 'Error de conexión. Intenta nuevamente.';
    errorEl.style.display = 'block';
  } finally {
    btn.disabled = false;
    btn.textContent = 'Ingresar';
  }
}

async function doRequestDemo() {
  const name = document.getElementById('demoName').value.trim();
  const company = document.getElementById('demoCompany').value.trim();
  const email = document.getElementById('demoEmail').value.trim();
  const errorEl = document.getElementById('demoError');
  const successEl = document.getElementById('demoSuccess');
  const btn = document.getElementById('demoBtn');

  errorEl.style.display = 'none';
  successEl.style.display = 'none';

  if (!name || !company || !email) {
    errorEl.textContent = 'Completa todos los campos.';
    errorEl.style.display = 'block';
    return;
  }

  btn.disabled = true;
  btn.textContent = 'Enviando...';

  try {
    const res = await fetch('/api/auth/demo', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name, company, email }),
    });
    const data = await res.json();
    if (!res.ok) {
      errorEl.textContent = data.detail || 'Error al solicitar acceso.';
      errorEl.style.display = 'block';
      return;
    }
    successEl.textContent = '¡Listo! Revisa tu correo con las credenciales de acceso.';
    successEl.style.display = 'block';
    btn.style.display = 'none';
  } catch (e) {
    errorEl.textContent = 'Error de conexión. Intenta nuevamente.';
    errorEl.style.display = 'block';
  } finally {
    btn.disabled = false;
    if (btn.style.display !== 'none') btn.textContent = 'Solicitar acceso';
  }
}

function logout() {
  clearAuth();
  location.reload();
}

// Wrapper de fetch que agrega token y maneja 401 automáticamente
async function apiFetch(url, options = {}) {
  const headers = { ...authHeaders(), ...(options.headers || {}) };
  const res = await fetch(url, { ...options, headers });
  if (res.status === 401 || res.status === 403) {
    const data = await res.json().catch(() => ({}));
    // Si el acceso demo expiró, mostrar mensaje antes de redirigir
    if (data.detail && data.detail.includes('expirado')) {
      alert(data.detail);
    }
    clearAuth();
    location.reload();
    return res;
  }
  return res;
}

// Reemplaza una miniatura rota con el ícono genérico de documento
function imgThumbError(img) {
  const parent = img.parentElement;
  if (!parent) return;
  parent.innerHTML = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="#94a3b8" stroke-width="1.5" style="width:36px;height:36px"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>';
}

// ================================================================
// Estado global
// ================================================================
const state = {
  currentFilter: 'all',
  messages: [],
  documents: [],
  isLoading: false,
  scraperPollingInterval: null,
  sidebarOpen: true,
};

const FILTER_LABELS = {
  all: 'Todas las fuentes',
  normativa: 'Solo normativa oficial',
  internos: 'Solo documentos internos',
};

const CONTENT_TYPE_LABELS = {
  circular: 'Circular',
  resolucion: 'Resolución',
  arancel: 'Arancel',
  procedimiento: 'Procedimiento',
  ley: 'Ley',
  decreto: 'Decreto',
  normativa: 'Normativa',
  pdf: 'PDF',
  word: 'Word',
  texto: 'TXT',
  documento: 'Documento',
  interno: 'Interno',
};

// ================================================================
// Inicialización
// ================================================================
function initApp() {
  // Mostrar info del usuario en sidebar
  if (auth.user) {
    const nameEl = document.getElementById('sidebarUserName');
    const roleEl = document.getElementById('sidebarUserRole');
    if (nameEl) nameEl.textContent = auth.user.name || auth.user.email;
    if (roleEl) roleEl.textContent = auth.user.role === 'admin' ? 'Administrador' : 'Demo';
  }
  initSidebarMobile();
  loadDocuments();
  loadScraperStatus();
  startScraperPolling();
  focusChatInput();
}

document.addEventListener('DOMContentLoaded', () => {
  if (!isLoggedIn()) {
    document.getElementById('loginScreen').style.display = 'flex';
  } else {
    initApp();
  }

  // Enter en los inputs de login
  document.getElementById('loginPassword').addEventListener('keydown', e => {
    if (e.key === 'Enter') doLogin();
  });
  document.getElementById('loginEmail').addEventListener('keydown', e => {
    if (e.key === 'Enter') doLogin();
  });
});

// ================================================================
// Chat
// ================================================================
async function sendMessage() {
  const input = document.getElementById('chatInput');
  const query = input.value.trim();

  if (!query || state.isLoading) return;

  // Ocultar pantalla de bienvenida
  hideWelcomeScreen();

  // Añadir mensaje del usuario
  addUserMessage(query);
  input.value = '';
  autoResize(input);

  // Mostrar indicador de carga
  const loadingId = addLoadingMessage();

  state.isLoading = true;
  setInputEnabled(false);

  try {
    const response = await apiFetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        query: query,
        filter: state.currentFilter,
      }),
    });

    if (!response.ok) {
      const err = await response.json().catch(() => ({ detail: 'Error desconocido' }));
      throw new Error(err.detail || `Error ${response.status}`);
    }

    const data = await response.json();

    // Reemplazar mensaje de carga con la respuesta
    removeMessage(loadingId);
    addAssistantMessage(data.answer, data.sources || []);

  } catch (error) {
    removeMessage(loadingId);
    addAssistantMessage(
      `Lo siento, ocurrió un error al procesar su consulta: ${error.message}`,
      []
    );
    showToast('Error al conectar con el servidor', 'error');
  } finally {
    state.isLoading = false;
    setInputEnabled(true);
    focusChatInput();
  }
}

function sendSuggestion(text) {
  const input = document.getElementById('chatInput');
  input.value = text;
  autoResize(input);
  sendMessage();
}

function addUserMessage(text) {
  const id = generateId();
  const message = { id, role: 'user', text };
  state.messages.push(message);

  const el = createUserMessageEl(id, text);
  document.getElementById('messagesContainer').appendChild(el);
  scrollToBottom();
  return id;
}

function addAssistantMessage(text, sources) {
  const id = generateId();
  const message = { id, role: 'assistant', text, sources };
  state.messages.push(message);

  const el = createAssistantMessageEl(id, text, sources);
  document.getElementById('messagesContainer').appendChild(el);
  scrollToBottom();
  return id;
}

function addLoadingMessage() {
  const id = generateId();
  const el = createLoadingMessageEl(id);
  document.getElementById('messagesContainer').appendChild(el);
  scrollToBottom();
  return id;
}

function removeMessage(id) {
  const el = document.getElementById(`msg-${id}`);
  if (el) el.remove();
}

function clearChat() {
  state.messages = [];
  document.getElementById('messagesContainer').innerHTML = '';
  document.getElementById('welcomeScreen').style.display = '';
  focusChatInput();
}

// ---- Creación de elementos de mensaje ----

function createUserMessageEl(id, text) {
  const el = document.createElement('div');
  el.id = `msg-${id}`;
  el.className = 'message message--user';
  el.innerHTML = `
    <div class="message-avatar">
      <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
        <path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/>
        <circle cx="12" cy="7" r="4"/>
      </svg>
    </div>
    <div class="message-bubble">
      <div class="message-text">${escapeHtml(text)}</div>
    </div>
  `;
  return el;
}

function createAssistantMessageEl(id, text, sources) {
  const el = document.createElement('div');
  el.id = `msg-${id}`;
  el.className = 'message message--assistant';

  const sourcesHtml = buildSourcesHtml(sources);

  el.innerHTML = `
    <div class="message-avatar">
      <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
        <path d="M3 9l9-7 9 7v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>
        <polyline points="9 22 9 12 15 12 15 22"/>
      </svg>
    </div>
    <div class="message-bubble">
      <div class="message-text">${formatMessageText(text)}</div>
      ${sourcesHtml}
    </div>
  `;
  return el;
}

function createLoadingMessageEl(id) {
  const el = document.createElement('div');
  el.id = `msg-${id}`;
  el.className = 'message message--assistant message--loading';
  el.innerHTML = `
    <div class="message-avatar">
      <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
        <path d="M3 9l9-7 9 7v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>
        <polyline points="9 22 9 12 15 12 15 22"/>
      </svg>
    </div>
    <div class="message-bubble">
      <div class="message-text">
        Consultando base de datos normativa
        <div class="typing-dots">
          <span></span><span></span><span></span>
        </div>
      </div>
    </div>
  `;
  return el;
}

function buildSourcesHtml(sources) {
  if (!sources || sources.length === 0) return '';

  const itemsHtml = sources.map(src => {
    const typeLabel = CONTENT_TYPE_LABELS[src.content_type] || src.content_type || 'Fuente';
    const displayLabel = src.title || src.source || 'Fuente desconocida';
    return `<li class="source-text-item"><span class="source-text-type">${escapeHtml(typeLabel)}</span>${escapeHtml(displayLabel)}</li>`;
  }).join('');

  return `
    <div class="message-sources">
      <div class="sources-label">Fuentes consultadas</div>
      <ul class="sources-text-list">${itemsHtml}</ul>
    </div>
  `;
}

function formatMessageText(text) {
  if (!text) return '';

  const lines = text.split('\n');
  const output = [];
  let i = 0;

  while (i < lines.length) {
    const line = lines[i];

    // --- Tabla markdown ---
    if (line.trim().startsWith('|') && line.trim().endsWith('|')) {
      const tableLines = [];
      while (i < lines.length && lines[i].trim().startsWith('|') && lines[i].trim().endsWith('|')) {
        tableLines.push(lines[i]);
        i++;
      }
      output.push(renderTable(tableLines));
      continue;
    }

    // --- Encabezados ---
    const h4 = line.match(/^#{4,} (.+)/);
    const h3 = line.match(/^### (.+)/);
    const h2 = line.match(/^## (.+)/);
    const h1 = line.match(/^# (.+)/);
    if (h4) { output.push(`<p class="md-h4">${inlineFormat(h4[1])}</p>`); i++; continue; }
    if (h3) { output.push(`<h3 class="md-h3">${inlineFormat(h3[1])}</h3>`); i++; continue; }
    if (h2) { output.push(`<h2 class="md-h2">${inlineFormat(h2[1])}</h2>`); i++; continue; }
    if (h1) { output.push(`<h1 class="md-h1">${inlineFormat(h1[1])}</h1>`); i++; continue; }

    // --- Separador horizontal ---
    if (/^---+$/.test(line.trim())) { output.push('<hr class="md-hr">'); i++; continue; }

    // --- Bloque de código ---
    if (line.trim().startsWith('```')) {
      const codeLines = [];
      i++;
      while (i < lines.length && !lines[i].trim().startsWith('```')) {
        codeLines.push(escapeHtml(lines[i]));
        i++;
      }
      output.push(`<pre class="md-pre"><code>${codeLines.join('\n')}</code></pre>`);
      i++;
      continue;
    }

    // --- Blockquote ---
    if (line.startsWith('> ')) {
      const quoteLines = [];
      while (i < lines.length && lines[i].startsWith('> ')) {
        quoteLines.push(inlineFormat(lines[i].slice(2)));
        i++;
      }
      output.push(`<blockquote class="md-quote">${quoteLines.join('<br>')}</blockquote>`);
      continue;
    }

    // --- Lista no ordenada ---
    if (/^[-*] /.test(line)) {
      const items = [];
      while (i < lines.length && /^[-*] /.test(lines[i])) {
        items.push(`<li>${inlineFormat(lines[i].replace(/^[-*] /, ''))}</li>`);
        i++;
      }
      output.push(`<ul class="md-ul">${items.join('')}</ul>`);
      continue;
    }

    // --- Lista ordenada ---
    if (/^\d+\. /.test(line)) {
      const items = [];
      while (i < lines.length && /^\d+\. /.test(lines[i])) {
        items.push(`<li>${inlineFormat(lines[i].replace(/^\d+\. /, ''))}</li>`);
        i++;
      }
      output.push(`<ol class="md-ol">${items.join('')}</ol>`);
      continue;
    }

    // --- Línea vacía ---
    if (line.trim() === '') { output.push('<br>'); i++; continue; }

    // --- Párrafo normal ---
    output.push(`<p class="md-p">${inlineFormat(line)}</p>`);
    i++;
  }

  return output.join('');
}

function inlineFormat(text) {
  if (!text) return '';
  let t = escapeHtml(text);
  // Código inline
  t = t.replace(/`([^`]+)`/g, '<code class="md-code">$1</code>');
  // Negrita
  t = t.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
  // Cursiva
  t = t.replace(/\*(.+?)\*/g, '<em>$1</em>');
  // Enlace [texto](url)
  t = t.replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" target="_blank" rel="noopener" class="md-link">$1</a>');
  return t;
}

function renderTable(lines) {
  const rows = lines.map(l =>
    l.trim().replace(/^\||\|$/g, '').split('|').map(c => c.trim())
  );
  // La segunda fila es el separador (---|---), la omitimos
  const header = rows[0];
  const body = rows.slice(2);

  const headerHtml = header.map(c => `<th>${inlineFormat(c)}</th>`).join('');
  const bodyHtml = body.map(row =>
    `<tr>${row.map(c => `<td>${inlineFormat(c)}</td>`).join('')}</tr>`
  ).join('');

  return `<div class="md-table-wrap"><table class="md-table"><thead><tr>${headerHtml}</tr></thead><tbody>${bodyHtml}</tbody></table></div>`;
}

// ================================================================
// Documentos internos
// ================================================================
async function loadDocuments() {
  try {
    const response = await apiFetch('/api/documents');
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    state.documents = data.documents || [];
    renderDocumentsList();
  } catch (error) {
    console.error('Error cargando documentos:', error);
  }
}

function renderDocumentsList() {
  // Actualizar contador del botón en sidebar
  const count = document.getElementById('explorerCount');
  if (count) count.textContent = state.documents.length || '0';
  // Si el explorador está abierto, actualizarlo también
  if (document.getElementById('explorerModal').style.display !== 'none') {
    renderExplorerBody();
  }
}

// ================================================================
// Explorador de documentos
// ================================================================
let _explorerView = 'grid';
let _explorerSearch = '';

async function openExplorer() {
  document.getElementById('explorerModal').style.display = 'flex';
  // Limpiar buscador al abrir
  _explorerSearch = '';
  const searchInput = document.getElementById('explorerSearch');
  if (searchInput) searchInput.value = '';
  // Siempre refrescar documentos al abrir el explorador
  await loadDocuments();
  renderExplorerBody();
}

function closeExplorer() {
  document.getElementById('explorerModal').style.display = 'none';
}

function filterExplorer(value) {
  _explorerSearch = (value || '').trim().toLowerCase();
  renderExplorerBody();
}

function setExplorerView(view) {
  _explorerView = view;
  document.getElementById('viewGrid').classList.toggle('active', view === 'grid');
  document.getElementById('viewList').classList.toggle('active', view === 'list');
  renderExplorerBody();
}

function docUrl(filename) {
  return '/api/uploads/' + encodeURIComponent(filename || '');
}

function renderExplorerBody() {
  const body = document.getElementById('explorerBody');
  const total = document.getElementById('explorerTotal');
  const allDocs = state.documents;
  const docs = _explorerSearch
    ? allDocs.filter(d =>
        (d.title || '').toLowerCase().includes(_explorerSearch) ||
        (d.filename || '').toLowerCase().includes(_explorerSearch)
      )
    : allDocs;

  const totalLabel = _explorerSearch && docs.length < allDocs.length
    ? `${docs.length} de ${allDocs.length} archivo${allDocs.length !== 1 ? 's' : ''}`
    : `${allDocs.length} archivo${allDocs.length !== 1 ? 's' : ''}`;
  total.textContent = totalLabel;

  if (docs.length === 0) {
    const emptyMsg = _explorerSearch
      ? `<p>Sin resultados para "<strong>${escapeHtml(_explorerSearch)}</strong>"</p><span>Intente con otro término</span>`
      : `<p>No hay documentos subidos aún</p><span>Arrastra archivos a la barra lateral para indexarlos</span>`;
    body.innerHTML = `<div class="explorer-empty"><svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>${emptyMsg}</div>`;
    return;
  }

  const IMAGE_TYPES = ['imagen', 'png', 'jpg', 'jpeg', 'tiff', 'bmp', 'webp'];
  const TYPE_ICONS = {
    pdf: `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/></svg>`,
    word: `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>`,
    texto: `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/></svg>`,
  };

  const makeCard = (doc, isGrid) => {
    const ct = (doc.content_type || '').toLowerCase();
    const isImage = IMAGE_TYPES.includes(ct);
    const isPdf = ct === 'pdf';
    const url = docUrl(doc.filename);
    const clickAction = isImage
      ? `openDocPreview(event,'image','${escapeAttr(url)}','${escapeAttr(doc.title || doc.filename)}')`
      : isPdf
        ? `openDocPreview(event,'pdf','${escapeAttr(url)}','${escapeAttr(doc.title || doc.filename)}')`
        : `openDocPreview(event,'download','${escapeAttr(url)}','${escapeAttr(doc.filename || '')}')`;

    if (isGrid) {
      const thumb = isImage
        ? `<div class="explorer-card-thumb"><img src="${url}" alt="" onerror="imgThumbError(this)"/></div>`
        : `<div class="explorer-card-icon">${TYPE_ICONS[ct] || TYPE_ICONS.pdf}</div>`;
      return `<div class="explorer-file-card" onclick="${clickAction}">
        ${thumb}
        <div class="explorer-file-info">
          <span class="explorer-file-name" title="${escapeAttr(doc.title || doc.filename)}">${escapeHtml(truncate(doc.title || doc.filename, 28))}</span>
          <span class="explorer-file-meta">${escapeHtml(doc.content_type || 'doc')} · ${doc.total_chunks} chunks${doc.date ? ' · ' + escapeHtml(doc.date) : ''}</span>
        </div>
        <button class="explorer-delete-btn" onclick="event.stopPropagation();deleteDocument('${escapeAttr(doc.doc_id)}','${escapeAttr(doc.title || doc.filename)}')" title="Eliminar">
          <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6"/><path d="M14 11v6"/></svg>
        </button>
      </div>`;
    } else {
      const icon = isImage
        ? `<div class="explorer-list-thumb"><img src="${url}" alt="" onerror="imgThumbError(this)"/></div>`
        : `<div class="explorer-list-icon">${TYPE_ICONS[ct] || TYPE_ICONS.pdf}</div>`;
      return `<div class="explorer-list-row" onclick="${clickAction}">
        ${icon}
        <div class="explorer-list-info">
          <span class="explorer-file-name" title="${escapeAttr(doc.title || doc.filename)}">${escapeHtml(truncate(doc.title || doc.filename, 40))}</span>
          <span class="explorer-file-meta">${escapeHtml(doc.content_type || 'doc')} · ${doc.total_chunks} chunks${doc.date ? ' · ' + escapeHtml(doc.date) : ''}</span>
        </div>
        <button class="explorer-delete-btn" onclick="event.stopPropagation();deleteDocument('${escapeAttr(doc.doc_id)}','${escapeAttr(doc.title || doc.filename)}')" title="Eliminar">
          <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6"/><path d="M14 11v6"/></svg>
        </button>
      </div>`;
    }
  };

  const isGrid = _explorerView === 'grid';
  const wrapClass = isGrid ? 'explorer-grid' : 'explorer-list';
  body.innerHTML = `<div class="${wrapClass}">${docs.map(d => makeCard(d, isGrid)).join('')}</div>`;
}

function openDocPreview(event, type, url, label) {
  event.stopPropagation();
  if (type === 'image') {
    showImagePreview(url, label);
  } else if (type === 'pdf') {
    showPdfPreview(url, label);
  } else {
    // Descarga directa para Word, TXT, etc.
    const a = document.createElement('a');
    a.href = url;
    a.download = label;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
  }
}

function showPdfPreview(url, title) {
  document.getElementById('pdfPreviewTitle').textContent = title || '';
  document.getElementById('pdfPreviewFrame').src = url;
  document.getElementById('pdfPreviewOverlay').style.display = 'flex';
}

function closePdfPreview() {
  document.getElementById('pdfPreviewOverlay').style.display = 'none';
  document.getElementById('pdfPreviewFrame').src = '';
}

function openSourcesModal() {
  document.getElementById('sourcesModal').style.display = 'flex';
}

function closeSourcesModal() {
  document.getElementById('sourcesModal').style.display = 'none';
}


async function deleteDocument(docId, docName) {
  if (!confirm(`¿Desea eliminar el documento "${docName}"? Esta acción no se puede deshacer.`)) {
    return;
  }

  const el = document.getElementById(`doc-${docId}`);
  if (el) {
    el.style.opacity = '0.4';
    el.style.pointerEvents = 'none';
  }

  try {
    const response = await apiFetch(`/api/documents/${encodeURIComponent(docId)}`, {
      method: 'DELETE',
    });

    if (!response.ok) {
      const err = await response.json().catch(() => ({ detail: 'Error desconocido' }));
      throw new Error(err.detail || `Error ${response.status}`);
    }

    state.documents = state.documents.filter(d => d.doc_id !== docId);
    renderDocumentsList();
    showToast(`Documento "${truncate(docName, 40)}" eliminado`, 'success');
  } catch (error) {
    if (el) {
      el.style.opacity = '';
      el.style.pointerEvents = '';
    }
    showToast(`Error al eliminar: ${error.message}`, 'error');
  }
}

// ================================================================
// Carga de archivos
// ================================================================
function handleDragOver(event) {
  event.preventDefault();
  event.stopPropagation();
  document.getElementById('uploadZone').classList.add('drag-over');
}

function handleDragLeave(event) {
  event.preventDefault();
  event.stopPropagation();
  document.getElementById('uploadZone').classList.remove('drag-over');
}

function handleDrop(event) {
  event.preventDefault();
  event.stopPropagation();
  document.getElementById('uploadZone').classList.remove('drag-over');

  const files = Array.from(event.dataTransfer.files);
  if (files.length > 0) {
    uploadFiles(files);
  }
}

function handleFileSelect(event) {
  const files = Array.from(event.target.files);
  if (files.length > 0) {
    uploadFiles(files);
    event.target.value = ''; // reset para permitir re-subida del mismo archivo
  }
}

// Cola de archivos pendientes de subir y resolvers del modal
let _uploadQueue = [];
let _uploadQueueIndex = 0;
let _modalResolve = null;

async function uploadFiles(files) {
  const validExts = ['.pdf', '.docx', '.txt', '.png', '.jpg', '.jpeg', '.tiff', '.tif', '.bmp', '.webp'];
  _uploadQueue = [];

  for (const file of files) {
    const ext = '.' + file.name.split('.').pop().toLowerCase();
    if (!validExts.includes(ext)) {
      showToast(`Formato no soportado: ${file.name}`, 'warning');
      continue;
    }
    if (file.size > 50 * 1024 * 1024) {
      showToast(`Archivo demasiado grande: ${file.name} (máx. 50 MB)`, 'warning');
      continue;
    }
    _uploadQueue.push(file);
  }

  for (_uploadQueueIndex = 0; _uploadQueueIndex < _uploadQueue.length; _uploadQueueIndex++) {
    const file = _uploadQueue[_uploadQueueIndex];
    const meta = await showMetadataModal(file);
    if (!meta) continue; // usuario canceló

    await doUpload(file, meta, _uploadQueueIndex + 1, _uploadQueue.length);
  }
}

function showMetadataModal(file) {
  return new Promise(resolve => {
    _modalResolve = resolve;
    const ext = '.' + file.name.split('.').pop().toLowerCase();
    const isImage = ['.png', '.jpg', '.jpeg', '.tiff', '.tif', '.bmp', '.webp'].includes(ext);

    document.getElementById('modalFileName').textContent = file.name;
    document.getElementById('metaTitle').value = file.name.replace(/\.[^/.]+$/, '');
    document.getElementById('metaType').value = isImage ? 'imagen' : 'documento';
    document.getElementById('metaDate').value = new Date().toISOString().slice(0, 10);
    document.getElementById('metaSource').value = 'Documento interno';

    document.getElementById('metadataModal').style.display = 'flex';
  });
}

function confirmUpload() {
  const meta = {
    title: document.getElementById('metaTitle').value.trim() || document.getElementById('modalFileName').textContent.replace(/\.[^/.]+$/, ''),
    content_type: document.getElementById('metaType').value,
    date: document.getElementById('metaDate').value,
    source: document.getElementById('metaSource').value.trim() || 'Documento interno',
  };
  document.getElementById('metadataModal').style.display = 'none';
  if (_modalResolve) { _modalResolve(meta); _modalResolve = null; }
}

function showImagePreview(url, title) {
  const overlay = document.getElementById('imagePreviewOverlay');
  document.getElementById('imagePreviewImg').src = url;
  document.getElementById('imagePreviewTitle').textContent = title || '';
  overlay.style.display = 'flex';
}

function closeImagePreview() {
  document.getElementById('imagePreviewOverlay').style.display = 'none';
}

function cancelUpload() {
  document.getElementById('metadataModal').style.display = 'none';
  if (_modalResolve) { _modalResolve(null); _modalResolve = null; }
}

async function doUpload(file, meta, current, total) {
  const overlay = document.getElementById('uploadProgressOverlay');
  const progressText = document.getElementById('uploadProgressText');

  overlay.style.display = 'flex';
  progressText.textContent = `Procesando ${file.name}…${total > 1 ? ` (${current}/${total})` : ''}`;

  const slowTimer = setTimeout(() => {
    progressText.textContent = `Procesando ${file.name}… (Esto puede tardar unos momentos)`;
  }, 8000);

  try {
    const formData = new FormData();
    formData.append('file', file);
    formData.append('title', meta.title);
    formData.append('content_type', meta.content_type);
    formData.append('date', meta.date);
    formData.append('source', meta.source);

    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 5 * 60 * 1000);

    const response = await apiFetch('/api/documents/upload', {
      method: 'POST',
      body: formData,
      signal: controller.signal,
    });
    clearTimeout(timeout);

    if (!response.ok) {
      const err = await response.json().catch(() => ({ detail: 'Error desconocido' }));
      throw new Error(err.detail || `Error ${response.status}`);
    }

    const data = await response.json();
    showToast(`"${meta.title}" indexado correctamente (${data.chunks_created} chunks)`, 'success');
    await loadDocuments();

  } catch (error) {
    const msg = error.name === 'AbortError'
      ? `Tiempo de espera agotado al subir "${file.name}".`
      : `Error al subir "${file.name}": ${error.message}`;
    showToast(msg, 'error');
  } finally {
    clearTimeout(slowTimer);
    overlay.style.display = 'none';
  }
}

// ================================================================
// Estado del scraper
// ================================================================
async function loadScraperStatus() {
  try {
    const response = await apiFetch('/api/scraper/status');
    if (!response.ok) return;

    const data = await response.json();
    updateScraperStatusUI(data);
  } catch (error) {
    console.error('Error cargando estado del scraper:', error);
  }
}

function updateScraperStatusUI(data) {
  const badge = document.getElementById('scraperStatusBadge');
  const totalDocs = document.getElementById('scraperTotalDocs');
  const lastRun = document.getElementById('scraperLastRun');
  const nextRun = document.getElementById('scraperNextRun');
  const btnUpdate = document.getElementById('btnUpdateNow');

  // Badge de estado
  badge.className = 'status-badge';
  if (data.is_running) {
    badge.textContent = 'Ejecutando…';
    badge.classList.add('running');
    btnUpdate.disabled = true;
    btnUpdate.classList.add('spinning');
  } else if (data.status === 'completado') {
    badge.textContent = 'Activo';
    badge.classList.add('ok');
    btnUpdate.disabled = false;
    btnUpdate.classList.remove('spinning');
  } else if (data.status === 'error') {
    badge.textContent = 'Error';
    badge.classList.add('error');
    btnUpdate.disabled = false;
    btnUpdate.classList.remove('spinning');
  } else {
    badge.textContent = 'Inactivo';
    btnUpdate.disabled = false;
    btnUpdate.classList.remove('spinning');
  }

  // Documentos totales
  totalDocs.textContent = data.total_docs !== undefined
    ? data.total_docs.toLocaleString('es-CL') + ' chunks'
    : '—';

  // Fechas
  lastRun.textContent = data.last_run ? formatDateRelative(data.last_run) : 'Nunca';
  nextRun.textContent = data.next_run ? formatDateRelative(data.next_run) : '—';
}

function startScraperPolling() {
  if (state.scraperPollingInterval) return;
  state.scraperPollingInterval = setInterval(loadScraperStatus, 30000);
}

async function triggerScraper() {
  const btn = document.getElementById('btnUpdateNow');
  btn.disabled = true;
  btn.classList.add('spinning');

  try {
    const response = await apiFetch('/api/scraper/run', { method: 'POST' });
    if (!response.ok) {
      const err = await response.json().catch(() => ({ detail: 'Error desconocido' }));
      throw new Error(err.detail || `Error ${response.status}`);
    }

    showToast('Actualización iniciada en segundo plano', 'info');

    // Actualizar estado después de un momento
    setTimeout(loadScraperStatus, 2000);
    setTimeout(loadScraperStatus, 10000);
  } catch (error) {
    showToast(`Error: ${error.message}`, 'error');
    btn.disabled = false;
    btn.classList.remove('spinning');
  }
}

async function resetNormativa() {
  const word = window.prompt(
    'Esta acción borrará toda la normativa indexada y la re-descargará desde cero.\n\n' +
    'Para confirmar, escriba exactamente:  LIMPIAR'
  );
  if (word === null) return;           // canceló
  if (word.trim() !== 'LIMPIAR') {
    showToast('Texto incorrecto. La operación fue cancelada.', 'error');
    return;
  }

  const btn = document.getElementById('btnResetNormativa');
  btn.disabled = true;
  btn.textContent = 'Limpiando…';

  try {
    const response = await apiFetch('/api/scraper/reset', { method: 'POST' });
    if (!response.ok) {
      const err = await response.json().catch(() => ({ detail: 'Error desconocido' }));
      throw new Error(err.detail || `Error ${response.status}`);
    }
    showToast('Base limpiada. Re-indexando normativa en segundo plano…', 'info');
    setTimeout(loadScraperStatus, 2000);
    setTimeout(loadScraperStatus, 15000);
  } catch (error) {
    showToast(`Error: ${error.message}`, 'error');
  } finally {
    btn.disabled = false;
    btn.textContent = 'Limpiar y re-indexar';
  }
}

// ================================================================
// Filtros
// ================================================================
function setFilter(filter, buttonEl) {
  state.currentFilter = filter;

  // Actualizar botones
  document.querySelectorAll('.filter-btn').forEach(btn => {
    btn.classList.remove('active');
  });
  if (buttonEl) buttonEl.classList.add('active');

  // Actualizar indicador en el input
  document.getElementById('filterIndicatorText').textContent = FILTER_LABELS[filter] || 'Todas las fuentes';
}

// ================================================================
// Sidebar toggle
// ================================================================
function isMobile() {
  return window.innerWidth <= 768 || (window.innerWidth <= 900 && window.innerHeight < window.innerWidth);
}

function toggleSidebar() {
  const sidebar = document.getElementById('sidebar');
  const overlay = document.getElementById('sidebarOverlay');
  state.sidebarOpen = !state.sidebarOpen;
  if (state.sidebarOpen) {
    sidebar.classList.remove('collapsed');
    if (overlay && isMobile()) overlay.classList.add('active');
  } else {
    sidebar.classList.add('collapsed');
    if (overlay) overlay.classList.remove('active');
  }
}

function initSidebarMobile() {
  if (isMobile()) {
    const sidebar = document.getElementById('sidebar');
    const overlay = document.getElementById('sidebarOverlay');
    sidebar.classList.add('collapsed');
    state.sidebarOpen = false;
    if (overlay) overlay.classList.remove('active');
  }
}

window.addEventListener('resize', () => {
  const overlay = document.getElementById('sidebarOverlay');
  if (!isMobile() && overlay) {
    overlay.classList.remove('active');
    const sidebar = document.getElementById('sidebar');
    sidebar.classList.remove('collapsed');
    state.sidebarOpen = true;
  }
});

// ================================================================
// Utilidades de UI
// ================================================================
function hideWelcomeScreen() {
  const ws = document.getElementById('welcomeScreen');
  if (ws) ws.style.display = 'none';
}

function scrollToBottom() {
  const chatArea = document.getElementById('chatArea');
  chatArea.scrollTop = chatArea.scrollHeight;
}

function focusChatInput() {
  const input = document.getElementById('chatInput');
  if (input) input.focus();
}

function setInputEnabled(enabled) {
  const input = document.getElementById('chatInput');
  const btn = document.getElementById('sendButton');
  input.disabled = !enabled;
  btn.disabled = !enabled;
}

function handleKeyDown(event) {
  if (event.key === 'Enter' && !event.shiftKey) {
    event.preventDefault();
    sendMessage();
  }
}

function autoResize(el) {
  el.style.height = 'auto';
  el.style.height = Math.min(el.scrollHeight, 140) + 'px';
}

function showToast(message, type = 'info') {
  const container = document.getElementById('toastContainer');
  const id = generateId();

  const icons = {
    success: `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="20 6 9 17 4 12"/></svg>`,
    error: `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><circle cx="12" cy="12" r="10"/><line x1="15" y1="9" x2="9" y2="15"/><line x1="9" y1="9" x2="15" y2="15"/></svg>`,
    info: `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>`,
    warning: `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>`,
  };

  const toast = document.createElement('div');
  toast.id = `toast-${id}`;
  toast.className = `toast toast--${type}`;
  toast.innerHTML = `${icons[type] || icons.info} <span>${escapeHtml(message)}</span>`;
  container.appendChild(toast);

  // Auto-remove
  setTimeout(() => {
    const el = document.getElementById(`toast-${id}`);
    if (el) {
      el.style.animation = 'none';
      el.style.opacity = '0';
      el.style.transform = 'translateX(100%)';
      el.style.transition = 'all 0.3s ease';
      setTimeout(() => el.remove(), 300);
    }
  }, 4500);
}

// ================================================================
// Helpers
// ================================================================
function generateId() {
  return Math.random().toString(36).substr(2, 9);
}

function escapeHtml(str) {
  if (!str) return '';
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#x27;');
}

function escapeAttr(str) {
  if (!str) return '';
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#x27;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');
}

function truncate(str, maxLen) {
  if (!str) return '';
  return str.length > maxLen ? str.slice(0, maxLen) + '…' : str;
}

function formatDateRelative(isoString) {
  if (!isoString) return '—';
  try {
    const date = new Date(isoString);
    if (isNaN(date.getTime())) return isoString;

    const now = new Date();
    const diffMs = now - date;
    const diffMins = Math.floor(diffMs / 60000);
    const diffHours = Math.floor(diffMs / 3600000);
    const diffDays = Math.floor(diffMs / 86400000);

    // Para fechas futuras
    if (diffMs < 0) {
      const absDiffMins = Math.abs(diffMins);
      const absDiffHours = Math.abs(diffHours);
      const absDiffDays = Math.abs(diffDays);
      if (absDiffDays > 0) return `En ${absDiffDays} día${absDiffDays > 1 ? 's' : ''}`;
      if (absDiffHours > 0) return `En ${absDiffHours} hora${absDiffHours > 1 ? 's' : ''}`;
      return `En ${absDiffMins} min`;
    }

    if (diffMins < 1) return 'Hace un momento';
    if (diffMins < 60) return `Hace ${diffMins} min`;
    if (diffHours < 24) return `Hace ${diffHours}h`;
    if (diffDays < 7) return `Hace ${diffDays} día${diffDays > 1 ? 's' : ''}`;

    return date.toLocaleDateString('es-CL', {
      day: '2-digit',
      month: '2-digit',
      year: 'numeric',
    });
  } catch (e) {
    return isoString;
  }
}
