"""Chatter -- the company's Slack-like chat.

Built like a modern SPA: the browser talks to a versioned JSON API using a session
cookie plus a CSRF header. The agent is given *UI credentials only* (no API docs,
no token), so it must learn this app by operating the UI -- and may synthesize an
API tool from the traffic it observes.

Failure-injection flags (sandbox/flags.json):
  chat_ui_variant: 1|2     relabel/re-id the UI (simulates a redesign)
  chat_api_version: 1|2    move the JSON API; the old version answers 410 Gone
  chat_archived: [names]   channels that reject new members
"""
from __future__ import annotations

import json
import secrets

from flask import Flask, abort, jsonify, make_response, redirect, request

from sandbox.common import db, flags, now, rows

app = Flask("chatter")

ROLES = ["member", "people_ops", "admin"]


def init(con):
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, username TEXT UNIQUE, display_name TEXT,
          email TEXT, title TEXT, role TEXT DEFAULT 'member', active INTEGER DEFAULT 1, password TEXT,
          created_at TEXT, last_seen_at TEXT);
        CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY, name TEXT UNIQUE, purpose TEXT, private INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS channel_members(channel_id INTEGER, user_id INTEGER, added_by INTEGER, added_at TEXT,
          PRIMARY KEY(channel_id, user_id));
        CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY, channel_id INTEGER, user_id INTEGER, text TEXT, ts TEXT);
        CREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY, user_id INTEGER, csrf TEXT);
        CREATE TABLE IF NOT EXISTS telemetry(id INTEGER PRIMARY KEY, ts TEXT, user_id INTEGER, event TEXT);
        CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY, ts TEXT, actor TEXT, action TEXT, detail TEXT);
        """
    )


# ---------------------------------------------------------------- auth helpers
def _session(con):
    tok = request.cookies.get("chatter_session")
    if not tok:
        return None
    r = con.execute("SELECT u.*, s.csrf FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token=?", (tok,)).fetchone()
    if r and r["active"]:
        con.execute("UPDATE users SET last_seen_at=? WHERE id=?", (now().isoformat(), r["id"]))
        return dict(r)
    return None


def _api_user(con, write=False):
    u = _session(con)
    if not u:
        abort(401)
    if write:
        if request.headers.get("X-CSRF-Token") != u["csrf"] or request.headers.get("X-Requested-With") != "XMLHttpRequest":
            abort(403, "csrf")
    return u


def _can_manage(u):
    return u["role"] in ("people_ops", "admin")


def _audit(con, actor, action, detail):
    con.execute("INSERT INTO audit(ts,actor,action,detail) VALUES(?,?,?,?)",
                (now().isoformat(), actor, action, json.dumps(detail)))


def _ver():
    return int(flags().get("chat_api_version", 1))


@app.before_request
def version_gate():
    p = request.path
    if p.startswith("/api/v"):
        v = int(p.split("/")[2][1:])
        if v != _ver():
            return jsonify({"error": f"API v{v} has been retired; use v{_ver()}"}), 410


# ---------------------------------------------------------------- JSON API
def _user_json(con, u):
    chans = [r["name"] for r in con.execute(
        "SELECT c.name FROM channel_members m JOIN channels c ON c.id=m.channel_id WHERE m.user_id=? ORDER BY c.name", (u["id"],))]
    return {"id": u["id"], "username": u["username"], "display_name": u["display_name"], "email": u["email"],
            "title": u["title"], "role": u["role"], "active": bool(u["active"]), "channels": chans,
            "created_at": u["created_at"], "last_seen_at": u["last_seen_at"]}


@app.get("/api/v<int:v>/users")
def api_users(v):
    with db("chat") as con:
        _api_user(con)
        q = (request.args.get("q") or "").lower()
        us = rows(con.execute("SELECT * FROM users ORDER BY id"))
        out = [_user_json(con, u) for u in us if not q or q in u["username"].lower() or q in (u["display_name"] or "").lower()]
    return jsonify(out)


@app.get("/api/v<int:v>/users/<username>")
def api_user(v, username):
    with db("chat") as con:
        _api_user(con)
        u = con.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        if not u:
            abort(404)
        return jsonify(_user_json(con, dict(u)))


@app.post("/api/v<int:v>/users")
def api_create_user(v):
    with db("chat") as con:
        me = _api_user(con, write=True)
        if not _can_manage(me):
            abort(403)
        if flags().get("chat_signup_disabled"):
            return jsonify({"error": "account creation is temporarily disabled (seat limit reached)"}), 503
        b = request.get_json(force=True)
        if not b.get("username") or not b.get("display_name"):
            return jsonify({"error": "username and display_name are required"}), 400
        if con.execute("SELECT 1 FROM users WHERE username=?", (b["username"],)).fetchone():
            return jsonify({"error": "username taken"}), 409
        cur = con.execute("INSERT INTO users(username,display_name,email,title,role,active,password,created_at) VALUES(?,?,?,?,?,?,?,?)",
                          (b["username"], b["display_name"], b.get("email"), b.get("title"), "member", 1,
                           secrets.token_hex(8), now().isoformat()))
        general = con.execute("SELECT id FROM channels WHERE name='general'").fetchone()
        if general:  # side effect: every new user lands in #general (a realistic hidden write)
            con.execute("INSERT OR IGNORE INTO channel_members VALUES(?,?,?,?)", (general["id"], cur.lastrowid, me["id"], now().isoformat()))
        _audit(con, me["username"], "user.create", b)
        u = dict(con.execute("SELECT * FROM users WHERE id=?", (cur.lastrowid,)).fetchone())
        return jsonify(_user_json(con, u)), 201


@app.patch("/api/v<int:v>/users/<int:uid>")
def api_patch_user(v, uid):
    with db("chat") as con:
        me = _api_user(con, write=True)
        b = request.get_json(force=True)
        if "role" in b and me["role"] != "admin":
            abort(403, "only workspace admins can change roles")
        if not _can_manage(me):
            abort(403)
        for k in ("display_name", "title", "email", "role"):
            if k in b:
                if k == "role" and b[k] not in ROLES:
                    return jsonify({"error": "bad role"}), 400
                con.execute(f"UPDATE users SET {k}=? WHERE id=?", (b[k], uid))
        _audit(con, me["username"], "user.update", {"id": uid, **b})
        return jsonify(_user_json(con, dict(con.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone())))


@app.post("/api/v<int:v>/users/<int:uid>/<action>")
def api_user_action(v, uid, action):
    if action not in ("deactivate", "reactivate"):
        abort(404)
    with db("chat") as con:
        me = _api_user(con, write=True)
        if not _can_manage(me):
            abort(403)
        con.execute("UPDATE users SET active=? WHERE id=?", (1 if action == "reactivate" else 0, uid))
        if action == "deactivate":
            con.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
        _audit(con, me["username"], f"user.{action}", {"id": uid})
        return jsonify(_user_json(con, dict(con.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone())))


@app.get("/api/v<int:v>/channels")
def api_channels(v):
    with db("chat") as con:
        _api_user(con)
        return jsonify(rows(con.execute("SELECT id, name, purpose, private FROM channels ORDER BY name")))


@app.post("/api/v<int:v>/channels/<int:cid>/members")
def api_add_member(v, cid):
    with db("chat") as con:
        me = _api_user(con, write=True)
        if not _can_manage(me):
            abort(403)
        ch = con.execute("SELECT * FROM channels WHERE id=?", (cid,)).fetchone()
        if not ch:
            abort(404)
        if ch["name"] in flags().get("chat_archived", []):
            return jsonify({"error": f"#{ch['name']} is archived"}), 409
        uid = request.get_json(force=True).get("user_id")
        con.execute("INSERT OR IGNORE INTO channel_members VALUES(?,?,?,?)", (cid, uid, me["id"], now().isoformat()))
        _audit(con, me["username"], "channel.add", {"channel": ch["name"], "user_id": uid})
        return jsonify({"ok": True, "channel_id": cid, "user_id": uid}), 201


@app.delete("/api/v<int:v>/channels/<int:cid>/members/<int:uid>")
def api_remove_member(v, cid, uid):
    with db("chat") as con:
        me = _api_user(con, write=True)
        if not _can_manage(me):
            abort(403)
        con.execute("DELETE FROM channel_members WHERE channel_id=? AND user_id=?", (cid, uid))
        _audit(con, me["username"], "channel.remove", {"channel_id": cid, "user_id": uid})
        return jsonify({"ok": True})


@app.post("/api/v<int:v>/telemetry")
def api_telemetry(v):
    with db("chat") as con:
        u = _session(con)
        con.execute("INSERT INTO telemetry(ts,user_id,event) VALUES(?,?,?)",
                    (now().isoformat(), u["id"] if u else None, request.get_data(as_text=True)[:200]))
    return "", 204


# ---------------------------------------------------------------- UI
LABELS = {
    1: {"people": "People", "invite": "Invite member", "full_name": "Full name", "username": "Username",
        "email": "Email", "title": "Job title", "submit": "Create account", "add_channel": "Add to channel",
        "add_btn": "Add", "remove": "Remove", "deactivate": "Deactivate account", "reactivate": "Reactivate account",
        "make_admin": "Make workspace admin", "search": "Search", "channels": "Channels"},
    2: {"people": "Directory", "invite": "Add a person", "full_name": "Name", "username": "Handle",
        "email": "Work email", "title": "Role / title", "submit": "Send invite", "add_channel": "Join channel",
        "add_btn": "Join", "remove": "Leave", "deactivate": "Suspend user", "reactivate": "Restore user",
        "make_admin": "Grant admin rights", "search": "Find", "channels": "Rooms"},
    3: {"people": "Members", "invite": "Add member", "full_name": "Display name", "username": "User ID",
        "email": "Email address", "title": "Position", "submit": "Save", "add_channel": "Space",
        "add_btn": "Add", "remove": "Remove from", "deactivate": "Disable account", "reactivate": "Enable account",
        "make_admin": "Admin", "search": "Search", "channels": "Spaces"},
}


def _L():
    return LABELS[int(flags().get("chat_ui_variant", 1))]


def _rid(name):  # variant 2 also scrambles element ids so CSS selectors break
    v = int(flags().get("chat_ui_variant", 1))
    return name if v == 1 else f"x{v}{abs(hash(name)) % 9973}"


JS_COMMON = """
const API='/api/v%(ver)d';
function csrf(){return (document.cookie.split('; ').find(c=>c.startsWith('csrf_token='))||'=').split('=')[1];}
async function api(method, path, body){
  const r = await fetch(API+path,{method, headers:{'Content-Type':'application/json','X-CSRF-Token':csrf(),
     'X-Requested-With':'XMLHttpRequest'}, body: body?JSON.stringify(body):undefined});
  let data=null; try{data=await r.json();}catch(e){}
  if(!r.ok){ const m=document.getElementById('flash'); m.textContent='Error: '+((data&&data.error)||r.status); m.setAttribute('role','alert'); throw new Error(r.status);}
  return data;
}
fetch(API+'/telemetry',{method:'POST',body:JSON.stringify({event:'pageview',path:location.pathname})});
"""


# Presentation only: same DOM, text and element order in every UI variant (the explorer's view and
# recorded prompts don't change), just readable for people looking at screenshots.
CSS = """
*{box-sizing:border-box}
body{margin:0;font:15px/1.5 -apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:#1d1c1d;background:#f4f4f6}
nav,aside{display:block;background:#3f0e40;padding:12px 24px;color:#cfc3cf}
nav a{color:#fff;font-weight:600;text-decoration:none;margin:0 6px}
header{background:#350d36;color:#fff;padding:10px 24px;font-size:18px}
#flash{max-width:880px;margin:12px auto 0;padding:0 24px;color:#b42318;font-weight:600}
main{max-width:880px;margin:16px auto 40px;padding:24px 28px;background:#fff;border:1px solid #e4e4e7;border-radius:10px;
     box-shadow:0 1px 3px rgba(0,0,0,.06)}
h1{margin:0 0 16px;font-size:24px}
h2{font-size:17px;margin:24px 0 8px;color:#3f0e40}
a{color:#1264a3}
ul{padding-left:20px}
li{margin:4px 0}
dl{display:grid;grid-template-columns:120px 1fr;gap:6px 16px;margin:0 0 8px}
dt{color:#616061;font-weight:600}
dd{margin:0}
table{border-collapse:collapse;width:100%}
th,td{text-align:left;padding:8px 10px;border-bottom:1px solid #ececef}
th{font-size:13px;color:#616061;letter-spacing:.03em}   /* no text-transform: innerText (what the explorer reads) honours it */
form p,p{margin:10px 0}
label{font-weight:600;margin-right:8px}
input,select{font:inherit;padding:6px 10px;border:1px solid #c9c9cf;border-radius:6px;min-width:220px}
input[type=checkbox]{min-width:0;margin-right:6px}
button{font:inherit;font-weight:600;padding:6px 14px;border-radius:6px;border:1px solid #007a5a;background:#007a5a;color:#fff;
       cursor:pointer;margin-left:6px}
li button{background:#fff;color:#b42318;border-color:#e3b4b0;padding:2px 10px;font-size:13px}
article{padding:10px 0;border-bottom:1px solid #f0f0f2}
time{color:#868686;font-size:12px;margin-left:6px}
"""


def page(title, body, js=""):
    L = _L()
    nav = (f"<nav aria-label='Main'><a href='/'>{L['channels']}</a> | <a href='/people'>{L['people']}</a> | "
           f"<a href='/search'>{L['search']}</a></nav>")
    if int(flags().get("chat_ui_variant", 1)) >= 2:
        nav = f"<header><strong>Chatter</strong></header><aside>{nav}</aside>"
    html = (f"<!doctype html><html><head><title>{title} - Chatter</title><style>{CSS}</style></head><body>{nav}"
            f"<div id='flash'></div><main><h1>{title}</h1>{body}</main>"
            f"<script>{JS_COMMON % {'ver': _ver()}}{js}</script></body></html>")
    return html


def _ui_user():
    with db("chat") as con:
        return _session(con)


@app.get("/login")
def login_form():
    return ("<!doctype html><html><head><title>Sign in - Chatter</title></head><body><h1>Sign in to Chatter</h1>"
            "<form method='post'><label>Username <input name='username'></label>"
            "<label>Password <input name='password' type='password'></label><button type='submit'>Sign in</button></form></body></html>")


@app.post("/login")
def login():
    with db("chat") as con:
        u = con.execute("SELECT * FROM users WHERE username=? AND password=? AND active=1",
                        (request.form.get("username"), request.form.get("password"))).fetchone()
        if not u:
            return "Invalid credentials", 401
        tok, cs = secrets.token_hex(16), secrets.token_hex(12)
        con.execute("INSERT INTO sessions VALUES(?,?,?)", (tok, u["id"], cs))
    resp = make_response(redirect("/"))
    resp.set_cookie("chatter_session", tok, httponly=True, samesite="Lax")
    resp.set_cookie("csrf_token", cs, samesite="Lax")
    return resp


@app.get("/")
def home():
    u = _ui_user()
    if not u:
        return redirect("/login")
    with db("chat") as con:
        chans = rows(con.execute("SELECT name, purpose FROM channels ORDER BY name"))
    lis = "".join(f"<li><a href='/c/{c['name']}'>#{c['name']}</a> {c['purpose'] or ''}</li>" for c in chans)
    return page(_L()["channels"], f"<p>Signed in as {u['display_name']}</p><ul>{lis}</ul>")


@app.get("/c/<name>")
def channel(name):
    if not _ui_user():
        return redirect("/login")
    with db("chat") as con:
        msgs = rows(con.execute("SELECT u.display_name a, m.text, m.ts FROM messages m JOIN channels c ON c.id=m.channel_id "
                                "JOIN users u ON u.id=m.user_id WHERE c.name=? ORDER BY m.ts", (name,)))
    body = "".join(f"<article><b>{m['a']}</b> <time>{m['ts'][:10]}</time><p>{m['text']}</p></article>" for m in msgs)
    return page(f"#{name}", body or "<p>No messages yet.</p>")


@app.get("/search")
def search():
    if not _ui_user():
        return redirect("/login")
    q = request.args.get("q", "")
    res = []
    if q:
        with db("chat") as con:
            res = rows(con.execute("SELECT c.name ch, u.display_name a, m.text, m.ts FROM messages m JOIN channels c ON c.id=m.channel_id "
                                   "JOIN users u ON u.id=m.user_id WHERE lower(m.text) LIKE ? ORDER BY m.ts DESC LIMIT 50",
                                   (f"%{q.lower()}%",)))
    items = "".join(f"<li class='result'><span>#{r['ch']}</span> <b>{r['a']}</b> <time>{r['ts'][:10]}</time>: {r['text']}</li>" for r in res)
    return page(_L()["search"], f"<form><label>Query <input name='q' value='{q}'></label><button>{_L()['search']}</button></form><ul>{items}</ul>")


@app.get("/people")
def people():
    if not _ui_user():
        return redirect("/login")
    L = _L()
    body = (f"<p><a href='/people/new' id='{_rid('invite-link')}'>{L['invite']}</a></p>"
            f"<table id='{_rid('people-table')}'><thead><tr><th>Name</th><th>{L['username']}</th><th>Title</th><th>Status</th></tr></thead>"
            f"<tbody></tbody></table>")
    js = """
    api('GET','/users').then(us=>{const tb=document.querySelector('tbody');
      us.forEach(u=>{const tr=document.createElement('tr');
        tr.innerHTML=`<td><a href="/people/${u.username}">${u.display_name}</a></td><td>${u.username}</td><td>${u.title||''}</td><td>${u.active?'Active':'Deactivated'}</td>`;
        tb.appendChild(tr);});});
    """
    return page(L["people"], body, js)


@app.get("/people/new")
def invite():
    if not _ui_user():
        return redirect("/login")
    L = _L()
    order = ["full_name", "username", "email", "title"]
    if int(flags().get("chat_ui_variant", 1)) == 2:
        order = ["email", "full_name", "title", "username"]
    if int(flags().get("chat_ui_variant", 1)) == 3:
        order = ["title", "username", "full_name", "email"]
    fields = "".join(f"<p><label for='{_rid(f)}'>{L[f]}</label> <input id='{_rid(f)}' name='{_rid(f)}'></p>" for f in order)
    body = f"<form id='{_rid('invite')}'>{fields}<button type='submit'>{L['submit']}</button></form>"
    js = f"""
    document.querySelector('form').addEventListener('submit', async (e)=>{{e.preventDefault();
      const g=id=>document.getElementById(id).value;
      const u = await api('POST','/users',{{display_name:g('{_rid('full_name')}'), username:g('{_rid('username')}'),
                 email:g('{_rid('email')}'), title:g('{_rid('title')}')}});
      location.href='/people/'+u.username;}});
    """
    return page(L["invite"], body, js)


@app.get("/people/<username>")
def person(username):
    if not _ui_user():
        return redirect("/login")
    L = _L()
    body = (f"<dl><dt>Name</dt><dd id='{_rid('name')}'></dd><dt>{L['username']}</dt><dd>{username}</dd>"
            f"<dt>Title</dt><dd id='{_rid('title-v')}'></dd><dt>Email</dt><dd id='{_rid('email-v')}'></dd><dt>Status</dt><dd id='{_rid('status')}'></dd>"
            f"<dt>Role</dt><dd id='{_rid('role')}'></dd></dl>"
            f"<h2>{L['channels']}</h2><ul id='{_rid('chans')}'></ul>"
            f"<p><label for='{_rid('chsel')}'>{L['add_channel']}</label> <select id='{_rid('chsel')}'></select> "
            f"<button id='{_rid('addbtn')}'>{L['add_btn']}</button></p>"
            f"<p><button id='{_rid('toggle')}'></button></p>"
            f"<p><label><input type='checkbox' id='{_rid('admin')}'> {L['make_admin']}</label></p>")
    js = f"""
    let U=null;
    async function load(){{
      U = await api('GET','/users/{username}');
      document.getElementById('{_rid('name')}').textContent=U.display_name;
      document.getElementById('{_rid('title-v')}').textContent=U.title||'';
      document.getElementById('{_rid('email-v')}').textContent=U.email||'';
      document.getElementById('{_rid('status')}').textContent=U.active?'Active':'Deactivated';
      document.getElementById('{_rid('role')}').textContent=U.role;
      document.getElementById('{_rid('admin')}').checked = U.role==='admin';
      const t=document.getElementById('{_rid('toggle')}'); t.textContent=U.active?'{L['deactivate']}':'{L['reactivate']}';
      const chs = await api('GET','/channels'); const ul=document.getElementById('{_rid('chans')}'); ul.innerHTML='';
      const sel=document.getElementById('{_rid('chsel')}'); sel.innerHTML='';
      chs.forEach(c=>{{ if(U.channels.includes(c.name)){{ const li=document.createElement('li');
          li.innerHTML=`#${{c.name}} <button data-cid="${{c.id}}">{L['remove']} #${{c.name}}</button>`; ul.appendChild(li);
          li.querySelector('button').onclick=async()=>{{await api('DELETE',`/channels/${{c.id}}/members/${{U.id}}`); load();}};
        }} else {{ const o=document.createElement('option'); o.value=c.id; o.textContent='#'+c.name; sel.appendChild(o);}} }});
    }}
    document.getElementById('{_rid('addbtn')}').onclick=async()=>{{
      const sel=document.getElementById('{_rid('chsel')}'); await api('POST',`/channels/${{sel.value}}/members`,{{user_id:U.id}}); load();}};
    document.getElementById('{_rid('toggle')}').onclick=async()=>{{
      await api('POST',`/users/${{U.id}}/${{U.active?'deactivate':'reactivate'}}`); load();}};
    document.getElementById('{_rid('admin')}').onchange=async(e)=>{{
      try{{ await api('PATCH',`/users/${{U.id}}`,{{role:e.target.checked?'admin':'member'}}); }} finally {{ load(); }} }};
    load();
    """
    return page(username, body, js)


with db("chat") as _c:
    init(_c)
