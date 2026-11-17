const $ = id => document.getElementById(id);
let csrf = '', data = null;

function notify(text, bad = false) { const node = $('message'); node.textContent = text; node.className = 'notice ' + (bad ? 'error' : 'ok'); }
function micro(value) { const text = String(value).padStart(7, '0'); return '$' + text.slice(0, -6) + '.' + text.slice(-6); }
function cell(text, kind) { const span = document.createElement('span'); span.textContent = text; if (kind) span.className = kind; return span; }
function table(id, headings, rows) {
  const host = $(id); host.replaceChildren();
  if (!rows.length) { const empty = document.createElement('div'); empty.className = 'empty'; empty.textContent = 'Nothing yet.'; host.append(empty); return; }
  const grid = document.createElement('table'), head = document.createElement('thead'), line = document.createElement('tr');
  headings.forEach(text => { const th = document.createElement('th'); th.textContent = text; line.append(th); }); head.append(line); grid.append(head);
  const body = document.createElement('tbody');
  rows.forEach(values => { const tr = document.createElement('tr'); values.forEach(value => { const td = document.createElement('td'); td.append(value instanceof Node ? value : document.createTextNode(String(value))); tr.append(td); }); body.append(tr); });
  grid.append(body); host.append(grid);
}

async function api(path, method = 'GET', body) {
  const headers = {'X-CSRF-Token': csrf};
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  const response = await fetch(path, {method, headers, body: body === undefined ? undefined : JSON.stringify(body), cache: 'no-store', credentials: 'same-origin'});
  const result = await response.json().catch(() => ({}));
  if (!response.ok) {
    if (response.status === 401 && path !== '/portal/login') showSignedOut();
    throw new Error((result.error?.message || 'Request failed') + ' [' + (result.error?.code || response.status) + ']');
  }
  return result;
}

function showTab(name) { document.querySelectorAll('.tab').forEach(node => node.hidden = node.id !== 'tab-' + name); document.querySelectorAll('[data-tab]').forEach(node => node.classList.toggle('active', node.dataset.tab === name)); }
document.querySelectorAll('[data-tab]').forEach(node => node.onclick = () => { if (csrf) showTab(node.dataset.tab); });

function showSignedOut() {
  csrf = ''; data = null;
  $('signed-out').hidden = false; $('signed-in').hidden = true; $('logout').classList.add('hidden');
  $('status').textContent = 'Signed out'; $('status').className = 'pill';
  $('title').textContent = 'Connect your app to AI with Tollgate'; $('subtitle').textContent = 'Sign in or create an account for your business.';
  $('side-business').textContent = 'Not signed in';
}

function render() {
  const balance = data.balance;
  $('m-available').textContent = '$' + balance.available_usd; $('m-reserved').textContent = '$' + balance.reserved_usd; $('m-spent').textContent = '$' + balance.spent_usd;
  const own = data.provider_keys.length > 0;
  $('m-payer').textContent = own ? 'You' : (data.provider_key_policy === 'customer_only' ? 'Not set' : 'Operator');
  $('m-payer-note').textContent = own ? 'Your own provider key is used' : 'Add your provider key to use your own account';
  $('policy').textContent = data.provider_key_policy === 'customer_only' ? 'Required: requests are refused until you add your own key.' : 'Recommended: without your own key, the operator\'s key is used.';
  const providerSelect = $('provider-name'), providerChosen = providerSelect.value, providers = [...new Set(data.models.map(model => model.owned_by))];
  providerSelect.replaceChildren(...providers.map(value => { const option = document.createElement('option'); option.value = value; option.textContent = value; return option; }));
  if (providers.includes(providerChosen)) providerSelect.value = providerChosen;
  const select = $('key-models'), chosen = [...select.selectedOptions].map(option => option.value);
  select.replaceChildren(...data.models.map(model => { const option = document.createElement('option'); option.value = model.id; option.textContent = model.id; option.selected = chosen.includes(model.id); return option; }));
  table('key-table', ['Label', 'Prefix', 'Models', 'Status', 'Action'], data.keys.map(row => {
    const button = document.createElement('button'); button.textContent = 'Revoke'; button.className = 'danger'; button.disabled = !row.active;
    button.onclick = async () => { if (!confirm('Revoke this key? Apps using it stop working.')) return; try { await api('/portal/keys/' + encodeURIComponent(row.id), 'DELETE'); notify('Key revoked.'); await load(); } catch (error) { notify(error.message, true); } };
    const status = !row.active ? 'revoked' : row.expires_at && row.expires_at < Date.now() / 1000 ? 'expired' : 'active';
    return [row.label, cell(row.prefix + '…', 'mono'), row.allowed_models.join(', '), status, button];
  }));
  table('provider-table', ['Provider', 'Key', 'Saved', 'Action'], data.provider_keys.map(row => {
    const button = document.createElement('button'); button.textContent = 'Remove'; button.className = 'danger';
    button.onclick = async () => { if (!confirm('Remove your ' + row.provider + ' key?')) return; try { await api('/portal/provider-keys/' + row.provider, 'DELETE'); notify('Provider key removed.'); await load(); } catch (error) { notify(error.message, true); } };
    return [row.provider, cell('…' + row.hint, 'mono'), new Date(row.created_at * 1000).toLocaleString(), button];
  }));
  table('user-table', ['End user', 'Settled requests', 'Tokens in / out', 'Cost'], data.end_users.map(row => [cell(row.end_user, 'mono'), row.requests, row.input_tokens + ' / ' + row.output_tokens, micro(row.cost_micro)]));
  table('request-table', ['Request', 'End user', 'Model / feature', 'Paid by', 'State', 'Cost', 'Tokens in / out'], data.requests.map(row => [cell(row.id, 'mono'), row.end_user === null ? '—' : cell(row.end_user, 'mono'), row.model + ' / ' + row.feature, row.key_source === 'customer' ? 'Your key' : row.key_source === 'deployment' ? 'Operator key' : '—', row.state, row.cost_usd === null ? 'Pending' : '$' + row.cost_usd, row.input_tokens === null ? '—' : row.input_tokens + ' / ' + row.output_tokens]));
  const model = data.models[0]?.id || 'MODEL_ID';
  $('base-url').textContent = data.base_url;
  $('example').textContent = 'curl ' + data.base_url + '/chat/completions \\\n  -H "Authorization: Bearer tg_YOUR_KEY" \\\n  -H "Content-Type: application/json" \\\n  -H "Idempotency-Key: <new UUID per message>" \\\n  -H "X-Tollgate-End-User: <your user id>" \\\n  -d \'{"model":"' + model + '","messages":[{"role":"user","content":"Hello"}],"max_completion_tokens":256}\'';
}

async function load() { data = await api('/portal/workspace'); render(); }

async function enter() {
  const session = await api('/portal/session');
  csrf = session.csrf_token;
  $('signed-out').hidden = true; $('signed-in').hidden = false; $('logout').classList.remove('hidden');
  $('status').textContent = 'Signed in'; $('status').className = 'pill good';
  $('title').textContent = session.business; $('subtitle').textContent = session.email; $('side-business').textContent = session.business;
  showTab('home');
  await load();
}

function bind(id, action) {
  $(id).onsubmit = async event => {
    event.preventDefault();
    const button = $(id).querySelector('button'); button.disabled = true;
    try { await action(); } catch (error) { notify(error.message, true); } finally { button.disabled = false; }
  };
}

bind('login-form', async () => {
  const result = await api('/portal/login', 'POST', {email: $('login-email').value, password: $('login-password').value});
  csrf = result.csrf_token; $('login-password').value = '';
  await enter(); notify('Signed in.');
});
bind('signup-form', async () => {
  const result = await api('/portal/signup', 'POST', {business: $('signup-business').value, email: $('signup-email').value, password: $('signup-password').value});
  $('signup-password').value = '';
  notify(result.state === 'pending' ? 'Account created. The operator must approve it before you can sign in.' : 'Account created. You can sign in now.');
});
bind('key-form', async () => {
  const result = await api('/portal/keys', 'POST', {label: $('key-label').value, allowed_models: [...$('key-models').selectedOptions].map(option => option.value), expires_in_days: $('key-expiry').value ? Number($('key-expiry').value) : null});
  $('new-key').value = result.api_key; $('new-key-panel').classList.remove('hidden');
  notify('API key created. Copy it now; it cannot be shown again.'); await load();
});
bind('provider-form', async () => {
  const saved = await api('/portal/provider-keys/' + $('provider-name').value, 'PUT', {api_key: $('provider-key').value.trim()});
  $('provider-key').value = '';
  notify('Saved your ' + saved.provider + ' key ending ' + saved.hint + '. Your requests now bill your own provider account.'); await load();
});
$('copy-key').onclick = async () => { try { await navigator.clipboard.writeText($('new-key').value); notify('Key copied.'); } catch { notify('Select and copy the key manually.', true); } };
$('logout').onclick = async () => { try { await api('/portal/logout', 'POST'); } catch { } showSignedOut(); notify('Signed out.'); };
setInterval(() => { if (csrf && !document.hidden) load().catch(() => {}); }, 10000);
(async () => { try { await enter(); } catch { showSignedOut(); $('message').className = 'notice hidden'; } })();
