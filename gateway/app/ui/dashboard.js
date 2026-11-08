
// Keep secrets in memory and render all returned strings as text, never HTML.
const $ = id => document.getElementById(id);
let csrfToken = '', snapshot = null, refreshing = false, lastRequest = null, lastCustomerKey = '';
function notify(text, bad = false) { const element = $('message'); element.textContent = text; element.className = 'notice ' + (bad ? 'error' : 'ok'); }
function money(value) { return '$' + value; }
function usdToMicro(value) { const [whole, fraction = ''] = String(value).split('.'); return BigInt(whole) * 1000000n + BigInt(fraction.padEnd(6, '0')); }
function formatMicro(value) { return '$' + (value / 1000000n) + '.' + (value % 1000000n).toString().padStart(6, '0'); }
function cell(text, className = '') { const node = document.createElement('span'); node.textContent = text; node.className = className; return node; }
function badge(state) { return cell(state.replaceAll('_', ' '), 'pill ' + (['settled', 'active'].includes(state) ? 'good' : ['unresolved', 'revoked'].includes(state) ? 'bad' : 'warn')); }
function table(target, headings, rows) {
  // Create DOM cells safely, including names and feature tags supplied by users.
  const host = $(target); host.replaceChildren();
  if (!rows.length) { host.append(cell('No records yet.', 'empty')); return; }
  const table = document.createElement('table'), head = document.createElement('thead'), header = document.createElement('tr');
  headings.forEach(text => { const th = document.createElement('th'); th.textContent = text; header.append(th); }); head.append(header); table.append(head);
  const body = document.createElement('tbody'); rows.forEach(values => { const tr = document.createElement('tr'); values.forEach(value => { const td = document.createElement('td'); td.append(value instanceof Node ? value : document.createTextNode(String(value))); tr.append(td); }); body.append(tr); }); table.append(body); host.append(table);
}
async function api(path, method = 'GET', body, key = null) {
  // Owner sessions use an HttpOnly cookie; customer calls use their own bearer key.
  const headers = key === null ? {'X-CSRF-Token': csrfToken} : {Authorization: 'Bearer ' + key};
  if(body !== undefined) headers['Content-Type'] = 'application/json';
  const response = await fetch(path, {method,headers,body:body === undefined ? undefined : JSON.stringify(body),cache:'no-store',credentials:'same-origin'});
  const data = await response.json();
  if(!response.ok) {
    if(response.status === 401 && key === null && !path.startsWith('/auth/')) {csrfToken='';$('locked').hidden=false;$('connect').hidden=false;$('disconnect').classList.add('hidden');}
    throw new Error((data.error?.message || 'Request failed') + ' [' + (data.error?.code || response.status) + ']');
  }
  return data;
}
function modelOptions(id, models, multiple=false) {
  const select=$(id), selected=new Set([...select.selectedOptions].map(option=>option.value));
  if([...select.options].map(option=>option.value).join('|')===models.map(model=>model.id).join('|')) return;
  select.replaceChildren(); models.forEach((model,index)=>{const option=document.createElement('option');option.value=model.id;option.textContent=model.id+(model.pricing_source==='free_tier_assumed'?' · Free-tier rates':'');option.selected=selected.has(model.id)||(!selected.size&&index===0);select.append(option);});
}
function showTab(name) { document.querySelectorAll('.tab').forEach(node => node.hidden = node.id !== 'tab-' + name); document.querySelectorAll('[data-tab]').forEach(node => node.classList.toggle('active', node.dataset.tab === name)); }
document.querySelectorAll('[data-tab]').forEach(node => node.onclick = () => showTab(node.dataset.tab));
document.querySelectorAll('[data-jump]').forEach(node => node.onclick = () => showTab(node.dataset.jump));
function render(data) {
  // All wallet totals use integer micro-USD even in the dashboard.
  const customers = data.customers, name = id => customers.find(customer => customer.id === id)?.name || id;
  $('m-available').textContent = money(data.totals.available);
  $('m-reserved').textContent = money(data.totals.reserved);
  $('m-spent').textContent=money(data.totals.spent);$('m-customers').textContent=data.totals.customers;
  table('wallet-table', ['Customer', 'Available', 'Reserved', 'Spent', 'Lifetime cap'], customers.map(row => [row.name + (row.frozen ? ' · Suspended' : ''), money(row.available_usd), money(row.reserved_usd), money(row.spent_usd), row.spend_cap_usd === null ? 'No cap' : money(row.spend_cap_usd)]));
  ['grant-customer', 'key-customer', 'cap-customer', 'freeze-customer'].forEach(id => { const select = $(id), prior = select.value; select.replaceChildren(); customers.forEach(row => { const option = document.createElement('option'); option.value = row.id; option.textContent = row.name; select.append(option); }); if (customers.some(row => row.id === prior)) select.value = prior; });
  table('key-table', ['Customer', 'Label', 'Prefix', 'Models', 'Status', 'Action'], data.keys.map(row => { const button = document.createElement('button'); button.textContent = 'Revoke'; button.className = 'danger'; button.disabled = !row.active; button.onclick = async () => { if (!confirm('Revoke this key? Existing requests will still settle.')) return; try { await api('/admin/keys/' + row.id, 'DELETE'); notify('Key revoked.'); await refresh(); } catch(error) { notify(error.message, true); } }; return [name(row.customer_id), row.label, row.prefix + '…', JSON.parse(row.allowed_models).join(', '), badge(!row.active?'revoked':row.expires_at&&row.expires_at<Date.now()/1000?'expired':'active'), button]; }));
  table('request-table', ['Request', 'Customer', 'Model / feature', 'State', 'Reserved', 'Final cost', 'Tokens in / out'], data.requests.map(row => [cell(row.id, 'mono'), name(row.customer_id), row.model + ' / ' + row.feature, badge(row.state), money(row.reserved_usd), row.cost_usd === null ? 'Pending' : money(row.cost_usd), row.input_tokens === null ? '—' : row.input_tokens + ' / ' + row.output_tokens]));
  table('ledger-table', ['Entry', 'Customer', 'Kind', 'Amount', 'Reference'], data.ledger.map(row => [row.id, name(row.customer_id), badge(row.kind), money(row.amount_usd), cell(row.reference, 'mono')]));
  table('price-table', ['Model', 'Input / 1M', 'Output / 1M', 'Cached / 1M', 'Price status'], data.models.map(row => [row.id, money(row.input_usd_per_million), money(row.output_usd_per_million),money(row.cached_usd_per_million),(row.pricing_source==='free_tier_assumed'?'Free-tier rates · ':'')+row.price_version+(row.price_current?'':' · REVIEW REQUIRED')]));
  table('feature-table', ['Feature', 'Settled requests', 'Cost'], data.features.map(row => [row.feature, row.requests, formatMicro(BigInt(row.cost_micro))]));

  const onlyGemini=data.mode==='live'&&data.models.length>0&&data.models.every(model=>model.owned_by==='gemini');
  $('mode-badge').textContent=onlyGemini?'GEMINI · FREE-TIER RATES':data.mode==='live'?'LIVE PROVIDERS':'SIMULATOR MODE';
  $('mode-notice').textContent=onlyGemini?'Real Gemini responses. Tollgate records tokens and a $0 charge using free-tier rates. Your Google project must show Free tier in AI Studio; Tollgate cannot verify your billing tier. Free quotas apply, and test content may be used by Google to improve its products.':data.mode==='live'?'Live AI requests use your provider accounts and saved price versions. Gemini rates assume a Free-tier Google project; Claude uses a conservative hold until final usage arrives.':'Simulator mode: replies, usage, and prices are test data. The ledger and credit reservations are persistent.';
  $('checkout-panel').hidden=!data.payments_enabled;
  modelOptions('play-model',data.models);modelOptions('key-models',data.models,true);
  const age=data.worker_last_seen?Math.round(Date.now()/1000-data.worker_last_seen):null;
  const clustered=data.service_role==='control';
  document.querySelector('[data-tab="playground"]').hidden=clustered;
  if(clustered&&!$('tab-playground').hidden)showTab('overview');
  $('cluster-panel').hidden=!clustered;
  if(clustered&&data.cluster){
    table('cluster-table',['Node','Role','Attendance','Last seen'],data.cluster.nodes.map(row=>[row.node_id,row.role,row.status,row.last_seen_seconds===null?'Never':Math.round(row.last_seen_seconds)+'s ago']));
    $('patrol-status').textContent=(data.cluster.patrol_recent?'Patrol is reporting. ':'Patrol has no recent report. ')+(data.cluster.patrol?.findings.map(item=>item.code+(item.node_id?' ('+item.node_id+')':'')).join('; ')||'No findings recorded.');
  }
  $('worker-status').textContent=(age!==null&&age<60?'Worker healthy · last seen '+age+'s ago':'Worker needs attention · no recent heartbeat')+' · '+data.unresolved+' unresolved request(s)';
  table('audit-table',['When','Action','Target','Details'],data.audit.map(row=>[new Date(row.created_at*1000).toLocaleString(),row.action,cell(row.target,'mono'),cell(row.detail,'details')]));
  $('queue').replaceChildren(...['pending', 'done', 'blocked'].map(state => cell(state + ': ' + (data.queue[state] || 0))));
}
async function refresh(quiet = false) {
  if (!csrfToken || refreshing) return; refreshing = true;
  try { snapshot = await api('/admin/overview'); render(snapshot); $('connection').textContent = 'Connected'; $('connection').className = 'pill good'; $('locked').hidden = true; $('connect').hidden = true; $('disconnect').classList.remove('hidden'); }
  catch(error) { if (!quiet) notify(error.message, true); $('connection').textContent = 'Refresh failed'; } finally { refreshing = false; }
}
function openAuth() { $('auth-dialog').showModal(); $('admin-key').focus(); }
$('connect').onclick = $('unlock').onclick = openAuth; $('cancel-auth').onclick = () => $('auth-dialog').close();
$('auth-form').onsubmit=async event=>{event.preventDefault();const button=$('auth-form').querySelector('button');button.disabled=true;try{const result=await api('/auth/login','POST',{key:$('admin-key').value.trim()});csrfToken=result.csrf_token;$('admin-key').value='';$('auth-dialog').close();await refresh();notify('Signed in. Your workspace is ready.');}catch(error){notify(error.message,true);}finally{button.disabled=false;}};
$('disconnect').onclick=async()=>{try{await api('/auth/logout','POST');location.reload();}catch(error){notify(error.message,true);}};$('refresh').onclick=()=>refresh();
function bindForm(id, action) { $(id).onsubmit = async event => { event.preventDefault(); if (!csrfToken) { openAuth(); return; } const button = $(id).querySelector('button[type="submit"],button:not([type])'); if(button) button.disabled = true; try { await action(); await refresh(); } catch(error) { notify(error.message, true); } finally { if(button) button.disabled = false; } }; }
bindForm('customer-form', async () => { const customer = await api('/admin/customers', 'POST', { name: $('customer-name').value }); $('customer-name').value = ''; const freeOnly=snapshot?.models.length>0&&snapshot.models.every(model=>model.pricing_source==='free_tier_assumed'); notify('Created ' + customer.name + (freeOnly?'. Create its virtual key below. Free-tier Gemini needs no credit grant.':'. Grant credits and issue its key below.')); });
$('grant-ref').value = 'grant-' + crypto.randomUUID();
bindForm('grant-form', async () => { await api('/admin/credits', 'POST', {customer_id: $('grant-customer').value, amount_usd: $('grant-amount').value, reference: $('grant-ref').value}); notify('Credits granted. Repeating this reference will not add credits twice. Change the reference for a new grant.'); });
bindForm('key-form', async () => { const result = await api('/admin/keys', 'POST', {customer_id: $('key-customer').value, label: $('key-label').value, allowed_models:[...$('key-models').selectedOptions].map(option=>option.value),expires_in_days:$('key-expiry').value?Number($('key-expiry').value):null}); $('new-key').value = result.api_key; $('play-key').value = result.api_key; $('new-key-panel').classList.remove('hidden'); notify('Virtual key created. Copy it now; the database stores only its hash.'); });
bindForm('cap-form', async () => { await api('/admin/spend-cap', 'PUT', {customer_id: $('cap-customer').value, amount_usd: $('cap-amount').value || null}); notify('Lifetime spend cap saved.'); });
$('copy-key').onclick = async () => { try { await navigator.clipboard.writeText($('new-key').value); notify('Key copied.'); } catch { notify('Select and copy the key manually.', true); } };
$('settle').onclick = async () => { try { const result = await api('/admin/settle', 'POST'); notify('Settled ' + result.settled + ' ready job(s).'); await refresh(); } catch(error) { notify(error.message, true); } };
$('play-form').onsubmit = async event => {
  // The playground uses a customer key independently of the owner connection.
  event.preventDefault(); $('send').disabled = true; $('play-output').textContent = ''; $('play-status').textContent = ''; $('inspect').disabled = true;
  const key = $('play-key').value.trim(), stream = $('play-stream').checked, idem = crypto.randomUUID();
  try {
    const response = await fetch('/v1/chat/completions', {method:'POST',headers:{Authorization:'Bearer '+key,'Content-Type':'application/json','Idempotency-Key':idem,'X-Tollgate-Feature':$('play-feature').value},body:JSON.stringify({model:$('play-model').value,messages:[{role:'user',content:$('play-prompt').value}],max_completion_tokens:Number($('play-max').value),stream})});
    lastRequest = response.headers.get('X-Tollgate-Request-ID'); lastCustomerKey = key; $('inspect').disabled = !lastRequest;
    $('play-meta').textContent = 'Request: '+(lastRequest||response.headers.get('X-Request-ID'))+' · Reserved: $'+(response.headers.get('X-Tollgate-Reservation-USD')||'0')+' · Usage: '+response.headers.get('X-Tollgate-Usage-Source');
    if (!response.ok) { const data=await response.json(); throw new Error(data.error?.message||'Request failed'); }
    if (!stream) { const data=await response.json(); $('play-output').textContent=data.choices[0].message.content; }
    else {
      const reader=response.body.getReader(), decoder=new TextDecoder(); let buffer='', completed=false;
      try { while(true) { const {value,done}=await reader.read(); if(done) break; buffer+=decoder.decode(value,{stream:true}); let boundary;
        while((boundary=buffer.indexOf('\n\n'))>=0) { const frame=buffer.slice(0,boundary); buffer=buffer.slice(boundary+2); const data=frame.split('\n').filter(line=>line.startsWith('data:')).map(line=>line.slice(5).trimStart()).join('\n'); if(data==='[DONE]'){completed=true;continue;}if(!data)continue; const item=JSON.parse(data); if(item.error) throw new Error(item.error.message); for(const choice of item.choices||[]) if(choice.delta?.content) $('play-output').textContent+=choice.delta.content; if(item.usage) $('play-meta').textContent+=' · Tokens: '+item.usage.prompt_tokens+' in / '+item.usage.completion_tokens+' out'; }
      }if(!completed)throw new Error('Connection ended before completion. Inspect the request before retrying.');} finally { await reader.cancel(); reader.releaseLock(); }
    }
    notify('Answer received. The durable worker will settle its usage.'); await refresh(true);
  } catch(error) { $('play-output').textContent+='\n\n'+error.message; notify(error.message,true); await refresh(true); } finally { $('send').disabled=false; }
};
$('inspect').onclick=async()=>{if(!lastRequest)return;try{const record=await api('/v1/requests/'+lastRequest,'GET',undefined,lastCustomerKey);$('play-status').textContent=JSON.stringify(record,null,2);await refresh(true);}catch(error){notify(error.message,true);}};
setInterval(()=>{if(csrfToken&&!document.hidden)refresh(true);},5000);

bindForm('freeze-form',async()=>{await api('/admin/customers/'+$('freeze-customer').value+'/freeze','POST',{frozen:$('freeze-action').value==='true',reason:$('freeze-reason').value});notify('Customer access updated.');});
bindForm('resolve-form',async()=>{if(!confirm('Apply this accounting decision using the evidence entered?'))return;const result=await api('/admin/requests/'+encodeURIComponent($('resolve-id').value.trim())+'/resolve','POST',{action:$('resolve-action').value,evidence:$('resolve-evidence').value,input_tokens:Number($('resolve-input').value),output_tokens:Number($('resolve-output').value),cached_tokens:Number($('resolve-cached').value),cache_write_tokens:Number($('resolve-written').value)});notify('Request state: '+result.state);});
$('reconcile').onclick=async()=>{try{$('reconcile-output').textContent=JSON.stringify(await api('/admin/reconciliation'),null,2);}catch(error){notify(error.message,true);}};
let checkoutReference=crypto.randomUUID();
$('checkout-amount').onchange=()=>{checkoutReference=crypto.randomUUID();$('checkout-link').classList.add('hidden');};
$('checkout-form').onsubmit=async event=>{event.preventDefault();const button=$('checkout-form').querySelector('button');button.disabled=true;try{const result=await api('/v1/billing/checkout','POST',{amount_usd:$('checkout-amount').value,reference:checkoutReference},$('play-key').value.trim());$('checkout-link').href=result.checkout_url;$('checkout-link').classList.remove('hidden');notify('Checkout ready. Use the link to complete payment securely.');}catch(error){notify(error.message,true);}finally{button.disabled=false;}};
(async()=>{try{const session=await api('/auth/session');csrfToken=session.csrf_token;await refresh(true);}catch{/* A signed-out page is a normal state. */}})();
