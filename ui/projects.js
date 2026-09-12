'use strict';
const $ = id => document.getElementById(id);
let token = sessionStorage.getItem('manager-token') || '';
function consumeToken() {
  const fragment = new URLSearchParams(location.hash.slice(1));
  if (fragment.has('token')) {
    token = fragment.get('token');
    sessionStorage.setItem('manager-token', token);
    history.replaceState(null, '', '/');
  }
}
consumeToken();
let state = null, desktopProject = null, renderingKey = '', refreshBusy = false, quitting = false;
const accessChoices = new Map();
const empty = $('desktop').firstElementChild.cloneNode(true);
async function api(path, body) {
  if (quitting && path !== '/api/quit') throw new Error('The environment is shutting down.');
  const controller = new AbortController();
  const timer = path === '/api/state' ? setTimeout(() => controller.abort(), 12000) : null;
  let response, data;
  try {
    response = await fetch(path, {method: body === undefined ? 'GET' : 'POST',
      headers: {'Authorization': 'Bearer ' + token, ...(body === undefined ? {} : {'Content-Type': 'application/json'})},
      body: body === undefined ? undefined : JSON.stringify(body), signal: controller.signal});
    try { data = await response.json(); } catch (_) { data = {}; }
  } finally { if (timer !== null) clearTimeout(timer); }
  if (!response.ok) {
    const failure = new Error(data.error || 'Request failed.');
    failure.status = response.status;
    throw failure;
  }
  return data;
}
function error(message) { $('error').textContent = message; $('error').hidden = !message; }
function renderProjects() {
  const key = JSON.stringify([state.projects, state.active, state.busy, state.needs_reindex, state.target]);
  if (key === renderingKey) return;
  renderingKey = key;
  $('projects').replaceChildren();
  for (const project of state.projects) {
    const card = document.createElement('article');
    card.className = 'project' + (state.active === project.id ? ' active' : '');
    const title = document.createElement('h3'); title.textContent = project.name;
    const path = document.createElement('p'); path.className = 'path'; path.textContent = project.path;
    const select = document.createElement('select'); select.setAttribute('aria-label', project.name + ' access');
    for (const [value, text] of [['read-only', 'Read only'], ['read-write', 'Read & write · approve edits']]) {
      const option = document.createElement('option'); option.value = value; option.textContent = text; select.append(option);
    }
    select.value = accessChoices.get(project.id) || project.access;
    select.disabled = state.busy;
    select.onchange = () => accessChoices.set(project.id, select.value);
    const open = document.createElement('button');
    open.textContent = state.active === project.id ? 'Restart / apply access' : 'Open project';
    open.disabled = state.busy;
    open.onclick = async () => {
      await openProject(project.id, select.value, false);
    };
    card.append(title, path, select, open);
    if (state.needs_reindex && state.target === project.id) {
      const rebuild = document.createElement('button'); rebuild.className = 'quiet reindex';
      rebuild.textContent = 'Rebuild index & open'; rebuild.disabled = state.busy;
      rebuild.onclick = () => openProject(project.id, select.value, true); card.append(rebuild);
    }
    $('projects').append(card);
  }
}
// Expand inside the current app even if the host browser denies native fullscreen.
function leaveFullscreen() {
  const shell = $('desktop-shell');
  shell.classList.remove('expanded');
  $('fullscreen').setAttribute('aria-pressed', 'false');
  if (document.fullscreenElement && shell.contains(document.fullscreenElement)) {
    document.exitFullscreen().catch(() => {});
  }
}
$('fullscreen').onclick = async () => {
  const shell = $('desktop-shell');
  if (shell.classList.contains('expanded')) { leaveFullscreen(); return; }
  shell.classList.add('expanded');
  $('fullscreen').setAttribute('aria-pressed', 'true');
  if (shell.requestFullscreen && document.fullscreenEnabled) {
    try { await shell.requestFullscreen(); } catch (_) { /* Keep the in-app expansion. */ }
  }
};
$('fullscreen-exit').onclick = leaveFullscreen;
$('reconnect-desktop').onclick = () => {
  const frame = $('desktop').querySelector('iframe');
  if (frame) frame.replaceWith(frame.cloneNode(true));
};
document.addEventListener('fullscreenchange', () => {
  if (!document.fullscreenElement) leaveFullscreen();
});
document.addEventListener('keydown', event => {
  if (event.key === 'Escape') leaveFullscreen();
});
function clearDesktop() {
  leaveFullscreen();
  if (desktopProject !== null) {
    $('desktop').replaceChildren(empty.cloneNode(true)); desktopProject = null;
  }
  $('desktop-tools').hidden = true; $('password-value').textContent = '';
}
async function openProject(id, access, reindex) {
  try { await api('/api/open', {id, access, reindex}); clearDesktop(); await refresh(); }
  catch (e) { error(e.message); }
}
async function refresh() {
  if (refreshBusy || quitting) return;
  refreshBusy = true;
  try {
    const updated = await api('/api/state');
    if (quitting) return;
    state = updated; $('connection').hidden = true;
    $('status').textContent = state.busy ? state.phase : (state.error ? 'Needs attention' : state.active ? 'Ready' : 'Stopped');
    $('stop').disabled = !state.active && !state.busy;
    $('stop').textContent = state.busy ? 'Cancel & stop' : 'Stop environment';
    const active = state.projects.find(p => p.id === state.active);
    const pending = state.projects.find(p => p.id === state.target);
    $('workspace-title').textContent = (state.busy ? pending?.name : active?.name) || 'Choose a project';
    error(state.error); renderProjects();
    if (state.active && !state.busy && !state.error) {
      if (desktopProject !== state.active) {
        clearDesktop(); const frame = document.createElement('iframe');
        frame.title = 'Isolated desktop · ' + active.name;
        frame.src = `http://127.0.0.1:${state.ui_port}/vnc.html?resize=scale&autoconnect=1`;
        frame.allowFullscreen = true;
        frame.allow = 'fullscreen';
        frame.referrerPolicy = 'no-referrer'; $('desktop').replaceChildren(frame); desktopProject = state.active;
      }
      $('desktop-tools').hidden = false;
    } else clearDesktop();
  } catch (e) {
    if (!quitting) {
      const unauthorized = e.status === 401 || e.status === 403;
      $('status').textContent = unauthorized ? 'Authentication required' : 'Manager unavailable';
      $('connection').hidden = !unauthorized;
      error(unauthorized ? 'Open the authenticated manager link again.' : 'Cannot check workspace status. Retrying automatically; your desktop connection is kept.');
      // A failed health poll is not evidence that the desktop stopped.
      if (unauthorized) clearDesktop();
    }
  }
  finally { refreshBusy = false; }
}
$('add-toggle').onclick = () => { $('add-form').hidden = !$('add-form').hidden; if (!$('add-form').hidden) $('name').focus(); };
$('add-form').onsubmit = async event => {
  event.preventDefault();
  try { await api('/api/projects', {name: $('name').value, path: $('path').value, access: $('new-access').value});
    $('add-form').reset(); $('add-form').hidden = true; await refresh(); }
  catch (e) { error(e.message); }
};
$('browse').onclick = async () => {
  $('browse').disabled = true;
  try { const data = await api('/api/choose-folder', {}); if (data.path) { $('path').value = data.path; if (!$('name').value) $('name').value = data.path.replace(/\/$/, '').split('/').pop(); } }
  catch (e) { error(e.message); } finally { $('browse').disabled = false; }
};
$('stop').onclick = async () => { try { clearDesktop(); await api('/api/stop', {}); await refresh(); } catch (e) { error(e.message); } };
$('quit').onclick = async () => {
  if (quitting) return;
  quitting = true; clearDesktop(); error(''); $('connection').hidden = true;
  document.querySelectorAll('button, input, select').forEach(el => el.disabled = true);
  $('status').textContent = 'Shutting down…';
  try {
    const result = await api('/api/quit', {});
    if (result.stopped !== true) throw new Error('Shutdown was not confirmed.');
    clearInterval(refreshTimer); sessionStorage.removeItem('manager-token'); token = '';
    $('status').textContent = 'Shut down'; $('workspace-title').textContent = 'Everything is stopped';
    const message = document.createElement('div'); message.className = 'empty';
    const title = document.createElement('h3'); title.textContent = 'Sessions and indexes are saved';
    const detail = document.createElement('p'); detail.textContent = 'The workspace, search services and local manager have stopped. You can close this tab. To start again, run ./codeairlock projects.';
    message.append(title, detail); $('desktop').replaceChildren(message);
    $('search-requests').replaceChildren(); $('search-status').textContent = '';
  } catch (e) {
    quitting = false; renderingKey = '';
    document.querySelectorAll('button, input, select').forEach(el => el.disabled = false);
    await refresh(); error(e.message + ' Shutdown is not confirmed; check Docker before retrying.');
  }
};
$('password').onclick = async () => {
  const id = state.active;
  try { const result = await api('/api/password', {id});
    if (state.active === result.id && !state.busy) $('password-value').textContent = result.password;
  } catch (e) { error(e.message); }
};
window.addEventListener('hashchange', () => { consumeToken(); refresh(); });
async function refreshSearches() {
  $('search-refresh').disabled = true;
  try {
    const data = await api('/api/search');
    $('search-requests').replaceChildren();
    $('search-status').textContent = data.requests.length ? 'Review the exact query before approving.' : 'No pending searches.';
    for (const item of data.requests) {
      const card = document.createElement('article'); card.className = 'project';
      const query = document.createElement('pre'); query.className = 'search-query';
      // Show invisible and non-ASCII characters explicitly; never render HTML.
      query.textContent = JSON.stringify(item.query).replace(/[\u007f-\uffff]/g,
        c => '\\u' + c.charCodeAt(0).toString(16).padStart(4, '0'));
      card.append(query);
      for (const action of ['approve', 'deny']) {
        const button = document.createElement('button');
        button.textContent = action === 'approve' ? 'Approve this one query' : 'Deny';
        if (action === 'deny') button.className = 'quiet';
        button.onclick = async () => {
          card.querySelectorAll('button').forEach(b => b.disabled = true);
          $('search-status').textContent = action === 'approve' ? 'Sending the approved query…' : 'Denying…';
          try {
            const result = await api('/api/search', {action, id: item.id, query: item.query});
            card.remove(); $('search-status').textContent = 'Request: ' + result.status + '. Tell the agent to read its search result.';
          } catch (e) { $('search-status').textContent = e.message + ' Refresh to check the outcome.'; }
        };
        card.append(button);
      }
      $('search-requests').append(card);
    }
  } catch (e) { $('search-status').textContent = e.message; }
  finally { $('search-refresh').disabled = false; }
}
$('search-refresh').onclick = refreshSearches;
refresh(); const refreshTimer = setInterval(refresh, 2000);
