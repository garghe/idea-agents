/* IDEA portal (Intake, Design & Evaluation Agents). Plain JavaScript, no build step.
 *
 * Reads config.json at start:
 *   local:  {"mode": "mock", "apiBase": "/api", "pipeline": {...}, "sampleRequest": {...}}
 *   AWS:    {"mode": "aws",  "apiBase": "/api", "pipeline": {...}, "auth": {"baseUrl", "clientId", "redirectUri"}}
 * On AWS, people sign in through Cognito (authorization code flow with PKCE) and every API call
 * carries their ID token. Everything shown from the API is escaped before it reaches the page.
 */
(function () {
  'use strict';

  var config = null;
  var app = document.getElementById('app');
  var pollTimer = null;

  // ------------------------------------------------------------------ helpers
  function esc(value) {
    return String(value == null ? '' : value)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }
  function store(kind) {
    try { return window[kind]; } catch (e) { return null; }
  }
  function getItem(kind, key) {
    var s = store(kind);
    try { return s ? s.getItem(key) : null; } catch (e) { return null; }
  }
  function setItem(kind, key, value) {
    var s = store(kind);
    try { if (s) { if (value === null) { s.removeItem(key); } else { s.setItem(key, value); } } } catch (e) { /* storage unavailable */ }
  }
  function fmtTime(iso) {
    if (!iso) { return ''; }
    var d = new Date(iso);
    if (isNaN(d)) { return ''; }
    return d.toLocaleString('en-GB', { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' });
  }
  function fmtDate(iso) {  // with the year, for created and updated dates
    if (!iso) { return ''; }
    var d = new Date(iso);
    if (isNaN(d)) { return ''; }
    return d.toLocaleString('en-GB', { day: 'numeric', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit' });
  }
  function age(iso) {
    var mins = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 60000));
    if (mins < 60) { return mins + ' min'; }
    var hours = Math.round(mins / 60);
    if (hours < 48) { return hours + ' h'; }
    return Math.round(hours / 24) + ' d';
  }
  function money(n) { return '$' + Number(n || 0).toFixed(2); }
  function isMock() { return config && config.mode === 'mock'; }
  function toast(text) {
    var t = document.createElement('div');
    t.className = 'toast';
    t.setAttribute('role', 'status');
    t.textContent = text;
    document.body.appendChild(t);
    setTimeout(function () { t.remove(); }, 3000);
  }
  function statusPill(status, text) {
    var cls = { waiting_answers: 'wait', waiting_gate: 'wait', waiting_escalation: 'wait', running: 'work',
      'new': 'work', done: 'done', rejected: 'bad', failed: 'bad' }[status] || 'grey';
    return '<span class="pill ' + cls + '">' + esc(text) + '</span>';
  }
  function actingAs() { return getItem('localStorage', 'idea.name') || 'Local user'; }

  // ------------------------------------------------------------------ auth (AWS only)
  function b64url(bytes) {
    var s = '';
    bytes.forEach(function (b) { s += String.fromCharCode(b); });
    return btoa(s).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
  }
  function randomString(n) {
    var bytes = new Uint8Array(n);
    crypto.getRandomValues(bytes);
    return b64url(bytes);
  }
  function jwtClaims(token) {
    try {
      var part = token.split('.')[1].replace(/-/g, '+').replace(/_/g, '/');
      return JSON.parse(decodeURIComponent(escape(atob(part))));
    } catch (e) { return null; }
  }
  function currentToken() {
    var token = getItem('sessionStorage', 'idea.idToken');
    var claims = token && jwtClaims(token);
    if (!claims || claims.exp * 1000 < Date.now() + 60000) { return null; }
    return token;
  }
  async function login() {
    var a = config.auth;
    var verifier = randomString(48);
    var st = randomString(16);
    setItem('sessionStorage', 'idea.pkce', verifier);
    setItem('sessionStorage', 'idea.state', st);
    setItem('sessionStorage', 'idea.returnTo', location.hash || '#/cases');
    var digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(verifier));
    var challenge = b64url(new Uint8Array(digest));
    location.assign(a.baseUrl + '/oauth2/authorize?response_type=code&client_id=' + encodeURIComponent(a.clientId) +
      '&redirect_uri=' + encodeURIComponent(a.redirectUri) + '&scope=' + encodeURIComponent('openid email profile') +
      '&code_challenge_method=S256&code_challenge=' + challenge + '&state=' + st);
  }
  async function finishLogin() {
    var params = new URLSearchParams(location.search);
    var code = params.get('code');
    if (!code) { return false; }
    if (params.get('state') !== getItem('sessionStorage', 'idea.state')) {
      throw new Error('Sign-in failed: state mismatch. Please try again.');
    }
    var a = config.auth;
    var body = new URLSearchParams({
      grant_type: 'authorization_code', client_id: a.clientId, code: code, redirect_uri: a.redirectUri,
      code_verifier: getItem('sessionStorage', 'idea.pkce') || ''
    });
    var resp = await fetch(a.baseUrl + '/oauth2/token', {
      method: 'POST', headers: { 'Content-Type': 'application/x-www-form-urlencoded' }, body: body
    });
    if (!resp.ok) { throw new Error('Sign-in failed (' + resp.status + '). Please try again.'); }
    var tokens = await resp.json();
    setItem('sessionStorage', 'idea.idToken', tokens.id_token);
    setItem('sessionStorage', 'idea.pkce', null);
    var returnTo = getItem('sessionStorage', 'idea.returnTo') || '#/cases';
    history.replaceState(null, '', location.pathname + returnTo);
    return true;
  }
  function logout() {
    setItem('sessionStorage', 'idea.idToken', null);
    var a = config.auth;
    location.assign(a.baseUrl + '/logout?client_id=' + encodeURIComponent(a.clientId) +
      '&logout_uri=' + encodeURIComponent(a.redirectUri));
  }

  // ------------------------------------------------------------------ API
  async function call(path, options) {
    options = options || {};
    var headers = { 'Accept': 'application/json' };
    if (options.body) { headers['Content-Type'] = 'application/json'; }
    if (config.mode === 'aws') {
      var token = currentToken();
      if (!token) { await login(); throw new Error('Signing in…'); }
      headers['Authorization'] = token;
    }
    var resp = await fetch(config.apiBase + path, {
      method: options.method || 'GET', headers: headers,
      body: options.body ? JSON.stringify(options.body) : undefined
    });
    var data = {};
    try { data = await resp.json(); } catch (e) { data = {}; }
    if (resp.status === 401 && config.mode === 'aws') { await login(); }
    if (!resp.ok) { throw new Error(data.error || data.message || ('Request failed (' + resp.status + ')')); }
    return data;
  }

  // ------------------------------------------------------------------ markdown (safe subset)
  function inline(text) {
    return esc(text)
      .replace(/`([^`]+)`/g, '<code>$1</code>')
      .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
      .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
  }
  function markdown(src) {
    var lines = String(src || '').replace(/\r/g, '').split('\n');
    var out = [];
    var i = 0;
    function cells(row) { return row.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map(function (c) { return c.trim(); }); }
    while (i < lines.length) {
      var line = lines[i];
      if (/^```/.test(line)) {
        var code = [];
        i++;
        while (i < lines.length && !/^```/.test(lines[i])) { code.push(lines[i]); i++; }
        i++;
        out.push('<pre><code>' + esc(code.join('\n')) + '</code></pre>');
        continue;
      }
      var h = /^(#{1,4})\s+(.*)$/.exec(line);
      if (h) { var lv = Math.min(h[1].length, 3); out.push('<h' + lv + '>' + inline(h[2]) + '</h' + lv + '>'); i++; continue; }
      if (/^\s*\|/.test(line) && i + 1 < lines.length && /^\s*\|?\s*:?-{3,}/.test(lines[i + 1])) {
        var head = cells(line);
        i += 2;
        var rows = [];
        while (i < lines.length && /^\s*\|/.test(lines[i])) { rows.push(cells(lines[i])); i++; }
        out.push('<table><thead><tr>' + head.map(function (c) { return '<th>' + inline(c) + '</th>'; }).join('') +
          '</tr></thead><tbody>' + rows.map(function (r) {
            return '<tr>' + r.map(function (c) { return '<td>' + inline(c) + '</td>'; }).join('') + '</tr>';
          }).join('') + '</tbody></table>');
        continue;
      }
      if (/^\s*>/.test(line)) {
        var quote = [];
        while (i < lines.length && /^\s*>/.test(lines[i])) { quote.push(lines[i].replace(/^\s*>\s?/, '')); i++; }
        out.push('<blockquote>' + inline(quote.join(' ')) + '</blockquote>');
        continue;
      }
      if (/^\s*([-*]|\d+\.)\s+/.test(line)) {
        var ordered = /^\s*\d+\./.test(line);
        var items = [];
        while (i < lines.length && /^\s*([-*]|\d+\.)\s+/.test(lines[i])) {
          items.push('<li>' + inline(lines[i].replace(/^\s*([-*]|\d+\.)\s+/, '')) + '</li>');
          i++;
        }
        out.push((ordered ? '<ol>' : '<ul>') + items.join('') + (ordered ? '</ol>' : '</ul>'));
        continue;
      }
      if (!line.trim()) { i++; continue; }
      var para = [];
      while (i < lines.length && lines[i].trim() && !/^(#{1,4}\s|```|\s*\||\s*>|\s*([-*]|\d+\.)\s)/.test(lines[i])) {
        para.push(lines[i]);
        i++;
      }
      out.push('<p>' + inline(para.join(' ')) + '</p>');
    }
    return '<div class="md">' + out.join('') + '</div>';
  }
  function backlogHtml(text) {
    var data;
    try { data = JSON.parse(text); } catch (e) { return '<pre>' + esc(text) + '</pre>'; }
    if (!data.epics) { return '<pre>' + esc(JSON.stringify(data, null, 2)) + '</pre>'; }
    return '<div class="md">' + data.epics.map(function (e) {
      return '<div class="epic"><h2>' + esc(e.id) + ' · ' + esc(e.title) + '</h2><p class="muted">' + esc(e.description) + '</p>' +
        e.stories.map(function (s) {
          return '<div class="story"><div class="id">' + esc(s.id) + (s.depends_on && s.depends_on.length ? ' · depends on ' + esc(s.depends_on.join(', ')) : '') +
            '</div><strong>' + esc(s.title) + '</strong><p>As ' + (/^[aeiou]/i.test(s.as_a) ? 'an ' : 'a ') + esc(s.as_a) + ', I want ' + esc(s.i_want) + ', so that ' + esc(s.so_that) + '.</p>' +
            '<ul>' + s.acceptance_criteria.map(function (c) { return '<li>' + esc(c) + '</li>'; }).join('') + '</ul></div>';
        }).join('') + '</div>';
    }).join('') + '</div>';
  }

  // ------------------------------------------------------------------ chrome
  function setNav(active) {
    document.querySelectorAll('.topnav a').forEach(function (a) {
      a.classList.toggle('active', a.getAttribute('data-nav') === active);
    });
  }
  function renderUserArea() {
    var area = document.getElementById('user-area');
    if (isMock()) {
      area.innerHTML = '<div class="acting-as"><label for="acting-as" class="small">Acting as</label>' +
        '<input id="acting-as" value="' + esc(actingAs()) + '" autocomplete="off"></div>';
      document.getElementById('acting-as').addEventListener('change', function (e) {
        setItem('localStorage', 'idea.name', e.target.value.trim() || 'Local user');
      });
      return;
    }
    var claims = jwtClaims(currentToken() || '') || {};
    var name = claims.name || claims.email || 'Signed in';
    var initials = name.split(/[\s@.]+/).filter(Boolean).slice(0, 2).map(function (p) { return p[0].toUpperCase(); }).join('');
    area.innerHTML = '<span>' + esc(name) + '</span><span class="avatar" aria-hidden="true">' + esc(initials) + '</span>' +
      '<button type="button" class="btn" id="logout" style="min-height:36px;padding:6px 12px">Sign out</button>';
    document.getElementById('logout').addEventListener('click', logout);
  }
  async function refreshWaitingCount(counts) {
    var el = document.getElementById('waiting-count');
    if (!counts) {
      try { counts = (await call('/cases')).counts; } catch (e) { return; }
    }
    el.textContent = counts.waiting;
    el.hidden = !counts.waiting;
  }
  function showError(err) {
    app.innerHTML = '<div class="card"><h2>Something went wrong</h2><p>' + esc(err.message || err) + '</p>' +
      '<a href="#/cases">Back to cases</a></div>';
  }

  // ------------------------------------------------------------------ case list
  var FILTERS = [
    { id: 'all', label: 'All', key: 'all', test: function () { return true; } },
    { id: 'waiting', label: 'Waiting for people', key: 'waiting',
      test: function (r) { return /^waiting/.test(r.status); } },
    { id: 'working', label: 'Agents working', key: 'working',
      test: function (r) { return r.status === 'running' || r.status === 'new'; } },
    { id: 'closed', label: 'Closed', key: 'closed',
      test: function (r) { return ['done', 'rejected', 'failed'].indexOf(r.status) >= 0; } }
  ];

  async function listPage(filterId) {
    setNav(filterId === 'waiting' ? 'waiting' : 'cases');
    var data = await call('/cases');
    refreshWaitingCount(data.counts);
    var filter = FILTERS.find(function (f) { return f.id === filterId; }) || FILTERS[0];
    var rows = data.cases.filter(filter.test);
    var c = data.counts;
    app.innerHTML =
      '<div class="page-head"><div><h1>Cases</h1><div class="muted">Business requests moving through intake, design and evaluation</div></div>' +
      '<a class="btn primary" href="#/new" style="text-decoration:none;display:inline-flex;align-items:center">New request</a></div>' +
      '<div class="kpis">' +
      '<div class="kpi strong"><div class="muted small">Waiting for people</div><div class="n">' + c.waiting + '</div></div>' +
      '<div class="kpi"><div class="muted small">Agents working</div><div class="n">' + c.working + '</div></div>' +
      '<div class="kpi"><div class="muted small">Released</div><div class="n">' + c.done + '</div></div>' +
      '<div class="kpi"><div class="muted small">All cases</div><div class="n">' + c.all + '</div></div></div>' +
      '<div class="filters" role="tablist" aria-label="Filter cases">' + FILTERS.map(function (f) {
        return '<button type="button" role="tab" class="filter" data-filter="' + f.id + '" aria-selected="' + (f === filter) + '">' +
          esc(f.label) + ' (' + c[f.key] + ')</button>';
      }).join('') + '</div>' +
      '<section class="table-box" aria-label="Case list">' +
      (rows.length ? '<table class="cases"><thead><tr><th>Request</th><th>Progress</th><th>Waiting for</th><th>Age</th><th class="num">Spend</th></tr></thead><tbody>' +
        rows.map(function (r) {
          return '<tr><td><a class="title" href="#/cases/' + esc(r.id) + '">' + esc(r.title) + '</a>' +
            '<div class="muted small">' + esc(r.id) + ' · ' + esc(r.requester) + '</div></td>' +
            '<td><div class="dots" aria-hidden="true">' + r.progress.map(function (p) { return '<span class="dot ' + esc(p) + '"></span>'; }).join('') + '</div>' +
            '<div class="small">' + esc(r.stage_note) + '</div></td>' +
            '<td>' + statusPill(r.status, r.waiting_for) + '</td>' +
            '<td class="muted">' + esc(age(r.created_at)) + '</td>' +
            '<td class="num">' + money(r.cost_usd) + '</td></tr>';
        }).join('') + '</tbody></table>'
        : '<div class="empty">No cases here yet. <a href="#/new">Start a new request</a>.</div>') +
      '</section><p class="muted small">Progress bars: one segment per stage' +
      (isMock() ? '. All cases on this machine use mock data.' : '.') + '</p>';
    app.querySelectorAll('[data-filter]').forEach(function (b) {
      b.addEventListener('click', function () {
        location.hash = '#/cases' + (b.getAttribute('data-filter') === 'all' ? '' : '?filter=' + b.getAttribute('data-filter'));
      });
    });
  }

  // ------------------------------------------------------------------ attachments (drop zone + upload)
  var picked = {};
  function attLimits() { return (config.pipeline && config.pipeline.attachments) || { types: [], max_file_mb: 20, max_image_mb: 3.75, max_files: 20 }; }
  var IMAGE_EXT = ['png', 'jpg', 'jpeg', 'gif', 'webp'];
  function extOf(name) { var m = /\.([^.]+)$/.exec(name); return m ? m[1].toLowerCase() : ''; }
  function sizeText(n) { return n >= 1048576 ? (n / 1048576).toFixed(1) + ' MB' : Math.max(1, Math.round(n / 1024)) + ' KB'; }
  function dropZoneHtml(id, label) {
    var L = attLimits();
    picked[id] = [];
    return '<div class="drop" id="drop-' + id + '"><div class="field" style="margin-bottom:4px">' + esc(label) + '</div>' +
      '<input type="file" multiple id="file-' + id + '" class="sr-only" accept="' + esc(L.types.map(function (t) { return '.' + t; }).join(',')) + '">' +
      '<label for="file-' + id + '" class="drop-target"><span><strong>Choose files</strong> or drop them here</span>' +
      '<span class="muted small">PDF, Word, HTML, text and images. Up to ' + L.max_file_mb + ' MB each (images ' + L.max_image_mb + ' MB).</span></label>' +
      '<ul class="file-list" id="list-' + id + '" aria-live="polite"></ul><div class="error small" id="ferr-' + id + '" role="alert"></div></div>';
  }
  function renderPicked(id) {
    var list = document.getElementById('list-' + id);
    list.innerHTML = picked[id].map(function (f, i) {
      return '<li><span>' + esc(f.name) + ' <span class="muted small">' + sizeText(f.size) + '</span></span>' +
        '<button type="button" class="link-btn" data-remove="' + i + '" aria-label="Remove ' + esc(f.name) + '">Remove</button></li>';
    }).join('');
    list.querySelectorAll('[data-remove]').forEach(function (b) {
      b.addEventListener('click', function () { picked[id].splice(Number(b.getAttribute('data-remove')), 1); renderPicked(id); });
    });
  }
  function addFiles(id, files) {
    var L = attLimits();
    var err = document.getElementById('ferr-' + id);
    var problems = [];
    Array.prototype.forEach.call(files, function (f) {
      var ext = extOf(f.name);
      var cap = (IMAGE_EXT.indexOf(ext) >= 0 ? L.max_image_mb : L.max_file_mb) * 1048576;
      if (L.types.indexOf(ext) < 0) { problems.push(f.name + ': this type of file is not accepted'); return; }
      if (f.size > cap) { problems.push(f.name + ': too large'); return; }
      if (!f.size) { problems.push(f.name + ': the file is empty'); return; }
      if (picked[id].length >= L.max_files) { problems.push('At most ' + L.max_files + ' files'); return; }
      picked[id].push(f);
    });
    err.textContent = problems.join('. ');
    renderPicked(id);
  }
  function bindDropZone(id) {
    var input = document.getElementById('file-' + id);
    var zone = document.getElementById('drop-' + id);
    input.addEventListener('change', function () { addFiles(id, input.files); input.value = ''; });
    zone.addEventListener('dragover', function (e) { e.preventDefault(); zone.classList.add('over'); });
    zone.addEventListener('dragleave', function () { zone.classList.remove('over'); });
    zone.addEventListener('drop', function (e) { e.preventDefault(); zone.classList.remove('over'); addFiles(id, e.dataTransfer.files); });
  }
  function pickedMeta(id) { return (picked[id] || []).map(function (f) { return { name: f.name, size: f.size }; }); }
  // Uploads go straight to storage: locally to this server, on AWS to S3 through a short-lived signed link.
  async function uploadFiles(uploads, files, onProgress) {
    for (var i = 0; i < uploads.length; i++) {
      if (onProgress) { onProgress(i + 1, uploads.length); }
      var u = uploads[i];
      var resp = await fetch(u.url, { method: u.method || 'PUT', headers: u.headers || {}, body: files[i] });
      if (!resp.ok) { throw new Error('Upload of ' + files[i].name + ' failed (' + resp.status + ')'); }
    }
  }
  async function attachTo(caseId, id, onProgress) {
    var files = picked[id] || [];
    if (!files.length) { return 0; }
    var res = await call('/cases/' + encodeURIComponent(caseId) + '/attachments', { method: 'POST', body: { files: pickedMeta(id), by: actingAs() } });
    await uploadFiles(res.uploads, files, onProgress);
    picked[id] = [];
    return files.length;
  }

  // ------------------------------------------------------------------ new request
  function newPage() {
    setNav('new');
    var sample = config.sampleRequest;
    app.innerHTML =
      '<div class="crumbs"><a href="#/cases">Cases</a> / New request</div>' +
      '<div class="page-head"><div><h1>New request</h1><div class="muted">Describe what the business needs. The intake agent will ask if anything essential is missing.</div></div></div>' +
      '<form class="card form-card stack" id="new-form" novalidate>' +
      (isMock() ? '<div class="notice">Mock mode: the agents are scripted and write mock documents. Use any text, or fill in the sample.' +
        (sample ? ' <button type="button" class="btn" id="fill-sample" style="margin-left:8px;min-height:36px;padding:6px 12px">Fill in the sample request</button>' : '') + '</div>' : '') +
      '<div><label class="field" for="title">Title</label><input class="input" id="title" maxlength="200" required></div>' +
      '<div><label class="field" for="requester">Requested by</label><input class="input" id="requester" maxlength="100" value="' + esc(isMock() ? actingAs() : '') + '"></div>' +
      '<div><label class="field" for="request">The request</label>' +
      '<textarea class="input" id="request" rows="12" required placeholder="What do you need, for whom, and by when? Include any constraints you know about."></textarea></div>' +
      dropZoneHtml('new', 'Supporting documents (optional)') +
      '<div class="btn-row"><button class="btn primary" type="submit" id="submit">Start the pipeline</button><span class="error" id="form-error" role="alert"></span></div>' +
      '</form>';
    bindDropZone('new');
    var fill = document.getElementById('fill-sample');
    if (fill) {
      fill.addEventListener('click', function () {
        document.getElementById('title').value = sample.title;
        document.getElementById('requester').value = sample.requester;
        document.getElementById('request').value = sample.request;
      });
    }
    document.getElementById('new-form').addEventListener('submit', async function (e) {
      e.preventDefault();
      var err = document.getElementById('form-error');
      var title = document.getElementById('title').value.trim();
      var request = document.getElementById('request').value.trim();
      err.textContent = '';
      if (!title || !request) { err.textContent = 'Enter a title and the request first.'; return; }
      var btn = document.getElementById('submit');
      btn.disabled = true;
      btn.textContent = 'Starting…';
      try {
        var files = picked['new'].slice();
        var res = await call('/cases', { method: 'POST', body: {
          title: title, request: request, requester: document.getElementById('requester').value.trim(),
          attachments: pickedMeta('new')
        } });
        if (res.uploads && res.uploads.length) {
          await uploadFiles(res.uploads, files, function (n, total) { btn.textContent = 'Uploading ' + n + ' of ' + total + '…'; });
        }
        location.hash = '#/cases/' + res.case_id;
      } catch (ex) {
        err.textContent = ex.message;
        btn.disabled = false;
        btn.textContent = 'Start the pipeline';
      }
    });
  }

  // ------------------------------------------------------------------ case page
  function timelineHtml(items) {
    var parts = [];
    items.forEach(function (it, idx) {
      if (idx > 0) {
        parts.push('<div class="tl-line' + (items[idx - 1].state === 'done' && it.state !== 'todo' ? ' done' : '') + '"></div>');
      }
      var mark;
      if (it.kind === 'gate') {
        mark = '<span class="diamond ' + esc(it.state) + '" aria-hidden="true"></span>';
      } else {
        var sym = { done: '&#10003;', waiting: '?', stopped: '&#10005;', current: '…', todo: '' }[it.state] || '';
        mark = '<span class="node ' + esc(it.state) + '" aria-hidden="true">' + sym + '</span>';
      }
      var stateText = { done: 'done', current: 'in progress', waiting: 'waiting', stopped: 'stopped', todo: 'not started' }[it.state];
      parts.push('<div class="tl-item ' + esc(it.kind) + ' ' + esc(it.state) + '" role="listitem" aria-label="' +
        esc(it.label + ': ' + stateText + (it.note ? ', ' + it.note : '')) + '">' + mark +
        '<span class="name">' + esc(it.label) + '</span><span class="note" title="' + esc(it.note) + '">' + esc(it.note) + '</span></div>');
    });
    return '<section class="timeline" aria-label="Pipeline progress"><div class="tl" role="list">' + parts.join('') + '</div></section>';
  }

  // ------------------------------------------------------------------ documents (grouped by stage)
  var ICONS = {
    person: '<svg viewBox="0 0 16 16" width="15" height="15" aria-hidden="true"><circle cx="8" cy="5.5" r="2.8" fill="none" stroke="currentColor" stroke-width="1.4"/><path d="M2.8 14c.6-2.7 2.7-4.2 5.2-4.2s4.6 1.5 5.2 4.2" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/></svg>',
    agent: '<svg viewBox="0 0 16 16" width="15" height="15" aria-hidden="true"><rect x="2.5" y="4.5" width="11" height="9" rx="2.5" fill="none" stroke="currentColor" stroke-width="1.4"/><circle cx="6" cy="9" r="1" fill="currentColor"/><circle cx="10" cy="9" r="1" fill="currentColor"/><path d="M8 4.5V2.5" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/></svg>',
    attachment: '<svg viewBox="0 0 16 16" width="15" height="15" aria-hidden="true"><path d="M10.5 4.5 5.6 9.4a1.6 1.6 0 0 0 2.3 2.3l5-5a3 3 0 0 0-4.3-4.3l-5 5a4.4 4.4 0 0 0 6.2 6.2l4.2-4.2" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/></svg>'
  };
  var docItems = {};
  var d_groups = [];
  function itemIcon(it) { return it.type === 'attachment' ? 'attachment' : (it.kind === 'agent' ? 'agent' : (it.kind === 'system' ? 'agent' : 'person')); }
  function itemMeta(it) {
    var parts = [it.by || (it.kind === 'person' ? 'Person' : 'Agent')];
    if (it.type === 'attachment') { parts.push(it.status === 'ready' ? (it.file_type || 'File') : (it.status === 'pending' ? 'Uploading' : 'Turned away')); }
    else if (it.versions > 1) { parts.push(it.versions + ' versions'); }
    if (it.at) { parts.push(fmtTime(it.at)); }
    return parts.join(' · ');
  }
  function docListHtml(groups) {
    docItems = {};
    return groups.map(function (g) {
      return '<div class="doc-group"><h3>' + esc(g.label) + '</h3><ul>' + g.items.map(function (it) {
        var inner = '<span class="ico ' + itemIcon(it) + '">' + ICONS[itemIcon(it)] + '</span><span class="txt"><span class="lbl">' + esc(it.label) +
          '</span><span class="meta">' + esc(itemMeta(it)) + '</span></span>';
        if (it.name && (it.type === 'document' || it.status === 'ready')) {
          docItems[it.name] = it;
          return '<li><button type="button" class="doc-item" data-doc="' + esc(it.name) + '">' + inner + '</button></li>';
        }
        return '<li><div class="doc-item static" title="' + esc(it.type === 'attachment' && it.status === 'ready' ? 'An image: agents open it directly' : '') + '">' + inner + '</div></li>';
      }).join('') + '</ul></div>';
    }).join('');
  }

  function actionHtml(d) {
    var c = d.case;
    var p = c.pending || {};
    if (c.status === 'waiting_answers') {
      return '<form class="card action stack" id="answer-form" novalidate><div><h2>' +
        (d.open_questions.length === 1 ? 'One question needs an answer' : esc(d.open_questions.length) + ' questions need answers') +
        '</h2><p class="muted" style="margin:0">Answer what you can. Short answers are fine; say "not decided yet" if that is the honest answer.</p></div>' +
        d.open_questions.map(function (q) {
          return '<div class="question"><div class="btn-row" style="gap:8px;margin-bottom:6px"><strong>' + esc(q.id) + '</strong>' +
            '<span class="pill grey">For ' + esc(q.for.replace('_', ' ')) + '</span><span class="muted small">from the ' + esc(q.raised_by) + ' agent</span></div>' +
            '<label class="field" for="q-' + esc(q.id) + '">' + esc(q.question) + '</label>' +
            '<div class="why">Why it matters: ' + esc(q.why_it_matters) + '</div>' +
            '<textarea class="input" rows="2" id="q-' + esc(q.id) + '" data-qid="' + esc(q.id) + '"></textarea></div>';
        }).join('') +
        dropZoneHtml('answers', 'Attach documents with your answers (optional)') +
        '<div class="btn-row"><button class="btn primary" type="submit" id="send-answers">Send answers</button>' +
        '<span class="muted small">The ' + esc(c.stage) + ' stage reruns with your answers.</span></div>' +
        '<div class="error" id="action-error" role="alert"></div></form>';
    }
    if (c.status === 'waiting_gate' || c.status === 'waiting_escalation') {
      var esc8n = c.status === 'waiting_escalation';
      var gateLabel = (d.progress.find(function (x) { return x.kind === 'gate' && x.id === p.gate; }) || {}).label || 'Decision';
      var open = d.findings.filter(function (f) { return f.status === 'open'; });
      var body = esc8n
        ? '<p class="muted">The review panel could not settle these findings within the allowed rounds, or reviewers disagree. Approve the design as it is, send it back with direction, or reject the case.</p>' +
          '<div class="summary-box"><ul style="margin:0;padding-left:20px">' + open.map(function (f) {
            return '<li><strong>' + esc(f.id) + '</strong> (' + esc(f.severity) + ') ' + esc(f.issue) + '</li>';
          }).join('') + '</ul></div>'
        : '<p class="muted" style="margin:0">Read the documents below, then decide.</p>' +
          (d.stage_summary ? '<div class="summary-box"><div style="font-weight:600;margin-bottom:4px">Latest agent summary</div>' + esc(d.stage_summary) +
            (c.stage === 'design' ? '<div class="small muted" style="margin-top:6px">' + open.length + ' open finding(s); ' +
              open.filter(function (f) { return f.severity !== 'minor'; }).length + ' blocker or major.</div>' : '') + '</div>' : '');
      var unseen = (d.unseen_at_gate || []).length ? '<div class="notice" style="margin-top:12px">Attached after this stage ran, so its agents have not read: ' +
        esc(d.unseen_at_gate.join(', ')) + '. Send it back if they should take it into account.</div>' : '';
      return '<section class="card action" aria-labelledby="decide-h"><h2 id="decide-h">' +
        (esc8n ? 'Escalated design review' : 'Your decision: ' + esc(gateLabel)) + '</h2>' + body + unseen +
        '<label class="field" for="comment">Comment <span class="muted" style="font-weight:400">(required if you send it back)</span></label>' +
        '<textarea class="input" id="comment" rows="3" placeholder="What should change, and why?" style="margin-bottom:16px"></textarea>' +
        '<div class="btn-row"><button type="button" class="btn primary" data-outcome="approve">' + (esc8n ? 'Approve design' : 'Approve') + '</button>' +
        '<button type="button" class="btn" data-outcome="send_back">Send back</button>' +
        '<button type="button" class="btn danger" data-outcome="reject">Reject case</button></div>' +
        '<div class="error" id="action-error" role="alert"></div></section>';
    }
    if (c.status === 'running' || c.status === 'new') {
      return '<section class="card action"><h2><span class="spinner" aria-hidden="true"></span> Agents are working on the ' + esc(c.stage) + ' stage</h2>' +
        '<p class="muted" style="margin:0">This page refreshes by itself. You will be notified when someone needs to act.</p></section>';
    }
    if (c.status === 'done') {
      return '<section class="card"><h2>Backlog released</h2><p style="margin:0">The approved stories are in the <strong>Backlog</strong> tab' +
        (d.documents.indexOf('06-published.json') >= 0 ? ' and were published to Jira.' : '.') + '</p></section>';
    }
    var last = d.decisions[d.decisions.length - 1];
    if (c.status === 'rejected') {
      var stageLabel = (d.progress.find(function (x) { return x.kind === 'stage' && x.id === d.reopen_stage; }) || {}).label || c.stage;
      return '<section class="card action" aria-labelledby="reopen-h"><h2 id="reopen-h">Rejected</h2><p>' +
        (last ? esc(last.by) + ' rejected this case' + (last.comment ? ': "' + esc(last.comment) + '"' : '.') : 'This case was rejected.') + '</p>' +
        '<label class="field" for="reopen-comment">Reopen with a comment</label>' +
        '<textarea class="input" id="reopen-comment" rows="3" placeholder="What has changed since it was rejected?" style="margin-bottom:8px"></textarea>' +
        '<p class="muted small" style="margin:0 0 16px">Reopening sends the case back to the <strong>' + esc(stageLabel) +
        '</strong> stage. The agents get your comment as input, and the case carries on from there.</p>' +
        '<div class="btn-row"><button type="button" class="btn primary" id="reopen">Reopen case</button></div>' +
        '<div class="error" id="action-error" role="alert"></div></section>';
    }
    return '<section class="card"><h2>Stopped</h2><p style="margin:0">' +
      esc(c.error || 'The case stopped. See the activity log.') + '</p></section>';
  }

  function sideHtml(d) {
    var open = d.findings.filter(function (f) { return f.status === 'open'; });
    var major = open.filter(function (f) { return f.severity !== 'minor'; }).length;
    var findings = d.findings.length
      ? '<div class="btn-row" style="gap:8px;margin-bottom:14px"><span class="pill ' + (major ? 'wait' : 'done') + '">' + major + ' open blocker or major</span>' +
        '<span class="pill grey">' + (open.length - major) + ' minor</span></div><div class="stack" style="gap:12px">' +
        d.findings.map(function (f) {
          var st = f.status === 'open' ? 'Open' : (f.status === 'duplicate' ? 'Duplicate of ' + (f.duplicate_of || '') : 'Closed round ' + (f.closed_round || ''));
          return '<div class="finding"><div class="top"><strong>' + esc(f.id) + ' · ' + esc(f.severity[0].toUpperCase() + f.severity.slice(1)) + '</strong>' +
            '<span class="small" style="font-weight:600;color:' + (f.status === 'open' ? 'var(--wait-text)' : 'var(--ok)') + '">' + esc(st) + '</span></div>' +
            '<div>' + esc(f.issue) + '</div><div class="meta">Evidence: ' + esc(f.evidence) +
            (f.designer_response ? ' · Designer: ' + esc(f.designer_response.replace(/_/g, ' ')) : '') + '</div></div>';
        }).join('') + '</div>'
      : '<p class="muted" style="margin:0">No review findings yet. They appear when the design review starts.</p>';
    var pct = d.budget_usd ? Math.min(100, Math.round(100 * d.cost_usd / d.budget_usd)) : 0;
    var atts = d.attachments || [];
    var attCard = '<section class="card" aria-labelledby="att-h"><h2 id="att-h">Attachments</h2>' +
      (atts.length ? '<ul class="att-list">' + atts.map(function (a) {
        var title = esc(a.id) + ' · ' + esc(a.name);
        var head = a.text_doc && a.status === 'ready' ? '<a href="#" data-att-doc="' + esc(a.text_doc) + '">' + title + '</a>' : '<span>' + title + '</span>';
        var state = a.status === 'pending' ? '<span class="pill wait">' + (isMock() ? 'Uploading' : 'Uploading or scanning') + '</span>'
          : a.status === 'rejected' ? '<span class="pill bad">Turned away</span>' : '';
        var size = a.pages ? a.pages + (a.pages === 1 ? ' page' : ' pages') : sizeText(a.size || 0);
        var seen = a.status !== 'ready' ? '' : (a.seen_by.length ? 'Read by ' + a.seen_by.join(', ') : 'Not read by an agent yet');
        return '<li><div class="row">' + head + state + '</div>' +
          '<div class="muted small">' + esc(a.type) + ', ' + esc(size) + ' · ' + esc(a.added_by || '') + ', during ' + esc(a.added_during || '') + '</div>' +
          (a.status === 'rejected' ? '<div class="small" style="color:var(--bad)">' + esc(a.reason || '') + '</div>' : '') +
          (seen ? '<div class="small">' + esc(seen) + '</div>' : '') +
          (a.note ? '<div class="muted small">' + esc(a.note) + '</div>' : '') + '</li>';
      }).join('') + '</ul>' : '<p class="muted" style="margin:0 0 12px">No files yet. Agents read and cite anything attached here.</p>') +
      (d.can_attach ? dropZoneHtml('case', atts.length ? 'Add more files' : 'Add files') +
        '<div class="btn-row" style="margin-top:8px"><button type="button" class="btn" id="attach-btn">Attach</button>' +
        '<span class="muted small">' + (isMock() ? 'Not scanned locally.' : 'Scanned for malware first.') + ' Stages that run next will read them.</span></div>' : '') +
      '</section>';
    return attCard + '<section class="card" aria-labelledby="find-h"><h2 id="find-h">Review findings</h2>' + findings + '</section>' +
      '<section class="card" aria-labelledby="act-h"><h2 id="act-h">Activity</h2><ol class="activity">' +
      d.activity.slice(0, 14).map(function (a) {
        return '<li><time datetime="' + esc(a.at) + '">' + esc(fmtTime(a.at)) + '</time><span>' + esc(a.text) +
          (a.comment ? '<br><span class="muted">"' + esc(a.comment) + '"</span>' : '') + '</span></li>';
      }).join('') + '</ol></section>' +
      '<section class="card" aria-labelledby="cost-h"><h2 id="cost-h">Spend</h2>' +
      '<div style="display:flex;justify-content:space-between;margin-bottom:6px"><span>' + money(d.cost_usd) + ' used</span><span class="muted">' + money(d.budget_usd) + ' budget</span></div>' +
      '<div class="bar" role="img" aria-label="' + pct + ' percent of budget used"><div style="width:' + pct + '%"></div></div>' +
      '<div class="muted small" style="margin-top:8px">' + d.agent_runs + ' agent runs' + (isMock() ? ' · mock agents, no LLM cost' : '') + '</div>' +
      (d.spend_by_role && d.spend_by_role.length ? '<h3 style="font-size:14px;margin:18px 0 0">Spend by role</h3><ul class="role-spend">' +
        d.spend_by_role.map(function (r) {
          var cap = r.budget_usd ? Math.min(100, Math.round(100 * r.cost_usd / (r.budget_usd * r.runs))) : 0;
          return '<li><div class="row"><span>' + esc(r.label) + (r.limit_hit ? ' <span class="pill cap">hit cap</span>' : '') +
            '<br><span class="muted small">' + r.runs + (r.runs === 1 ? ' run' : ' runs') +
            (r.budget_usd != null ? ', cap ' + money(r.budget_usd) + ' each' : '') + '</span></span>' +
            '<span class="amt">' + money(r.cost_usd) + '</span></div>' +
            '<div class="bar" aria-hidden="true"><div style="width:' + cap + '%"></div></div></li>';
        }).join('') + '</ul><p class="muted small" style="margin:10px 0 0"><a href="#/roles">See every role\'s budget</a></p>' : '') +
      '</section>';
  }

  async function loadDoc(caseId, name) {
    var body = document.getElementById('doc-body');
    document.querySelectorAll('.doc-item[data-doc]').forEach(function (t) {
      if (t.getAttribute('data-doc') === name) { t.setAttribute('aria-current', 'true'); } else { t.removeAttribute('aria-current'); }
    });
    var it = docItems[name] || {};
    var stage = ((d_groups || []).find(function (g) { return g.items.indexOf(it) >= 0; }) || {}).label;
    var dates = it.at ? '<div class="muted small">Created <time datetime="' + esc(it.at) + '">' + esc(fmtDate(it.at)) + '</time>' +
      (it.updated_at && it.updated_at !== it.at ? ' · Updated <time datetime="' + esc(it.updated_at) + '">' + esc(fmtDate(it.updated_at)) + '</time>' : '') + '</div>'
      : '<div class="muted small">Date not recorded (created before IDEA kept document dates)</div>';
    document.getElementById('doc-head').innerHTML = it.label ? '<div><h3>' + esc(it.label) + '</h3><div class="muted small">' +
      esc([it.by, stage ? stage + ' stage' : ''].filter(Boolean).join(' · ')) + '</div>' + dates + '</div>' +
      (it.versions ? '<span class="pill grey">' + (it.versions > 1 ? 'Latest of ' + it.versions + ' versions' : '1 version') + '</span>' : '') : '';
    body.innerHTML = '<p class="muted">Loading…</p>';
    try {
      var doc = await call('/cases/' + encodeURIComponent(caseId) + '?doc=' + encodeURIComponent(name));
      var isAtt = /^attachments\//.test(name);
      var meta = '<div class="doc-meta"><span>' + esc(isAtt ? 'Attachment, extracted text' : name) + '</span>' +
        (isAtt ? '<span class="tag-file">Uploaded file</span>' : (isMock() ? '<span class="tag-mock">Mock data</span>' : '')) + '</div>';
      body.innerHTML = meta + (/\.json$/.test(name) ? backlogHtml(doc.content) : markdown(doc.content));
    } catch (e) {
      body.innerHTML = '<p class="error">' + esc(e.message) + '</p>';
    }
  }

  async function casePage(caseId, keepDoc) {
    setNav('cases');
    var d = await call('/cases/' + encodeURIComponent(caseId));
    var c = d.case;
    var pillText = d.waiting_for;
    d_groups = d.document_groups || [];
    var all = [];
    d_groups.forEach(function (g) { g.items.forEach(function (it) { if (it.name && (it.type === 'document' || it.status === 'ready')) { all.push(it); } }); });
    var latestAgent = all.filter(function (it) { return it.kind === 'agent'; }).sort(function (a, b) { return (a.updated_at || '').localeCompare(b.updated_at || ''); }).pop();
    var names = all.map(function (it) { return it.name; });
    var initial = keepDoc && names.indexOf(keepDoc) >= 0 ? keepDoc : (latestAgent ? latestAgent.name : names[0]);
    app.innerHTML =
      '<div class="crumbs"><a href="#/cases">Cases</a> / ' + esc(c.id) + '</div>' +
      '<div class="page-head"><div style="min-width:0"><h1>' + esc(c.title) + '</h1><div class="muted">Requested by ' + esc(c.requester) +
      ' · started ' + esc(fmtTime(c.created_at)) + ' · cost so far ' + money(d.cost_usd) + ' of ' + money(d.budget_usd) + '</div></div>' +
      '<span class="pill big ' + ({ done: 'done', rejected: 'bad', failed: 'bad', running: 'work', 'new': 'work' }[c.status] || 'wait') + '">' + esc(pillText) + '</span></div>' +
      timelineHtml(d.progress) +
      '<div class="cols"><div class="col-main">' + actionHtml(d) +
      '<section class="card docs" aria-labelledby="docs-h"><div class="docs-head"><h2 id="docs-h">Documents</h2>' +
      '<span class="muted small">By stage, oldest first. Every version is kept.</span></div>' +
      '<div class="doc-pane"><nav class="doc-list" aria-label="Case documents">' + docListHtml(d_groups) + '</nav>' +
      '<div class="doc-view"><div class="doc-head" id="doc-head"></div><div class="doc-body" id="doc-body"></div></div></div>' +
      '<div class="doc-key muted small"><span class="ico person">' + ICONS.person + '</span> Written by people' +
      '<span class="ico agent">' + ICONS.agent + '</span> Written by an agent' +
      '<span class="ico attachment">' + ICONS.attachment + '</span> Attached file</div></section></div>' +
      '<aside class="col-side">' + sideHtml(d) + '</aside></div>';

    app.querySelectorAll('.doc-item[data-doc]').forEach(function (t) {
      t.addEventListener('click', function () { loadDoc(caseId, t.getAttribute('data-doc')); });
    });
    if (initial) { loadDoc(caseId, initial); }

    var err = document.getElementById('action-error');
    var form = document.getElementById('answer-form');
    if (document.getElementById('drop-answers')) { bindDropZone('answers'); }
    if (document.getElementById('drop-case')) {
      bindDropZone('case');
      document.getElementById('attach-btn').addEventListener('click', async function () {
        var b = this;
        var ferr = document.getElementById('ferr-case');
        if (!picked['case'].length) { ferr.textContent = 'Choose files first.'; return; }
        b.disabled = true;
        try {
          var n = await attachTo(caseId, 'case', function (i, total) { b.textContent = 'Uploading ' + i + ' of ' + total + '…'; });
          toast(n === 1 ? 'File attached.' : n + ' files attached.');
          casePage(caseId, initial);
        } catch (ex) {
          ferr.textContent = ex.message;
          b.disabled = false;
          b.textContent = 'Attach';
        }
      });
    }
    app.querySelectorAll('[data-att-doc]').forEach(function (a) {
      a.addEventListener('click', function (e) { e.preventDefault(); loadDoc(caseId, a.getAttribute('data-att-doc')); });
    });
    if (form) {
      form.addEventListener('submit', async function (e) {
        e.preventDefault();
        var parts = [];
        form.querySelectorAll('textarea[data-qid]').forEach(function (t) {
          if (t.value.trim()) { parts.push(t.getAttribute('data-qid') + ': ' + t.value.trim()); }
        });
        if (!parts.length && !picked['answers'].length) { err.textContent = 'Answer at least one question, or attach a document, first.'; return; }
        if (!parts.length) { parts.push('See the attached documents.'); }
        err.textContent = '';
        var btn = document.getElementById('send-answers');
        btn.disabled = true;
        btn.textContent = 'Sending…';
        try {
          await attachTo(caseId, 'answers', function (n, total) { btn.textContent = 'Uploading ' + n + ' of ' + total + '…'; });
          await call('/cases/' + encodeURIComponent(caseId) + '/respond', { method: 'POST', body: { answers: parts.join('\n\n'), by: actingAs() } });
          toast('Answers sent. The agents are carrying on.');
          casePage(caseId);
        } catch (ex) {
          err.textContent = ex.message;
          btn.disabled = false;
          btn.textContent = 'Send answers';
        }
      });
    }
    app.querySelectorAll('[data-outcome]').forEach(function (b) {
      b.addEventListener('click', async function () {
        var outcome = b.getAttribute('data-outcome');
        var comment = document.getElementById('comment').value.trim();
        if (outcome === 'send_back' && !comment) { err.textContent = 'Add a comment saying what should change before sending it back.'; return; }
        if (outcome === 'reject' && !window.confirm('Reject this case? The pipeline stops here.')) { return; }
        err.textContent = '';
        app.querySelectorAll('[data-outcome]').forEach(function (x) { x.disabled = true; });
        try {
          await call('/cases/' + encodeURIComponent(caseId) + '/respond', { method: 'POST', body: { outcome: outcome, comment: comment, by: actingAs() } });
          toast({ approve: 'Approved.', send_back: 'Sent back with your comment.', reject: 'Case rejected.' }[outcome]);
          casePage(caseId);
        } catch (ex) {
          err.textContent = ex.message;
          app.querySelectorAll('[data-outcome]').forEach(function (x) { x.disabled = false; });
        }
      });
    });
    var reopenBtn = document.getElementById('reopen');
    if (reopenBtn) {
      reopenBtn.addEventListener('click', async function () {
        var comment = document.getElementById('reopen-comment').value.trim();
        if (!comment) { err.textContent = 'Say what has changed before reopening the case.'; return; }
        err.textContent = '';
        reopenBtn.disabled = true;
        reopenBtn.textContent = 'Reopening…';
        try {
          await call('/cases/' + encodeURIComponent(caseId) + '/respond', { method: 'POST', body: { outcome: 'reopen', comment: comment, by: actingAs() } });
          toast('Case reopened. The agents are carrying on.');
          casePage(caseId);
        } catch (ex) {
          err.textContent = ex.message;
          reopenBtn.disabled = false;
          reopenBtn.textContent = 'Reopen case';
        }
      });
    }
    refreshWaitingCount();
    if (c.status === 'running' || c.status === 'new' || d.attachments_pending) {
      pollTimer = setTimeout(function () {
        if (location.hash === '#/cases/' + caseId) { casePage(caseId, initial).catch(showError); }
      }, 5000);
    }
  }

  // ------------------------------------------------------------------ roles (read-only)
  var rolesCache = null;
  async function rolesPage() {
    setNav('roles');
    var data = rolesCache || (rolesCache = await call('/roles'));
    var stages = (config.pipeline && config.pipeline.stages) || [];
    var groups = stages.map(function (st) {
      return { stage: st, roles: data.roles.filter(function (r) { return r.stage === st.id; }) };
    }).filter(function (g) { return g.roles.length; });
    var L = data.limits;
    var perRound = data.roles.filter(function (r) { return r.stage === 'design'; })
      .reduce(function (sum, r) { return sum + r.budget_usd; }, 0);
    app.innerHTML =
      '<div class="page-head"><div><h1>Roles</h1><div class="muted">What each agent does, the model it uses and how much it may spend. ' +
      'Read-only: limits are changed in the catalogue and deployed.</div></div></div>' +
      '<div class="limits">' +
      '<div class="kpi strong"><div class="muted small">Budget per case</div><div class="n">' + money(L.case_budget_usd) + '</div><div class="muted small">Hard stop for everything one case spends</div></div>' +
      '<div class="kpi"><div class="muted small">Design review rounds</div><div class="n">' + L.max_review_rounds + '</div><div class="muted small">Up to ' + money(perRound) + ' per round at the role caps</div></div>' +
      '<div class="kpi"><div class="muted small">Retries for unusable output</div><div class="n">' + L.output_retries + '</div><div class="muted small">Never retried when a run hits its cap</div></div>' +
      '</div>' +
      '<div class="stage-wrap">' + groups.map(function (g) {
        return '<section class="stage-group' + (g.roles.length > 1 ? ' wide' : '') + '" aria-labelledby="st-' + esc(g.stage.id) + '"><h2 id="st-' + esc(g.stage.id) + '">' + esc(g.stage.label) +
          (g.stage.gate ? ' <span class="muted small" style="font-weight:400">then ' + esc(g.stage.gate.label) + '</span>' : '') + '</h2>' +
          '<div class="role-grid">' + g.roles.map(roleCard).join('') + '</div></section>';
      }).join('') + '</div>' +
      '<details class="card house"><summary>House rules every agent follows</summary>' + markdown(data.house_rules) + '</details>';
  }
  function roleCard(r) {
    var modelLabel = { opus: 'Claude Opus', sonnet: 'Claude Sonnet', haiku: 'Claude Haiku' }[r.model] || r.model;
    return '<article class="card role-card"><div class="head"><div><h3>' + esc(r.label) + '</h3>' +
      '<div class="part">' + esc(r.part) + (r.finding_prefix ? ' · findings ' + esc(r.finding_prefix) + '-n' : '') + '</div></div>' +
      '<span class="pill ' + (r.model === 'opus' ? 'work' : 'grey') + '">' + esc(modelLabel) + '</span></div>' +
      '<p style="margin:0">' + esc(r.description) + '</p>' +
      '<div class="facts"><div class="fact"><div class="k">Budget per run</div><div class="v">' + money(r.budget_usd) + '</div></div>' +
      '<div class="fact"><div class="k">Max turns</div><div class="v">' + esc(r.max_turns) + '</div></div>' +
      '<div class="fact"><div class="k">Writes</div><div class="v small">' + esc(r.writes || 'Findings only') + '</div></div></div>' +
      '<div><div class="muted small" style="margin-bottom:4px">Tools</div><div class="chips">' +
      r.tools.map(function (t) { return '<span class="chip">' + esc(t) + '</span>'; }).join('') + '</div></div>' +
      (r.reads.length ? '<div><div class="muted small" style="margin-bottom:4px">Reads</div><div class="chips">' +
        r.reads.map(function (t) { return '<span class="chip">' + esc(t) + '</span>'; }).join('') + '</div></div>' : '') +
      (r.specialists.length ? '<div><div class="muted small" style="margin-bottom:4px">Specialist subagents (count against this budget)</div><ul class="specialists">' +
        r.specialists.map(function (sp) { return '<li><strong>' + esc(sp.name) + '</strong>: ' + esc(sp.description) + '</li>'; }).join('') + '</ul></div>' : '') +
      '<details><summary>Full instructions</summary>' + markdown(r.instructions) + '</details></article>';
  }

  // ------------------------------------------------------------------ routing
  async function route() {
    clearTimeout(pollTimer);
    var hash = location.hash || '#/cases';
    var m;
    try {
      if ((m = /^#\/cases\/([A-Za-z0-9-]+)$/.exec(hash))) {
        await casePage(m[1]);
      } else if (hash === '#/new') {
        newPage();
      } else if (hash === '#/roles') {
        await rolesPage();
      } else {
        var f = /[?&]filter=([a-z]+)/.exec(hash);
        await listPage(f ? f[1] : 'all');
      }
      app.focus();
    } catch (e) {
      if (e.message !== 'Signing in…') { showError(e); }
    }
  }

  async function boot() {
    try {
      var resp = await fetch('config.json', { cache: 'no-store' });
      config = await resp.json();
    } catch (e) {
      app.innerHTML = '<div class="card"><h2>Portal not configured</h2><p>config.json could not be loaded.</p></div>';
      return;
    }
    document.getElementById('mock-banner').hidden = !isMock();
    if (config.mode === 'aws') {
      try {
        await finishLogin();
      } catch (e) { showError(e); return; }
      if (!currentToken()) { await login(); return; }
    }
    renderUserArea();
    window.addEventListener('hashchange', route);
    route();
  }

  boot();
})();
