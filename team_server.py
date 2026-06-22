"""
team_server.py - Aggregation server for team mode.

Receives metrics-only ingest from many agents (access-key bearer auth), stores
per-user, and serves manager dashboards + an admin access-key management page.

Auth split:
  - /api/ingest is ALWAYS access-key based (agents are machines; no SSO).
  - Manager/admin endpoints use resolve_ui_identity: a bearer access key in
    `local` mode, or a trusted reverse-proxy header in `proxy` mode.

Cost is computed server-side per model via cli.calc_cost (the single Python
pricing source) so the dashboard needs no pricing copy of its own. Figures are
API-equivalent only.

Side effects (network, DB, logging) live here; metric extraction (metrics.py)
and queries (server_db.py) stay pure of the request layer.
"""

import json
import logging
import os
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse

import auth
import metrics
import server_db
from cli import calc_cost

logger = logging.getLogger("claude_usage.team_server")

# Module globals — overridable by serve_team() args/env, and patched by tests.
DB_PATH = server_db.DB_PATH
AUTH_MODE = "local"
SSO_HEADER = auth.DEFAULT_SSO_HEADER

# Reject oversized batches to bound per-request memory.
MAX_BATCH_TURNS = 5000

# Days without a report before a developer is flagged "not reporting" (UI hint).
STALE_DAYS = 7


# ── Cost enrichment (per-model correct) ─────────────────────────────────────────

def _aggregate_models(daily_by_model):
    """Collapse the daily-by-model series into per-model totals with cost."""
    agg = {}
    for d in daily_by_model:
        m = agg.setdefault(d["model"], {
            "model": d["model"], "input": 0, "output": 0,
            "cache_read": 0, "cache_creation": 0, "turns": 0,
        })
        m["input"] += d["input"]
        m["output"] += d["output"]
        m["cache_read"] += d["cache_read"]
        m["cache_creation"] += d["cache_creation"]
        m["turns"] += d["turns"]
    out = []
    for m in agg.values():
        m["cost"] = round(calc_cost(m["model"], m["input"], m["output"],
                                    m["cache_read"], m["cache_creation"]), 4)
        out.append(m)
    out.sort(key=lambda x: x["input"] + x["output"], reverse=True)
    return out


def _user_costs(conn):
    """Per-user API-equivalent cost, attributed per model (never tokens-first)."""
    rows = conn.execute("""
        SELECT user_id,
               COALESCE(NULLIF(model, ''), 'unknown') as model,
               SUM(input_tokens)          as i,
               SUM(output_tokens)         as o,
               SUM(cache_read_tokens)     as cr,
               SUM(cache_creation_tokens) as cc
        FROM server_turns
        GROUP BY user_id, model
    """).fetchall()
    costs = {}
    for r in rows:
        costs[r["user_id"]] = costs.get(r["user_id"], 0.0) + calc_cost(
            r["model"], r["i"] or 0, r["o"] or 0, r["cr"] or 0, r["cc"] or 0)
    return {uid: round(c, 4) for uid, c in costs.items()}


def _team_payload(conn):
    data = server_db.get_team_data(conn)
    data["by_model"] = _aggregate_models(data["daily_by_model"])
    data["totals"]["cost"] = round(sum(m["cost"] for m in data["by_model"]), 4)
    costs = _user_costs(conn)
    for u in data["users"]:
        u["cost"] = costs.get(u["user_id"], 0.0)
    data["stale_days"] = STALE_DAYS
    return data


def _enrich_detail(detail):
    detail["by_model"] = _aggregate_models(detail["daily_by_model"])
    detail["totals"]["cost"] = round(sum(m["cost"] for m in detail["by_model"]), 4)
    return detail


# ── HTTP handler ────────────────────────────────────────────────────────────────

class TeamServerHandler(BaseHTTPRequestHandler):
    # Quiet the default per-request stderr logging; we log meaningful events.
    def log_message(self, fmt, *args):
        pass

    # -- response helpers --
    def _send_json(self, code, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html):
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length <= 0:
            return None
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None

    def _ui_cfg(self):
        return {"auth_mode": AUTH_MODE, "sso_header": SSO_HEADER}

    def _admin_conn(self):
        """Return (conn, identity) for an admin requester, else send the error
        response and return (None, None)."""
        conn = server_db.get_conn(DB_PATH)
        identity = auth.resolve_ui_identity(conn, self.headers, self._ui_cfg())
        if identity is None:
            conn.close()
            self._send_json(401, {"error": "authentication required"})
            return None, None
        if not identity.get("is_admin"):
            conn.close()
            logger.warning("admin endpoint denied for %s", identity.get("email"))
            self._send_json(403, {"error": "admin access required"})
            return None, None
        return conn, identity

    # -- routing --
    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            self._send_html(DASHBOARD_HTML)
        elif path == "/admin/keys":
            self._send_html(KEYS_HTML)
        elif path == "/api/team/data":
            self._handle_team_data()
        elif path.startswith("/api/team/user/"):
            self._handle_user_detail(path.rsplit("/", 1)[-1])
        elif path == "/api/admin/keys":
            self._handle_list_keys()
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/ingest":
            self._handle_ingest()
        elif path == "/api/admin/keys":
            self._handle_create_keys()
        elif path.startswith("/api/admin/keys/") and path.endswith("/revoke"):
            self._handle_revoke(path[len("/api/admin/keys/"):-len("/revoke")])
        else:
            self._send_json(404, {"error": "not found"})

    # -- ingest (access-key auth) --
    def _handle_ingest(self):
        conn = server_db.get_conn(DB_PATH)
        try:
            identity = auth.authenticate_bearer(conn, self.headers)
            if identity is None:
                logger.warning("ingest auth failure from %s", self.client_address[0])
                self._send_json(401, {"error": "invalid or revoked access key"})
                return

            body = self._read_json()
            if body is None:
                self._send_json(400, {"error": "invalid or empty JSON body"})
                return

            declared = (body.get("email") or "").strip().lower()
            if not declared:
                self._send_json(400, {"error": "email is required"})
                return
            if declared != identity["email"]:
                logger.warning("ingest email mismatch key_id=%s declared=%s canonical=%s",
                               identity["key_id"], declared, identity["email"])
                self._send_json(401, {"error": "declared email does not match access key"})
                return

            turns = body.get("turns", [])
            if not isinstance(turns, list):
                self._send_json(400, {"error": "turns must be a list"})
                return
            if len(turns) > MAX_BATCH_TURNS:
                self._send_json(413, {"error": "batch too large"})
                return

            try:
                metrics.assert_no_forbidden_fields(body)
            except metrics.ForbiddenFieldError as exc:
                logger.warning("ingest rejected key_id=%s: %s", identity["key_id"], exc)
                self._send_json(400, {"error": "forbidden fields in payload"})
                return

            result = server_db.upsert_turn_metrics(
                conn, identity["user_id"], body.get("install_uuid"), turns)
            auth.touch_last_seen(conn, identity["key_id"])
            logger.info("ingest user=%s received=%d inserted=%d deduped=%d",
                        identity["email"], result["received"],
                        result["inserted"], result["deduped"])
            self._send_json(200, result)
        finally:
            conn.close()

    # -- manager endpoints (admin auth) --
    def _handle_team_data(self):
        conn, _ = self._admin_conn()
        if conn is None:
            return
        try:
            self._send_json(200, _team_payload(conn))
        finally:
            conn.close()

    def _handle_user_detail(self, raw_id):
        conn, _ = self._admin_conn()
        if conn is None:
            return
        try:
            try:
                user_id = int(raw_id)
            except (TypeError, ValueError):
                self._send_json(404, {"error": "user not found"})
                return
            detail = server_db.get_user_detail(conn, user_id)
            if detail is None:
                self._send_json(404, {"error": "user not found"})
                return
            self._send_json(200, _enrich_detail(detail))
        finally:
            conn.close()

    # -- admin key management (admin auth) --
    def _handle_list_keys(self):
        conn, _ = self._admin_conn()
        if conn is None:
            return
        try:
            self._send_json(200, {"keys": auth.list_keys(conn)})
        finally:
            conn.close()

    def _handle_create_keys(self):
        conn, identity = self._admin_conn()
        if conn is None:
            return
        try:
            body = self._read_json() or {}
            emails = body.get("emails") or []
            if not emails:
                self._send_json(400, {"error": "no emails provided"})
                return
            created = auth.create_keys(
                conn, emails, label=body.get("label"), is_admin=bool(body.get("is_admin")))
            logger.info("admin %s created %d access key(s)", identity["email"], len(created))
            self._send_json(200, {"created": created})
        finally:
            conn.close()

    def _handle_revoke(self, raw_id):
        conn, identity = self._admin_conn()
        if conn is None:
            return
        try:
            try:
                key_id = int(raw_id)
            except (TypeError, ValueError):
                self._send_json(404, {"error": "key not found"})
                return
            auth.revoke_key(conn, key_id)
            logger.info("admin %s revoked key_id=%s", identity["email"], key_id)
            self._send_json(200, {"revoked": key_id})
        finally:
            conn.close()


def serve_team(host=None, port=None, db_path=None, auth_mode=None, sso_header=None,
               admin_key=None, admin_emails=None):
    """Initialise the server DB, bootstrap admin access, and serve forever."""
    global DB_PATH, AUTH_MODE, SSO_HEADER
    if db_path:
        DB_PATH = Path(db_path)
    AUTH_MODE = (auth_mode or os.environ.get("CLAUDE_USAGE_AUTH_MODE", "local")).lower()
    SSO_HEADER = sso_header or os.environ.get("CLAUDE_USAGE_SSO_HEADER", auth.DEFAULT_SSO_HEADER)

    admin_key = admin_key or os.environ.get("CLAUDE_USAGE_ADMIN_KEY")
    if admin_emails is None:
        env_emails = os.environ.get("CLAUDE_USAGE_ADMIN_EMAILS", "")
        admin_emails = [e.strip() for e in env_emails.split(",") if e.strip()]

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    conn = server_db.get_conn(DB_PATH)
    server_db.init_server_db(conn)
    auth.bootstrap_admin(conn, admin_key=admin_key, admin_emails=admin_emails)
    conn.close()

    host = host or os.environ.get("HOST", "localhost")
    port = int(port or os.environ.get("PORT", "8080"))
    server = ThreadingHTTPServer((host, port), TeamServerHandler)
    logger.info("Team server on http://%s:%d (auth_mode=%s)", host, port, AUTH_MODE)
    if AUTH_MODE == "local" and not admin_key:
        logger.warning("No CLAUDE_USAGE_ADMIN_KEY set — admin endpoints are unreachable. "
                       "Set it to bootstrap an admin access key.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Stopped.")


# ── Embedded UI ─────────────────────────────────────────────────────────────────

_COMMON_HEAD = r"""
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  :root { --bg:#1b1a17; --card:#26241f; --fg:#ece7df; --muted:#a59e90;
          --accent:#d98a4b; --line:#3a372f; }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--fg);
         font:14px/1.5 -apple-system,Segoe UI,Roboto,sans-serif; }
  header { padding:18px 24px; border-bottom:1px solid var(--line);
           display:flex; justify-content:space-between; align-items:center; }
  h1 { font-size:18px; margin:0; }
  a { color:var(--accent); text-decoration:none; }
  main { padding:24px; max-width:1200px; margin:0 auto; }
  .cards { display:grid; grid-template-columns:repeat(auto-fit,minmax(170px,1fr));
           gap:14px; margin-bottom:24px; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:10px;
          padding:16px; }
  .card .k { color:var(--muted); font-size:12px; text-transform:uppercase;
             letter-spacing:.04em; }
  .card .v { font-size:24px; font-weight:600; margin-top:4px; }
  section { background:var(--card); border:1px solid var(--line); border-radius:10px;
            padding:16px; margin-bottom:20px; }
  section h2 { font-size:14px; margin:0 0 12px; color:var(--muted);
               text-transform:uppercase; letter-spacing:.04em; }
  table { width:100%; border-collapse:collapse; }
  th,td { text-align:left; padding:7px 10px; border-bottom:1px solid var(--line); }
  th { color:var(--muted); font-weight:500; font-size:12px; }
  tr.click { cursor:pointer; } tr.click:hover { background:#2f2c25; }
  .pill { padding:2px 8px; border-radius:10px; font-size:11px; }
  .ok { background:#2e4d36; color:#bfe6c8; } .stale { background:#5a3030; color:#f0c4c4; }
  .muted { color:var(--muted); }
  input,button,textarea { font:inherit; background:#211f1b; color:var(--fg);
          border:1px solid var(--line); border-radius:7px; padding:8px 10px; }
  button { background:var(--accent); color:#1b1a17; border:none; cursor:pointer;
           font-weight:600; }
  .grid2 { display:grid; grid-template-columns:1fr 1fr; gap:20px; }
  @media(max-width:720px){ .grid2{grid-template-columns:1fr;} }
  code.key { background:#211f1b; padding:3px 6px; border-radius:5px; }
</style>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<script>
// Shared auth: in local mode we send the admin access key as a bearer token;
// in proxy mode the reverse proxy injects identity and no key is needed.
function getKey(){ return localStorage.getItem('clu_admin_key') || ''; }
async function authFetch(url, opts){
  opts = opts || {};
  opts.headers = opts.headers || {};
  const k = getKey();
  if (k) opts.headers['Authorization'] = 'Bearer ' + k;
  let r = await fetch(url, opts);
  if (r.status === 401){
    const entered = prompt('Admin access key (clu_...):');
    if (entered){ localStorage.setItem('clu_admin_key', entered);
      opts.headers['Authorization'] = 'Bearer ' + entered; r = await fetch(url, opts); }
  }
  return r;
}
function fmt(n){ n = n||0; if(n>=1e6) return (n/1e6).toFixed(2)+'M';
  if(n>=1e3) return (n/1e3).toFixed(1)+'K'; return ''+n; }
function fmtCost(c){ return '$' + (c||0).toFixed(2); }
</script>
"""

DASHBOARD_HTML = r"""<!doctype html><html><head><title>Team usage</title>""" + _COMMON_HEAD + r"""
</head><body>
<header><h1>Claude Code — Team usage</h1>
  <a href="/admin/keys">Manage access keys &rarr;</a></header>
<main>
  <div class="cards" id="cards"></div>
  <section><h2>Not reporting (no data in last <span id="staledays"></span> days)</h2>
    <div id="notreporting" class="muted">—</div></section>
  <div class="grid2">
    <section><h2>Daily tokens</h2><canvas id="daily" height="180"></canvas></section>
    <section><h2>Model mix (by tokens)</h2><canvas id="models" height="180"></canvas></section>
  </div>
  <div class="grid2">
    <section><h2>Activity by hour (UTC)</h2><canvas id="hours" height="180"></canvas></section>
    <section><h2>Top tools</h2><table id="tools"></table></section>
  </div>
  <div class="grid2">
    <section><h2>MCP servers</h2><table id="mcp"></table></section>
    <section><h2>Projects</h2><table id="projects"></table></section>
  </div>
  <section><h2>Developers</h2><table id="leaderboard"></table></section>
  <section id="detail" style="display:none"><h2 id="detailtitle"></h2>
    <div id="detailbody"></div></section>
</main>
<script>
let charts = {};
function card(k,v){ return '<div class="card"><div class="k">'+k+'</div><div class="v">'+v+'</div></div>'; }
function daysSince(iso){ if(!iso) return Infinity;
  return (Date.now() - Date.parse(iso.replace(' ','T'))) / 86400000; }

async function load(){
  const r = await authFetch('/api/team/data');
  if(!r.ok){ document.getElementById('cards').innerHTML =
    '<div class="card"><div class="k">Error</div><div class="v">'+r.status+'</div></div>'; return; }
  const d = await r.json();
  const stale = d.stale_days || 7;
  document.getElementById('staledays').textContent = stale;

  const active = d.enrollment.filter(e => daysSince(e.last_seen_at) <= stale).length;
  document.getElementById('cards').innerHTML =
    card('Enrolled', d.enrollment.length) +
    card('Active ('+stale+'d)', active) +
    card('Total tokens', fmt(d.totals.input + d.totals.output)) +
    card('Est. cost', fmtCost(d.totals.cost)) +
    card('Sessions', fmt(d.totals.sessions));

  const nr = d.enrollment.filter(e => daysSince(e.last_seen_at) > stale);
  document.getElementById('notreporting').innerHTML = nr.length
    ? nr.map(e => '<span class="pill stale">'+e.email+
        (e.last_seen_at ? ' · '+e.last_seen_at.slice(0,10) : ' · never')+'</span>').join(' ')
    : '<span class="muted">Everyone has reported recently.</span>';

  // Daily tokens
  const byDay = {};
  d.daily_by_model.forEach(x => { byDay[x.day] = (byDay[x.day]||0) + x.input + x.output; });
  const days = Object.keys(byDay).sort();
  drawBar('daily', days, days.map(k=>byDay[k]), 'Tokens');

  // Model mix doughnut
  drawDoughnut('models', d.by_model.map(m=>m.model),
    d.by_model.map(m=>m.input + m.output));

  // Activity by hour
  const byHour = new Array(24).fill(0);
  d.hourly_by_model.forEach(x => { byHour[x.hour] = (byHour[x.hour]||0) + x.turns; });
  drawBar('hours', byHour.map((_,i)=>i), byHour, 'Turns');

  tableRows('tools', ['Tool','Turns'], d.tool_freq.slice(0,12).map(t=>[t.tool_name, fmt(t.turns)]));
  tableRows('mcp', ['Server','Turns','Devs'], d.mcp_servers.map(m=>[m.server, fmt(m.turns), m.users]));
  tableRows('projects', ['Project','Turns','Devs','Tokens'],
    d.projects.slice(0,15).map(p=>[p.project_name, fmt(p.turns), p.users, fmt(p.input+p.output)]));

  const lb = document.getElementById('leaderboard');
  lb.innerHTML = '<tr><th>Developer</th><th>Turns</th><th>Sessions</th><th>Tokens</th><th>Est. cost</th><th>Last seen</th></tr>';
  const seen = {}; d.enrollment.forEach(e => seen[e.email] = e.last_seen_at);
  d.users.forEach(u => {
    const tr = document.createElement('tr'); tr.className = 'click';
    tr.onclick = () => loadUser(u.user_id, u.email);
    tr.innerHTML = '<td>'+u.email+'</td><td>'+fmt(u.turns)+'</td><td>'+fmt(u.sessions)+
      '</td><td>'+fmt(u.input+u.output)+'</td><td>'+fmtCost(u.cost)+'</td><td class="muted">'+
      ((seen[u.email]||'').slice(0,10) || '—')+'</td>';
    lb.appendChild(tr);
  });
}

async function loadUser(id, email){
  const r = await authFetch('/api/team/user/'+id); if(!r.ok) return;
  const d = await r.json();
  document.getElementById('detail').style.display = 'block';
  document.getElementById('detailtitle').textContent = 'Developer · ' + email;
  const tools = d.tool_freq.slice(0,8).map(t=>t.tool_name+' ('+t.turns+')').join(', ') || '—';
  const projs = d.projects.slice(0,8).map(p=>p.project_name+' ('+fmt(p.input+p.output)+')').join(', ') || '—';
  const models = d.by_model.map(m=>m.model+' '+fmtCost(m.cost)).join(', ') || '—';
  document.getElementById('detailbody').innerHTML =
    '<div class="cards">' + card('Turns', fmt(d.totals.turns)) +
      card('Sessions', fmt(d.totals.sessions)) +
      card('Tokens', fmt(d.totals.input + d.totals.output)) +
      card('Est. cost', fmtCost(d.totals.cost)) +
      card('Active days', d.totals.active_days) + '</div>' +
    '<p><b>Models:</b> '+models+'</p><p><b>Top tools:</b> '+tools+
    '</p><p><b>Projects:</b> '+projs+'</p>';
  document.getElementById('detail').scrollIntoView({behavior:'smooth'});
}

function tableRows(id, headers, rows){
  const t = document.getElementById(id);
  let h = '<tr>' + headers.map(x=>'<th>'+x+'</th>').join('') + '</tr>';
  h += rows.map(r => '<tr>'+r.map(c=>'<td>'+c+'</td>').join('')+'</tr>').join('');
  t.innerHTML = h || '<tr><td class="muted">No data</td></tr>';
}
function drawBar(id, labels, data, label){
  if(charts[id]) charts[id].destroy();
  charts[id] = new Chart(document.getElementById(id), { type:'bar',
    data:{ labels, datasets:[{ label, data, backgroundColor:'#d98a4b' }] },
    options:{ plugins:{legend:{display:false}}, scales:{x:{ticks:{color:'#a59e90'}},
      y:{ticks:{color:'#a59e90'}}} } });
}
function drawDoughnut(id, labels, data){
  if(charts[id]) charts[id].destroy();
  charts[id] = new Chart(document.getElementById(id), { type:'doughnut',
    data:{ labels, datasets:[{ data, backgroundColor:['#d98a4b','#7ba05b','#5b8aa0','#a05b8a','#8a8a5b','#888'] }] },
    options:{ plugins:{legend:{labels:{color:'#ece7df'}}} } });
}
load();
</script>
</body></html>"""

KEYS_HTML = r"""<!doctype html><html><head><title>Access keys</title>""" + _COMMON_HEAD + r"""
</head><body>
<header><h1>Claude Code — Access keys</h1><a href="/">&larr; Dashboard</a></header>
<main>
  <section><h2>Generate access keys</h2>
    <p class="muted">One key per email. The raw key is shown once — copy it now.</p>
    <textarea id="emails" rows="3" style="width:100%"
      placeholder="alice@example.com, bob@example.com"></textarea>
    <div style="margin-top:10px;display:flex;gap:10px;align-items:center">
      <input id="label" placeholder="label (optional)">
      <label class="muted"><input type="checkbox" id="isadmin"> admin</label>
      <button onclick="generate()">Generate</button>
    </div>
    <div id="generated"></div>
  </section>
  <section><h2>Existing keys</h2><table id="keys"></table></section>
</main>
<script>
async function generate(){
  const raw = document.getElementById('emails').value;
  const emails = raw.split(/[\s,]+/).map(s=>s.trim()).filter(Boolean);
  if(!emails.length){ alert('Enter at least one email'); return; }
  const r = await authFetch('/api/admin/keys', { method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({ emails, label: document.getElementById('label').value || null,
      is_admin: document.getElementById('isadmin').checked }) });
  if(!r.ok){ alert('Error: ' + r.status); return; }
  const d = await r.json();
  document.getElementById('generated').innerHTML =
    '<table><tr><th>Email</th><th>Access key (copy now)</th></tr>' +
    d.created.map(c => '<tr><td>'+c.email+'</td><td><code class="key">'+c.key+
      '</code></td></tr>').join('') + '</table>';
  load();
}
async function revoke(id){
  if(!confirm('Revoke this key?')) return;
  const r = await authFetch('/api/admin/keys/'+id+'/revoke', { method:'POST' });
  if(r.ok) load(); else alert('Error: ' + r.status);
}
async function load(){
  const r = await authFetch('/api/admin/keys'); if(!r.ok){ return; }
  const d = await r.json();
  const t = document.getElementById('keys');
  let h = '<tr><th>Email</th><th>Label</th><th>Created</th><th>Last seen</th><th>Status</th><th></th></tr>';
  h += d.keys.map(k => '<tr><td>'+k.email+'</td><td>'+(k.label||'')+'</td><td class="muted">'+
    (k.created_at||'').slice(0,10)+'</td><td class="muted">'+((k.last_seen_at||'').slice(0,10)||'—')+
    '</td><td><span class="pill '+(k.status==='active'?'ok':'stale')+'">'+k.status+'</span></td>'+
    '<td>'+(k.status==='active'?'<button onclick="revoke('+k.key_id+')">Revoke</button>':'')+
    '</td></tr>').join('');
  t.innerHTML = h;
}
load();
</script>
</body></html>"""
