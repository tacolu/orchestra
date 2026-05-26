/* ============================================================================
   Orchestra Console — app.js
   Vanilla JS SPA. No frameworks, no build step, no dependencies.
   ============================================================================ */

// ─── Config ────────────────────────────────────────────────────────────────
const API = (() => {
    const p = new URLSearchParams(location.search).get('api');
    if (p) return p;
    if (window.ORCHESTRA_API) return window.ORCHESTRA_API;
    return `${location.protocol}//${location.hostname}:8200/v1`;
})();

const POLL_INTERVAL_MS = 5000;

// ─── Global state ─────────────────────────────────────────────────────────
const state = {
    currentTab: 'plan',
    currentFrente: null,
    frentes: [],
    sesiones: [],
    tareas: [],
    objetivos: [],
    skills: [],
    biblioteca: [],
    bibCategorias: [],
    diario: [],
    stats: {},
    selectedSkill: null,
};

// ─── Utilities ────────────────────────────────────────────────────────────
const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => document.querySelectorAll(sel);

function el(tag, attrs = {}, ...children) {
    const e = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
        if (k === 'class') e.className = v;
        else if (k === 'html') e.innerHTML = v;
        else if (k.startsWith('on')) e.addEventListener(k.slice(2).toLowerCase(), v);
        else if (k === 'dataset') Object.assign(e.dataset, v);
        else e.setAttribute(k, v);
    }
    for (const c of children) {
        if (c == null) continue;
        e.append(c instanceof Node ? c : document.createTextNode(String(c)));
    }
    return e;
}

function esc(s) {
    if (s == null) return '';
    return String(s)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;').replace(/'/g, '&#039;');
}

function fmtDate(iso) {
    if (!iso) return '';
    return iso.replace('T', ' ').slice(0, 16);
}

function timeAgo(iso) {
    if (!iso) return '';
    const t = new Date(iso.replace(' ', 'T') + 'Z').getTime();
    const diff = (Date.now() - t) / 1000;
    if (diff < 60)    return `${Math.floor(diff)}s`;
    if (diff < 3600)  return `${Math.floor(diff / 60)}min`;
    if (diff < 86400) return `${Math.floor(diff / 3600)}h`;
    return `${Math.floor(diff / 86400)}d`;
}

function toast(msg, kind = '') {
    const t = $('#toast');
    t.textContent = msg;
    t.className = `toast ${kind}`;
    t.hidden = false;
    clearTimeout(toast._t);
    toast._t = setTimeout(() => { t.hidden = true; }, 3500);
}

// ─── HTTP ──────────────────────────────────────────────────────────────────
async function api(method, path, body = null) {
    const opts = { method, headers: {} };
    if (body) {
        opts.headers['Content-Type'] = 'application/json';
        opts.body = JSON.stringify(body);
    }
    let resp;
    try {
        resp = await fetch(`${API}${path}`, opts);
    } catch (e) {
        setApiStatus(false);
        throw new Error(`Network: ${e.message}`);
    }
    setApiStatus(true);
    if (resp.status === 204) return null;
    const ct = resp.headers.get('content-type') || '';
    const data = ct.includes('json') ? await resp.json() : await resp.text();
    if (!resp.ok) {
        const detail = (data && data.detail) || data;
        const err = typeof detail === 'object' ? `${detail.error}: ${detail.detail}` : String(detail);
        throw new Error(err);
    }
    return data;
}

function setApiStatus(ok) {
    const d = $('#api-status');
    d.classList.toggle('ok', ok);
    d.classList.toggle('fail', !ok);
}

// ─── Minimal Markdown renderer ─────────────────────────────────────────────
function renderMarkdown(md) {
    if (!md) return '';
    const lines = md.split('\n');
    let html = '';
    let inCode = false, codeBuffer = [], codeLang = '';
    let inList = false, listTag = '';

    function closeList() {
        if (inList) { html += `</${listTag}>`; inList = false; }
    }

    function inline(s) {
        s = esc(s);
        s = s.replace(/`([^`]+)`/g, '<code>$1</code>');
        s = s.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
        s = s.replace(/\*([^*]+)\*/g, '<em>$1</em>');
        s = s.replace(/__([^_]+)__/g, '<strong>$1</strong>');
        s = s.replace(/_([^_]+)_/g, '<em>$1</em>');
        s = s.replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" target="_blank">$1</a>');
        return s;
    }

    for (const raw of lines) {
        if (raw.startsWith('```')) {
            if (inCode) {
                html += `<pre><code class="lang-${codeLang}">${esc(codeBuffer.join('\n'))}</code></pre>`;
                codeBuffer = []; codeLang = ''; inCode = false;
            } else {
                closeList();
                inCode = true;
                codeLang = raw.slice(3).trim();
            }
            continue;
        }
        if (inCode) { codeBuffer.push(raw); continue; }

        const line = raw;
        const h = /^(#{1,6})\s+(.*)$/.exec(line);
        if (h) { closeList(); html += `<h${h[1].length}>${inline(h[2])}</h${h[1].length}>`; continue; }
        if (/^[-*_]{3,}\s*$/.test(line)) { closeList(); html += '<hr>'; continue; }
        if (/^>\s/.test(line)) { closeList(); html += `<blockquote>${inline(line.slice(2))}</blockquote>`; continue; }
        const ul = /^\s*[-*]\s+(.*)$/.exec(line);
        if (ul) {
            if (!inList || listTag !== 'ul') { closeList(); html += '<ul>'; inList = true; listTag = 'ul'; }
            html += `<li>${inline(ul[1])}</li>`;
            continue;
        }
        const ol = /^\s*\d+\.\s+(.*)$/.exec(line);
        if (ol) {
            if (!inList || listTag !== 'ol') { closeList(); html += '<ol>'; inList = true; listTag = 'ol'; }
            html += `<li>${inline(ol[1])}</li>`;
            continue;
        }
        if (!line.trim()) { closeList(); continue; }
        closeList();
        html += `<p>${inline(line)}</p>`;
    }
    closeList();
    if (inCode) html += `<pre><code>${esc(codeBuffer.join('\n'))}</code></pre>`;
    return html;
}

// ─── Modal ────────────────────────────────────────────────────────────────
function openModal(title, bodyNode, footerNodes = []) {
    $('#modal-title').textContent = title;
    const body = $('#modal-body');
    body.innerHTML = '';
    body.append(bodyNode);
    const footer = $('#modal-footer');
    footer.innerHTML = '';
    footer.append(...footerNodes);
    $('#modal').hidden = false;
}
function closeModal() { $('#modal').hidden = true; }
$$('#modal [data-close]').forEach(b => b.addEventListener('click', closeModal));
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeModal(); });

// ─── Tabs ──────────────────────────────────────────────────────────────────
$$('.tab').forEach(t => t.addEventListener('click', () => {
    if (!t.dataset.tab) return;
    switchTab(t.dataset.tab);
}));

function switchTab(name) {
    state.currentTab = name;
    $$('.tab').forEach(b => b.classList.toggle('active', b.dataset.tab === name));
    $$('.tab-content').forEach(c => c.classList.toggle('active', c.id === `tab-${name}`));
    renderCurrentTab();
}

// ─── Sidebar: front filter ────────────────────────────────────────────────
$('#btn-clear-filter').addEventListener('click', () => {
    state.currentFrente = null;
    $('#filter-info').textContent = 'All fronts';
    $$('.frente-item').forEach(f => f.classList.remove('selected'));
    renderCurrentTab();
});

$('#btn-refresh').addEventListener('click', () => loadAll());

// ─── Loaders ───────────────────────────────────────────────────────────────
async function loadFrentes() {
    state.frentes = await api('GET', '/frentes');
}
async function loadSesiones() {
    state.sesiones = await api('GET', '/sesiones?estado=active,idle');
}
async function loadTareas() {
    const params = new URLSearchParams();
    if (state.currentFrente) params.set('frente', state.currentFrente);
    params.set('limit', '500');
    state.tareas = await api('GET', `/tareas?${params.toString()}`);
}
async function loadObjetivos() {
    const q = state.currentFrente ? `?frente=${state.currentFrente}` : '';
    state.objetivos = await api('GET', `/objetivos${q}`);
}
async function loadSkills() {
    state.skills = await api('GET', '/skills');
}
async function loadBiblioteca() {
    const params = new URLSearchParams();
    const cat = $('#filter-categoria')?.value;
    if (cat) params.set('categoria', cat);
    const q = $('#bib-search')?.value;
    if (q) params.set('q', q);
    params.set('limit', '100');
    const data = await api('GET', `/biblioteca?${params}`);
    state.biblioteca = data.items;
}
async function loadBibCategorias() {
    state.bibCategorias = await api('GET', '/biblioteca/categorias');
}
async function loadDiario() {
    const params = new URLSearchParams();
    if (state.currentFrente) params.set('frente', state.currentFrente);
    const q = $('#diario-search')?.value;
    if (q) params.set('q', q);
    state.diario = await api('GET', `/diario?${params}`);
}
async function loadStats() {
    state.stats = await api('GET', '/stats');
}

async function loadAll() {
    try {
        await Promise.all([loadFrentes(), loadSesiones(), loadStats()]);
        renderSidebar();
        renderStats();
        await loadForTab();
    } catch (e) {
        toast(`Error: ${e.message}`, 'error');
    }
}

async function loadForTab() {
    switch (state.currentTab) {
        case 'plan':       await loadFrentes(); renderPlan(); break;
        case 'tareas':     await loadTareas(); renderTareas(); break;
        case 'objetivos':  await loadObjetivos(); renderObjetivos(); break;
        case 'skills':     await loadSkills(); renderSkills(); break;
        case 'biblioteca': await Promise.all([loadBiblioteca(), loadBibCategorias()]); renderBiblioteca(); break;
        case 'diario':     await loadDiario(); renderDiario(); break;
        case 'sesiones':   await loadTareas(); renderSesiones(); break;
        case 'architect':  initArchitect(); break;
    }
}

function renderCurrentTab() {
    loadForTab().catch(e => toast(`Error: ${e.message}`, 'error'));
}

// ─── Sidebar: render ──────────────────────────────────────────────────────
function renderSidebar() {
    const list = $('#frentes-list');
    list.innerHTML = '';
    const sesionesPorFrente = {};
    for (const s of state.sesiones) {
        if (!sesionesPorFrente[s.frente_slug]) sesionesPorFrente[s.frente_slug] = [];
        sesionesPorFrente[s.frente_slug].push(s);
    }
    for (const f of state.frentes) {
        const ses = sesionesPorFrente[f.slug] || [];
        const item = el('div', {
            class: `frente-item ${f.estado === 'paused' ? 'paused' : ''} ${state.currentFrente === f.slug ? 'selected' : ''}`,
            onClick: () => {
                state.currentFrente = f.slug;
                $('#filter-info').textContent = f.nombre;
                $$('.frente-item').forEach(x => x.classList.remove('selected'));
                item.classList.add('selected');
                renderCurrentTab();
            }
        },
            el('div', { class: 'frente-header' },
                el('span', { class: 'frente-color', style: `background:${f.color || '#666'}` }),
                el('span', { class: 'frente-name' }, f.nombre),
                el('span', { class: 'frente-count' }, ses.length ? `${ses.length}` : '')
            )
        );
        if (ses.length) {
            const sub = el('div', { class: 'sesiones-sub' });
            for (const s of ses) {
                sub.append(el('div', { class: 'sesion-item', title: `host: ${s.host}` },
                    el('span', { class: `salud-dot salud-${s.salud}` }),
                    el('span', { class: 'sesion-name' }, s.nombre_tmux),
                    el('span', { class: 'sesion-mins' }, `${s.minutos_desde_heartbeat}min`)
                ));
            }
            item.append(sub);
        }
        list.append(item);
    }
}

function renderStats() {
    $('#stat-frentes').textContent  = state.stats.frentes_activos ?? '.';
    $('#stat-sesiones').textContent = state.stats.sesiones_activas ?? '.';
    $('#stat-locks').textContent    = state.stats.locks_activos ?? '.';
    $('#stat-tareas').textContent   = state.stats.tareas_total ?? '.';
    $('#api-url').textContent = API.replace(/^https?:\/\//, '');
}

// ─── Tab: Plan ────────────────────────────────────────────────────────────
async function renderPlan() {
    const cont = $('#plan-content');
    cont.innerHTML = '<div class="placeholder">Loading plan...</div>';
    const plan = await api('GET', '/plan');
    cont.innerHTML = '';
    const frentes = state.currentFrente
        ? plan.frentes.filter(f => f.slug === state.currentFrente)
        : plan.frentes;
    if (!frentes.length) {
        cont.innerHTML = '<div class="placeholder">No active fronts</div>';
        return;
    }
    for (const f of frentes) {
        const block = el('div', { class: 'plan-frente' });
        block.append(el('div', { class: 'plan-frente-header' },
            el('span', { class: 'frente-color', style: `background:${f.color || '#666'}` }),
            el('h2', {}, f.nombre)
        ));
        if (!f.objetivos.length && !f.tareas_sin_objetivo.length) {
            block.append(el('div', { class: 'plan-objetivo' },
                el('div', { class: 'placeholder' }, 'No objectives or tasks')));
        }
        for (const o of f.objetivos) {
            block.append(renderObjetivoBlock(o));
        }
        if (f.tareas_sin_objetivo.length) {
            block.append(renderObjetivoBlock({
                titulo: 'Unassigned tasks',
                porcentaje_done: 0,
                total_tareas: f.tareas_sin_objetivo.length,
                tareas: f.tareas_sin_objetivo.map(t => ({
                    codigo: t.codigo, titulo: t.titulo, tarea_estado: t.estado,
                    prioridad: t.prioridad, lock_owner: t.lock_owner, blocker_codigo: null,
                }))
            }));
        }
        cont.append(block);
    }
}

function renderObjetivoBlock(o) {
    const block = el('div', { class: 'plan-objetivo' });
    block.append(el('div', { class: 'plan-objetivo-header' },
        el('span', { class: 'plan-objetivo-titulo' }, o.titulo),
        el('div', { class: 'progress-bar' },
            el('div', { class: 'progress-bar-fill', style: `width:${o.porcentaje_done}%` })),
        el('span', { class: 'plan-objetivo-pct' }, `${o.porcentaje_done}% | ${o.total_tareas}`)
    ));
    for (const t of o.tareas) {
        const isDone = t.tarea_estado === 'done';
        block.append(el('div', { class: `plan-tarea ${isDone ? 'done' : ''}` },
            el('div', { class: 'plan-tarea-check' }, isDone ? 'v' : ''),
            el('span', { class: 'plan-tarea-codigo' }, t.codigo),
            el('span', { class: 'plan-tarea-titulo' }, t.titulo),
            el('div', { class: 'plan-tarea-meta' },
                el('span', { class: `badge badge-${t.tarea_estado}` }, t.tarea_estado),
                t.lock_owner ? el('span', { class: 'badge badge-owner' }, `locked:${t.lock_owner}`) : null,
                t.blocker_codigo ? el('span', { class: 'badge badge-blocker' }, `blocked:${t.blocker_codigo}`) : null
            )
        ));
    }
    return block;
}

$('#btn-nuevo-frente').addEventListener('click', () => {
    const inSlug   = el('input',    { class: 'input', placeholder: 'slug-lowercase' });
    const inNombre = el('input',    { class: 'input', placeholder: 'Readable name' });
    const inDesc   = el('textarea', { class: 'textarea' });
    const inColor  = el('input',    { class: 'input', type: 'color', value: '#4A90D9' });
    const inRepo   = el('input',    { class: 'input', placeholder: '/path/to/repo or git URL' });
    const inBranch = el('input',    { class: 'input', placeholder: 'main', value: 'main' });

    const form = el('div', {},
        row('Slug *',       inSlug),
        row('Name *',       inNombre),
        row('Description',  inDesc),
        row('Color',        inColor),
        row('Repo path',    inRepo),
        row('Branch',       inBranch),
    );

    openModal('New front', form, [
        el('button', { class: 'btn', onClick: closeModal }, 'Cancel'),
        el('button', { class: 'btn primary', onClick: async () => {
            if (!inSlug.value.trim() || !inNombre.value.trim()) {
                toast('Slug and name are required', 'error');
                return;
            }
            try {
                await api('POST', '/frentes', {
                    slug:        inSlug.value.trim(),
                    nombre:      inNombre.value.trim(),
                    descripcion: inDesc.value || null,
                    color:       inColor.value || null,
                    repo_path:   inRepo.value.trim() || null,
                    branch:      inBranch.value.trim() || null,
                });
                toast('Front created', 'ok');
                closeModal();
                await loadAll();
            } catch (e) { toast(e.message, 'error'); }
        }}, 'Create'),
    ]);
});

$('#btn-snapshot-md').addEventListener('click', async () => {
    try {
        const md = await api('GET', '/plan/snapshot');
        const body = el('div', { class: 'md', html: renderMarkdown(md) });
        openModal('Master Plan snapshot', body, [
            el('button', { class: 'btn', onClick: () => navigator.clipboard.writeText(md).then(() => toast('Copied to clipboard', 'ok')) }, 'Copy markdown'),
            el('button', { class: 'btn primary', onClick: closeModal }, 'Close'),
        ]);
    } catch (e) { toast(e.message, 'error'); }
});

// ─── Tab: Tasks ──────────────────────────────────────────────────────────
function renderTareas() {
    const cont = $('#tareas-table');
    cont.innerHTML = '';
    const head = el('div', { class: 'tarea-row head' },
        el('div', {}, 'Code'),
        el('div', {}, 'Title'),
        el('div', {}, 'State'),
        el('div', {}, 'Prio'),
        el('div', {}, 'Owner / Blocker'),
        el('div', {}, 'Front')
    );
    cont.append(head);

    const estadoFiltro = $('#filter-estado').value;
    const tareas = state.tareas.filter(t => !estadoFiltro || t.estado === estadoFiltro);
    if (!tareas.length) {
        cont.append(el('div', { class: 'placeholder' }, 'No tasks to display'));
        return;
    }
    for (const t of tareas) {
        const owner = t.lock ? `locked:${t.lock.sesion_nombre}` : '';
        const blocker = t.blocker_tarea_codigo ? `blocked:${t.blocker_tarea_codigo}` : '';
        const modelo = t.modelo_efectivo || null;
        cont.append(el('div', {
            class: 'tarea-row',
            onClick: () => openTareaDetalle(t.codigo),
        },
            el('div', { class: 'tarea-codigo' }, t.codigo),
            el('div', { class: 'tarea-titulo' }, t.titulo),
            el('div', {},
                el('span', { class: `badge badge-${t.estado}` }, t.estado),
                modelo ? el('span', { class: 'badge badge-modelo' }, modelo) : null
            ),
            el('div', {},
                el('span', { class: 'tarea-prio' }, `P${t.prioridad}`)
            ),
            el('div', { class: 'tarea-meta' }, [owner, blocker].filter(Boolean).join(' | ')),
            el('div', { class: 'tarea-meta' }, t.frente_slug)
        ));
    }
}

$('#filter-estado').addEventListener('change', renderTareas);

async function openTareaDetalle(codigo) {
    try {
        const t = await api('GET', `/tareas/${codigo}`);
        const body = el('div');
        body.append(
            row('Title', t.titulo),
            row('Front', t.frente_slug),
            row('State', el('span', { class: `badge badge-${t.estado}` }, t.estado)),
            row('Priority', `P${t.prioridad}`),
            row('Objective', t.objetivo_titulo || '-'),
            row('Blocker', t.blocker_tarea_codigo || '-'),
            row('Glob', t.archivos_glob || '-'),
            row('Estimate', t.estimacion_min ? `${t.estimacion_min} min` : '-'),
            row('Model suggested', t.modelo_sugerido || '-'),
            row('Model effective', t.modelo_efectivo || '- (no estimate)'),
            row('Created', fmtDate(t.creado_ts)),
            t.lock ? row('Active lock', `${t.lock.sesion_nombre} (${t.lock.host}) - expires ${fmtDate(t.lock.expira_ts)}`) : null,
            t.descripcion ? row('Description', el('div', { class: 'md', html: renderMarkdown(t.descripcion) })) : null,
        );

        const transiciones = {
            'available':   ['in_progress', 'blocked', 'cancelled'],
            'locked':      ['in_progress', 'review', 'done', 'blocked', 'cancelled'],
            'in_progress': ['review', 'done', 'blocked'],
            'review':      ['in_progress', 'done', 'cancelled'],
            'blocked':     ['available', 'in_progress', 'cancelled'],
            'done':        [],
            'cancelled':   [],
        };
        const acciones = (transiciones[t.estado] || []).map(nuevo =>
            el('button', {
                class: 'btn',
                onClick: async () => {
                    if (!confirm(`Change ${t.codigo} from "${t.estado}" to "${nuevo}"?`)) return;
                    try {
                        await api('PATCH', `/tareas/${codigo}`, { estado: nuevo });
                        toast(`${t.codigo} -> ${nuevo}`, 'ok');
                        closeModal();
                        loadAll();
                    } catch (e) { toast(e.message, 'error'); }
                }
            }, `-> ${nuevo}`)
        );
        acciones.push(el('button', { class: 'btn primary', onClick: closeModal }, 'Close'));

        openModal(`Task ${t.codigo}`, body, acciones);
    } catch (e) { toast(e.message, 'error'); }
}

function row(label, value) {
    return el('div', { class: 'form-row' },
        el('label', {}, label),
        typeof value === 'string' || typeof value === 'number'
            ? el('div', {}, String(value))
            : value
    );
}

$('#btn-nueva-tarea').addEventListener('click', () => {
    const frenteSelect = el('select', { class: 'select' });
    for (const f of state.frentes) {
        frenteSelect.append(el('option', { value: f.slug }, f.nombre));
    }
    if (state.currentFrente) frenteSelect.value = state.currentFrente;

    const inCodigo = el('input', { class: 'input', placeholder: 'TASK-001' });
    const inTitulo = el('input', { class: 'input' });
    const inDesc = el('textarea', { class: 'textarea' });
    const inGlob = el('input', { class: 'input', placeholder: 'src/**/*.py' });
    const inPrio = el('input', { class: 'input', type: 'number', min: '0', max: '10', value: '5' });
    const inEst = el('input', { class: 'input', type: 'number', min: '0', placeholder: 'minutes' });
    const inModelo = el('input', { class: 'input', placeholder: 'e.g. small, medium, large' });
    const inBlocker = el('input', { class: 'input', placeholder: 'TASK-XXX (optional)' });

    const form = el('div', {},
        row('Front', frenteSelect),
        row('Code', inCodigo),
        row('Title', inTitulo),
        row('Description', inDesc),
        row('archivos_glob', inGlob),
        row('Priority (0-10)', inPrio),
        row('Estimate (min)', inEst),
        row('Model suggested', inModelo),
        row('Blocker (task code)', inBlocker),
    );

    openModal('New task', form, [
        el('button', { class: 'btn', onClick: closeModal }, 'Cancel'),
        el('button', { class: 'btn primary', onClick: async () => {
            if (!inCodigo.value.trim() || !inTitulo.value.trim()) {
                toast('Code and title are required', 'error');
                return;
            }
            try {
                await api('POST', '/tareas', {
                    frente: frenteSelect.value,
                    codigo: inCodigo.value.trim(),
                    titulo: inTitulo.value.trim(),
                    descripcion: inDesc.value || null,
                    archivos_glob: inGlob.value || null,
                    prioridad: parseInt(inPrio.value) || 0,
                    estimacion_min: inEst.value ? parseInt(inEst.value) : null,
                    modelo_sugerido: inModelo.value.trim() || null,
                    blocker_tarea_codigo: inBlocker.value.trim() || null,
                });
                toast('Task created', 'ok');
                closeModal();
                loadAll();
            } catch (e) { toast(e.message, 'error'); }
        }}, 'Create'),
    ]);
});

// ─── Tab: Objectives ──────────────────────────────────────────────────────
function renderObjetivos() {
    const cont = $('#objetivos-grid');
    cont.innerHTML = '';
    if (!state.objetivos.length) {
        cont.innerHTML = '<div class="placeholder">No objectives</div>';
        return;
    }
    for (const o of state.objetivos) {
        const card = el('div', { class: 'objetivo-card' });
        card.append(
            el('div', { class: 'objetivo-header' },
                el('h3', { class: 'objetivo-titulo' }, o.titulo),
                el('span', { class: `badge badge-${o.estado}` }, o.estado)
            ),
            el('div', { class: 'objetivo-frente' }, o.frente_slug),
            el('div', { class: 'objetivo-progreso' },
                el('div', { class: 'progress-bar' },
                    el('div', { class: 'progress-bar-fill', style: `width:${o.progreso.porcentaje}%` })),
                el('span', { class: 'plan-objetivo-pct' }, `${o.progreso.porcentaje}%`)
            ),
            el('div', { class: 'objetivo-numeros' },
                el('span', {}, 'total: ', el('strong', {}, o.progreso.total)),
                el('span', {}, 'done: ', el('strong', {}, o.progreso.done)),
                el('span', {}, 'wip: ', el('strong', {}, o.progreso.in_progress)),
                el('span', {}, 'blocked: ', el('strong', {}, o.progreso.blocked))
            ),
            o.deadline ? el('div', { class: 'objetivo-deadline' }, `Deadline: ${o.deadline}`) : null
        );
        cont.append(card);
    }
}

$('#btn-nuevo-objetivo').addEventListener('click', () => {
    const frenteSelect = el('select', { class: 'select' });
    for (const f of state.frentes) {
        frenteSelect.append(el('option', { value: f.slug }, f.nombre));
    }
    if (state.currentFrente) frenteSelect.value = state.currentFrente;

    const inTitulo = el('input', { class: 'input' });
    const inDesc = el('textarea', { class: 'textarea' });
    const inPrio = el('input', { class: 'input', type: 'number', min: '0', max: '10', value: '5' });
    const inDead = el('input', { class: 'input', type: 'date' });

    const form = el('div', {},
        row('Front', frenteSelect),
        row('Title', inTitulo),
        row('Description', inDesc),
        row('Priority', inPrio),
        row('Deadline', inDead),
    );

    openModal('New objective', form, [
        el('button', { class: 'btn', onClick: closeModal }, 'Cancel'),
        el('button', { class: 'btn primary', onClick: async () => {
            if (!inTitulo.value.trim()) { toast('Title is required', 'error'); return; }
            try {
                await api('POST', '/objetivos', {
                    frente: frenteSelect.value,
                    titulo: inTitulo.value.trim(),
                    descripcion: inDesc.value || null,
                    prioridad: parseInt(inPrio.value) || 0,
                    deadline: inDead.value || null,
                });
                toast('Objective created', 'ok');
                closeModal();
                loadAll();
            } catch (e) { toast(e.message, 'error'); }
        }}, 'Create'),
    ]);
});

// ─── Tab: Skills ──────────────────────────────────────────────────────────
function renderSkills() {
    const list = $('#skills-list');
    list.innerHTML = '';
    if (!state.skills.length) {
        list.innerHTML = '<div class="placeholder">No skills</div>';
        return;
    }
    for (const s of state.skills) {
        list.append(el('div', {
            class: `skill-item ${state.selectedSkill === s.slug ? 'selected' : ''}`,
            onClick: () => openSkill(s.slug),
        },
            el('div', { class: 'skill-name' }, s.nombre),
            el('div', { class: 'skill-meta' },
                `${s.categoria || '-'} | ${s.tags || ''}`,
                s.modelo_sugerido ? el('span', {
                    class: 'badge badge-modelo',
                    style: 'margin-left: 6px;'
                }, s.modelo_sugerido) : null
            )
        ));
    }
}

async function openSkill(slug) {
    state.selectedSkill = slug;
    $$('.skill-item').forEach(i => i.classList.remove('selected'));
    const content = $('#skill-content');
    content.innerHTML = '<div class="placeholder">Loading...</div>';
    try {
        const md = await api('GET', `/skills/${slug}/contenido`);
        content.innerHTML = `<div class="md">${renderMarkdown(md)}</div>`;
        renderSkills();
    } catch (e) {
        content.innerHTML = `<div class="placeholder">Error: ${esc(e.message)}</div>`;
    }
}

$('#btn-reindex').addEventListener('click', async () => {
    try {
        const r = await api('POST', '/skills/reindex');
        toast(`Reindex: ${r.descubiertas} new, ${r.actualizadas} updated, ${r.total_activas} active`, 'ok');
        await loadSkills();
        renderSkills();
    } catch (e) { toast(e.message, 'error'); }
});

// ─── Tab: Library ──────────────────────────────────────────────────────────
function renderBiblioteca() {
    const sel = $('#filter-categoria');
    const curVal = sel.value;
    sel.innerHTML = '<option value="">All categories</option>';
    for (const c of state.bibCategorias) {
        sel.append(el('option', { value: c.categoria }, `${c.categoria} (${c.count})`));
    }
    sel.value = curVal;

    const grid = $('#bib-grid');
    grid.innerHTML = '';
    if (!state.biblioteca.length) {
        grid.innerHTML = '<div class="placeholder">No repos</div>';
        return;
    }
    for (const r of state.biblioteca) {
        const frente = r.frente_slug
            ? el('span', { class: 'badge badge-frente' }, r.frente_slug)
            : null;
        const card = el('div', { class: 'bib-card' },
            el('div', { class: 'bib-card-head' },
                el('span', { class: 'bib-slug' }, r.slug),
                el('span', { class: 'badge' }, r.estado || 'active'),
                frente
            ),
            el('div', { class: 'bib-nombre' }, r.nombre),
            r.descripcion ? el('div', { class: 'bib-desc' }, r.descripcion) : null,
            el('div', { class: 'bib-meta' },
                el('span', { class: 'bib-cat' }, r.categoria || ''),
                r.lenguaje ? el('span', { class: 'bib-lang' }, r.lenguaje) : null,
                r.github ? el('a', {
                    class: 'bib-gh',
                    href: `https://github.com/${r.github}`,
                    target: '_blank',
                    rel: 'noopener',
                }, 'GitHub') : null
            )
        );
        grid.append(card);
    }
}

$('#filter-categoria').addEventListener('change', () => {
    loadBiblioteca().then(renderBiblioteca).catch(e => toast(e.message, 'error'));
});

let bibSearchTimer = null;
$('#bib-search').addEventListener('input', () => {
    clearTimeout(bibSearchTimer);
    bibSearchTimer = setTimeout(() => loadBiblioteca().then(renderBiblioteca), 300);
});

// ─── Tab: Journal ──────────────────────────────────────────────────────────
function renderDiario() {
    const cont = $('#diario-list');
    cont.innerHTML = '';
    if (!state.diario.length) {
        cont.innerHTML = '<div class="placeholder">No journal entries</div>';
        return;
    }
    for (const d of state.diario) {
        cont.append(el('div', { class: 'diario-entry' },
            el('div', { class: 'diario-entry-header' },
                el('span', { class: 'diario-titulo' }, d.titulo || '(untitled)'),
                el('span', { class: 'diario-fecha' }, fmtDate(d.ts)),
                el('span', { class: 'diario-autor' }, d.autor),
                d.frente_slug ? el('span', { class: 'diario-frente' }, d.frente_slug) : null
            ),
            el('div', { class: 'diario-contenido md', html: renderMarkdown(d.contenido) }),
            d.tags ? el('div', { class: 'diario-tags' },
                ...d.tags.split(',').map(t => el('span', { class: 'diario-tag' }, t.trim()))
            ) : null
        ));
    }
}

let diarioSearchTimer = null;
$('#diario-search').addEventListener('input', () => {
    clearTimeout(diarioSearchTimer);
    diarioSearchTimer = setTimeout(() => loadDiario().then(renderDiario), 300);
});

$('#btn-nueva-entrada').addEventListener('click', () => {
    const frenteSelect = el('select', { class: 'select' });
    frenteSelect.append(el('option', { value: '' }, '- no front -'));
    for (const f of state.frentes) {
        frenteSelect.append(el('option', { value: f.slug }, f.nombre));
    }
    if (state.currentFrente) frenteSelect.value = state.currentFrente;

    const inAutor = el('input', { class: 'input', value: 'user' });
    const inTitulo = el('input', { class: 'input' });
    const inCont = el('textarea', { class: 'textarea', style: 'min-height: 200px;' });
    const inTags = el('input', { class: 'input', placeholder: 'decision,architecture' });

    const form = el('div', {},
        row('Front', frenteSelect),
        row('Author', inAutor),
        row('Title', inTitulo),
        row('Content (markdown)', inCont),
        row('Tags (CSV)', inTags),
    );

    openModal('New journal entry', form, [
        el('button', { class: 'btn', onClick: closeModal }, 'Cancel'),
        el('button', { class: 'btn primary', onClick: async () => {
            if (!inCont.value.trim()) { toast('Content is required', 'error'); return; }
            try {
                await api('POST', '/diario', {
                    frente: frenteSelect.value || null,
                    autor: inAutor.value.trim() || 'user',
                    titulo: inTitulo.value.trim() || null,
                    contenido: inCont.value,
                    tags: inTags.value.trim() || null,
                });
                toast('Entry created', 'ok');
                closeModal();
                loadAll();
            } catch (e) { toast(e.message, 'error'); }
        }}, 'Create'),
    ]);
});

// ─── Tab: Sessions (SSE events per task, no terminal) ─────────────────────
let _activeSessionCodigo = null;
let _activeEventSource = null;

function _appendEventRow(list, ev) {
    if (!list) return;
    const tsHHMMSS = (ev.ts || '').slice(11, 19);
    const row = el('div', {
        class: 'ev-row',
        title: `${ev.ts}  ${ev.tipo}: ${ev.mensaje || ''}`,
    },
        el('span', { class: 'ev-ts' }, tsHHMMSS),
        el('span', { class: `ev-tipo ${ev.tipo}` }, ev.tipo),
        el('span', { class: 'ev-msg' }, ev.mensaje || ''),
    );
    list.append(row);
    list.scrollTop = list.scrollHeight;
}

function openEventsForTask(codigo) {
    if (_activeSessionCodigo === codigo) return;

    // Close previous
    if (_activeEventSource) {
        _activeEventSource.close();
        _activeEventSource = null;
    }

    _activeSessionCodigo = codigo;
    const list = $('#sesiones-eventos-list');
    const span = $('#sesiones-eventos-codigo');
    if (span) span.textContent = codigo;
    if (list) list.innerHTML = '<div class="placeholder">Connecting...</div>';

    // Highlight selected card
    $$('.sesion-card').forEach(c => c.classList.toggle('selected', c.dataset.codigo === codigo));

    const es = new EventSource(`${API}/eventos/stream?tarea_codigo=${encodeURIComponent(codigo)}`);
    _activeEventSource = es;
    let first = true;
    es.onmessage = (e) => {
        let ev;
        try { ev = JSON.parse(e.data); } catch (_) { return; }
        if (first) { list.innerHTML = ''; first = false; }
        _appendEventRow(list, ev);
    };
    es.onerror = () => {
        if (first) { list.innerHTML = '<div class="placeholder">Connection error</div>'; }
    };
}

function renderSesiones() {
    const lista = $('#sesiones-lista');
    lista.innerHTML = '';
    const candidatas = state.tareas.filter(
        t => t.estado === 'locked' || t.estado === 'in_progress'
    );
    if (!candidatas.length) {
        lista.innerHTML = '<div class="placeholder">No tasks with active locks right now.</div>';
        return;
    }
    for (const t of candidatas) {
        const owner = t.lock ? t.lock.sesion_nombre : '(no lock visible)';
        const item = el('div', {
            class: `sesion-card ${_activeSessionCodigo === t.codigo ? 'selected' : ''}`,
            dataset: { codigo: t.codigo },
            onClick: () => openEventsForTask(t.codigo),
        },
            el('div', { class: 'sesion-card-head' },
                el('span', { class: 'sesion-card-codigo' }, t.codigo),
                el('span', { class: `badge badge-${t.estado}` }, t.estado),
            ),
            el('div', { class: 'sesion-card-titulo' }, t.titulo),
            el('div', { class: 'sesion-card-meta' },
                el('span', {}, `locked:${owner}`),
                t.frente_slug ? el('span', { class: 'dim' }, ` | ${t.frente_slug}`) : null,
            ),
        );
        lista.append(item);
    }
}

// ─── Polling ──────────────────────────────────────────────────────────────
function startPolling() {
    setInterval(async () => {
        try {
            await Promise.all([loadFrentes(), loadSesiones(), loadStats()]);
            renderSidebar();
            renderStats();
            if (state.currentTab === 'plan') renderPlan();
            if (state.currentTab === 'sesiones') { loadTareas().then(renderSesiones).catch(() => {}); }
        } catch (e) {
            console.warn('polling error:', e.message);
        }
    }, POLL_INTERVAL_MS);
}

// ─── Architect Chat ──────────────────────────────────────────────────────

let _archSSE = null;
let _archInitialized = false;

function initArchitect() {
    if (_archInitialized) return;
    _archInitialized = true;

    // Load config
    api('GET', '/architect/config').then(cfg => {
        const info = $('#arch-config-info');
        if (info) {
            info.textContent = `model: ${cfg.architect_model} · auto-approve: ${cfg.auto_approve ? 'on' : 'off'}`;
        }
    }).catch(() => {});

    // Load assignments
    loadArchAssignments();

    // Start SSE
    startArchSSE();

    // Send button
    $('#btn-arch-send').addEventListener('click', sendArchMessage);
    $('#arch-input').addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            sendArchMessage();
        }
    });
    $('#btn-arch-invoke').addEventListener('click', async () => {
        try {
            await api('POST', '/architect/invoke');
            toast('Architect invocation queued');
        } catch (e) {
            toast(`Error: ${e.message}`, 'error');
        }
    });
}

function startArchSSE() {
    if (_archSSE) _archSSE.close();
    const container = $('#arch-messages');
    container.innerHTML = '';
    _archSSE = new EventSource(`${API}/architect/messages/stream?hydrate=50`);
    _archSSE.onmessage = (evt) => {
        try {
            const msg = JSON.parse(evt.data);
            _appendArchMessage(container, msg);
        } catch (e) { /* ignore keepalive / parse errors */ }
    };
    _archSSE.onerror = () => {
        // SSE will auto-reconnect
    };
}

function _appendArchMessage(container, msg) {
    const bubble = el('div', { class: `arch-msg arch-msg-${msg.role}` },
        el('div', { class: 'arch-msg-head' },
            el('span', { class: 'arch-msg-role' }, msg.role),
            el('span', { class: 'arch-msg-time dim' }, fmtDate(msg.created_at)),
        ),
        el('div', { class: 'arch-msg-body' }, msg.content),
    );
    container.append(bubble);
    container.scrollTop = container.scrollHeight;
}

async function sendArchMessage() {
    const input = $('#arch-input');
    const text = input.value.trim();
    if (!text) return;
    input.value = '';
    input.disabled = true;
    try {
        await api('POST', '/architect/messages', { role: 'user', content: text });
    } catch (e) {
        toast(`Error: ${e.message}`, 'error');
        input.value = text;
    }
    input.disabled = false;
    input.focus();
}

async function loadArchAssignments() {
    const container = $('#arch-assignments');
    try {
        const assignments = await api('GET', '/architect/assignments?status=pending');
        if (!assignments.length) {
            container.innerHTML = '<div class="placeholder">No pending assignments</div>';
            return;
        }
        container.innerHTML = '';
        for (const a of assignments) {
            container.append(_renderAssignment(a));
        }
    } catch (e) {
        container.innerHTML = `<div class="placeholder">Error: ${esc(e.message)}</div>`;
    }
}

function _renderAssignment(a) {
    const card = el('div', { class: `arch-assignment arch-assignment-${a.status}` },
        el('div', { class: 'arch-assignment-head' },
            el('span', { class: 'arch-assignment-task' }, a.task_codigo),
            el('span', { class: `badge badge-${a.status}` }, a.status),
        ),
        a.hint ? el('div', { class: 'arch-assignment-hint dim' }, a.hint) : null,
        a.model ? el('div', { class: 'dim' }, `model: ${a.model}`) : null,
    );
    if (a.status === 'pending') {
        const actions = el('div', { class: 'arch-assignment-actions' },
            el('button', {
                class: 'btn btn-sm primary',
                onClick: () => updateAssignment(a.id, 'approved'),
            }, 'Approve'),
            el('button', {
                class: 'btn btn-sm',
                onClick: () => updateAssignment(a.id, 'rejected'),
            }, 'Reject'),
        );
        card.append(actions);
    }
    return card;
}

async function updateAssignment(id, status) {
    try {
        await api('PUT', `/architect/assignments/${id}`, { status });
        toast(`Assignment ${status}`);
        loadArchAssignments();
    } catch (e) {
        toast(`Error: ${e.message}`, 'error');
    }
}

// ─── Polling (architect) ────────────────────────────────────────────────
// Refresh assignments periodically when on architect tab
setInterval(() => {
    if (state.currentTab === 'architect' && _archInitialized) {
        loadArchAssignments();
    }
}, POLL_INTERVAL_MS);

// ─── Init ─────────────────────────────────────────────────────────────────
loadAll().then(startPolling);
