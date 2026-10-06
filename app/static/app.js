const $ = id => document.getElementById(id);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const SETTINGS = ['api_keys','models','default_model','strategy','request_timeout','max_failures','impersonate','user_agent',
  'proxy','upstream_base','reasoning_field','default_temperature','default_max_tokens','enable_logging','keepalive_minutes',
  'log_retention_days','plan_refresh_minutes'];
const fmt = n => n == null ? '—' : n === -1 ? '∞' : Number(n).toLocaleString();
function planCell(a) {
  if (a.plan_remaining == null) return '<span class="muted">not fetched</span>';
  const unl = a.plan_remaining === -1;
  const pct = !unl && a.plan_total ? Math.max(0, Math.min(100, Math.round(a.plan_remaining / a.plan_total * 100))) : 100;
  const color = a.exhausted ? '#ef4444' : pct < 15 ? '#f59e0b' : '#22c55e';
  return `<div style="min-width:150px"><b>${esc(a.plan_name || '')}</b> ${fmt(a.plan_remaining)}${unl ? '' : ' / ' + fmt(a.plan_total)}
    <div style="height:5px;background:#ffffff1a;border-radius:3px;margin:4px 0"><div style="height:100%;width:${pct}%;background:${color};border-radius:3px"></div></div>
    <span class="muted" style="font-size:11px">bal $${(a.balance ?? 0).toFixed(2)} · ${esc(a.plan_checked_at || '')}</span></div>`;
}
let accounts = [];

function toast(msg) { const t = $('toast'); t.textContent = msg; t.classList.add('show'); setTimeout(() => t.classList.remove('show'), 2800); }

async function api(path, method = 'GET', body) {
  const r = await fetch('/api' + path, { method, headers: { 'content-type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body) });
  if (r.status === 401) { location.href = '/login'; throw new Error('unauthorized'); }
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error?.message || data.detail || 'Request failed');
  return data;
}
const guard = fn => async (...a) => { try { await fn(...a); } catch (e) { if (e.message !== 'unauthorized') toast('⚠️ ' + e.message); } };

// Tabs
document.querySelectorAll('nav button[data-tab]').forEach(b => b.addEventListener('click', () => {
  document.querySelectorAll('nav button').forEach(x => x.classList.remove('active'));
  document.querySelectorAll('.tab').forEach(x => x.classList.remove('active'));
  b.classList.add('active'); $('tab-' + b.dataset.tab).classList.add('active');
  ({overview: loadOverview, accounts: loadAccounts, settings: loadSettings, logs: loadLogs})[b.dataset.tab]?.();
}));
$('logout-btn').onclick = async () => { await fetch('/logout', { method: 'POST' }); location.href = '/login'; };

// Overview
$('base-url').textContent = location.origin + '/v1';
$('copy-url').onclick = () => { navigator.clipboard.writeText($('base-url').textContent); toast('Copied'); };
const statusBadge = s => `<span class="badge ${s === 200 ? 'on' : 'off'}">${s}</span>`;
const loadOverview = guard(async () => {
  const s = await api('/stats');
  const rate = s.requests_total ? Math.round(s.requests_ok / s.requests_total * 100) + '%' : '—';
  const quota = s.plan_unlimited ? '∞' : `${fmt(s.plan_remaining)} / ${fmt(s.plan_total)}`;
  const cards = [['Active accounts', `${s.accounts_active} / ${s.accounts_total}`], ['Plan tokens left (pool)', quota], ['Requests', s.requests_total],
    ['Success rate', rate], ['Avg latency', s.avg_ms + ' ms'], ['Prompt tokens', s.prompt_tokens], ['Completion tokens', s.completion_tokens]];
  $('stats').innerHTML = cards.map(([l, v]) => `<div class="glass card stat"><div class="l">${l}</div><div class="v">${esc(v)}</div></div>`).join('');
  const logs = await api('/logs?limit=8');
  $('recent').innerHTML = logs.map(l => `<tr><td>${esc(l.created_at)}</td><td>${esc(l.account_name)}</td><td>${esc(l.model)}</td><td>${statusBadge(l.status_code)}</td><td>${l.duration_ms}</td></tr>`).join('')
    || '<tr><td colspan="5" class="muted">No requests yet</td></tr>';
});

// Accounts
const loadAccounts = guard(async () => {
  accounts = await api('/accounts');
  $('acc-body').innerHTML = accounts.map(a => `<tr>
    <td><b>${esc(a.name)}</b></td>
    <td><span class="badge ${a.is_active ? 'on' : 'off'}">${a.is_active ? 'active' : 'disabled'}</span></td>
    <td class="mono">${esc(a.cf_clearance)}</td><td>${planCell(a)}</td><td>${a.success_count}</td><td>${a.error_count}</td>
    <td class="err-cell" title="${esc(a.last_error)}">${esc(a.last_error)}</td>
    <td style="white-space:nowrap">
      <button class="btn sm" data-act="test" data-id="${a.id}">Test</button>
      <button class="btn sm" data-act="plan" data-id="${a.id}">Refresh plan</button>
      <button class="btn sm" data-act="toggle" data-id="${a.id}">${a.is_active ? 'Disable' : 'Enable'}</button>
      <button class="btn sm" data-act="edit" data-id="${a.id}">Edit</button>
      <button class="btn sm danger" data-act="del" data-id="${a.id}">Delete</button></td></tr>`).join('')
    || '<tr><td colspan="8" class="muted">No accounts yet — add one above</td></tr>';
});
$('acc-body').addEventListener('click', guard(async e => {
  const b = e.target.closest('button'); if (!b) return;
  const id = +b.dataset.id, a = accounts.find(x => x.id === id);
  if (b.dataset.act === 'test') { b.textContent = '…'; const r = await api(`/accounts/${id}/test`, 'POST'); toast((r.ok ? '✅ ' : '❌ ') + r.message); loadAccounts(); }
  if (b.dataset.act === 'plan') { b.textContent = '…'; const r = await api(`/accounts/${id}/plan`, 'POST'); toast(r.ok ? '✅ Plan refreshed' : '❌ ' + r.message); loadAccounts(); }
  if (b.dataset.act === 'toggle') { await api('/accounts/' + id, 'PATCH', { is_active: !a.is_active }); loadAccounts(); }
  if (b.dataset.act === 'del' && confirm(`Delete ${a.name}?`)) { await api('/accounts/' + id, 'DELETE'); loadAccounts(); }
  if (b.dataset.act === 'edit') {
    $('acc-id').value = id; $('acc-name').value = a.name; $('acc-rn').value = a.remember_name;
    $('acc-cf').value = $('acc-rv').value = $('acc-cookie').value = '';
    $('acc-cf').placeholder = $('acc-rv').placeholder = 'leave blank to keep current';
    $('acc-form-title').textContent = 'Edit ' + a.name; $('acc-cancel').hidden = false; window.scrollTo(0, 0);
  }
}));
function resetAccForm() {
  $('acc-form').reset(); $('acc-id').value = ''; $('acc-form-title').textContent = 'Add account'; $('acc-cancel').hidden = true;
  $('acc-cf').placeholder = 'CF_CLEARANCE'; $('acc-rv').placeholder = 'REMEMBER_WEB_VALUE';
}
$('acc-cancel').onclick = resetAccForm;
$('acc-form').addEventListener('submit', guard(async e => {
  e.preventDefault();
  const body = { name: $('acc-name').value, cf_clearance: $('acc-cf').value, remember_name: $('acc-rn').value,
    remember_value: $('acc-rv').value, cookie_string: $('acc-cookie').value };
  const id = $('acc-id').value;
  await api(id ? '/accounts/' + id : '/accounts', id ? 'PATCH' : 'POST', body);
  toast('Account saved'); resetAccForm(); loadAccounts();
}));
$('bulk-form').addEventListener('submit', guard(async e => {
  e.preventDefault();
  const r = await api('/accounts/bulk', 'POST', { text: $('bulk-text').value });
  toast(`Imported ${r.added}, skipped ${r.skipped}`); $('bulk-text').value = ''; loadAccounts();
}));

// Settings
const loadSettings = guard(async () => {
  const s = await api('/settings');
  SETTINGS.forEach(k => { $('s-' + k).value = k === 'models' || k === 'api_keys' ? s[k].split(/[,\n]/).map(x => x.trim()).filter(Boolean).join('\n') : s[k]; });
});
$('set-form').addEventListener('submit', guard(async e => {
  e.preventDefault();
  const body = {}; SETTINGS.forEach(k => body[k] = $('s-' + k).value);
  await api('/settings', 'PUT', body); toast('Settings saved');
}));
$('gen-key').onclick = () => {
  const k = 'sk-' + Array.from(crypto.getRandomValues(new Uint8Array(24)), b => b.toString(16).padStart(2, '0')).join('');
  $('s-api_keys').value = ($('s-api_keys').value.trim() + '\n' + k).trim();
};
$('pw-form').addEventListener('submit', guard(async e => {
  e.preventDefault(); await api('/password', 'PUT', { password: $('new-pw').value }); $('new-pw').value = ''; toast('Password changed');
}));

// Test
$('t-send').onclick = guard(async () => {
  const out = $('t-out'); out.hidden = false; out.textContent = 'Waiting…';
  const r = await api('/chat-test', 'POST', { model: $('t-model').value || undefined, prompt: $('t-prompt').value });
  out.textContent = r.ok ? (r.reasoning ? '[reasoning]\n' + r.reasoning + '\n\n' : '') + r.content + '\n\n' + JSON.stringify(r.usage) : '❌ ' + r.message;
});

// Logs
const loadLogs = guard(async () => {
  const logs = await api('/logs?limit=100');
  $('logs-body').innerHTML = logs.map(l => `<tr><td>${esc(l.created_at)}</td><td>${esc(l.account_name)}</td><td>${esc(l.model)}</td>
    <td>${l.stream ? 'yes' : 'no'}</td><td>${statusBadge(l.status_code)}</td><td>${l.duration_ms}</td>
    <td>${l.prompt_tokens}/${l.completion_tokens}</td><td class="err-cell" title="${esc(l.error)}">${esc(l.error)}</td></tr>`).join('')
    || '<tr><td colspan="8" class="muted">No logs</td></tr>';
});
$('logs-refresh').onclick = loadLogs;
$('logs-clear').onclick = guard(async () => {
  if (!confirm('Delete ALL logs?')) return;
  const r = await api('/logs', 'DELETE'); toast(`Deleted ${r.deleted} logs`); loadLogs();
});
$('logs-old').onclick = guard(async () => {
  const d = parseInt($('logs-days').value, 10) || 3;
  const r = await api('/logs?older_than_days=' + d, 'DELETE'); toast(`Deleted ${r.deleted} logs older than ${d}d`); loadLogs();
});
$('plans-refresh').onclick = guard(async () => { const r = await api('/plans/refresh', 'POST'); toast(`Plans refreshed: ${r.ok} ok, ${r.failed} failed`); loadAccounts(); });

loadOverview();
