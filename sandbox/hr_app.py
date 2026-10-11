"""PeopleHub -- the HR system of record.

Has a *documented* REST API (OpenAPI at /openapi.json, bearer tokens with scopes).
This is the app where the agent's interface ladder should find and use the
documented API directly, without touching a UI.
"""
from __future__ import annotations

import json

from flask import Flask, abort, jsonify, request

from sandbox.common import DOMAIN, db, flags, now, rows

app = Flask("peoplehub")

FIELDS = ["full_name", "preferred_name", "work_email", "personal_email", "title", "team",
          "manager_id", "status", "start_date", "chat_username", "location"]
STATUSES = ["offer_accepted", "onboarding", "active", "terminated"]


def init(con):
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS employees(
          id INTEGER PRIMARY KEY, full_name TEXT, preferred_name TEXT, work_email TEXT,
          personal_email TEXT, title TEXT, team TEXT, manager_id INTEGER, status TEXT,
          start_date TEXT, chat_username TEXT, location TEXT, updated_at TEXT);
        CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY, owner TEXT, scopes TEXT);
        CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY, ts TEXT, actor TEXT, action TEXT, detail TEXT);
        """
    )


def _auth(scope: str) -> str:
    hdr = request.headers.get("Authorization", "")
    if not hdr.startswith("Bearer "):
        abort(401)
    with db("hr") as con:
        r = con.execute("SELECT owner, scopes FROM tokens WHERE token=?", (hdr[7:],)).fetchone()
    if not r:
        abort(401)
    if scope not in r["scopes"].split(","):
        abort(403)
    return r["owner"]


def _audit(con, actor, action, detail):
    con.execute("INSERT INTO audit(ts,actor,action,detail) VALUES(?,?,?,?)",
                (now().isoformat(), actor, action, json.dumps(detail)))


@app.before_request
def outage():
    if flags().get("hr_down"):
        abort(503)


@app.get("/openapi.json")
def openapi():
    emp = {"type": "object", "properties": {f: {"type": "string"} for f in FIELDS}}
    emp["properties"]["manager_id"] = {"type": "integer"}
    emp["properties"]["id"] = {"type": "integer"}
    emp["properties"]["status"] = {"type": "string", "enum": STATUSES}
    return jsonify({
        "openapi": "3.0.3",
        "info": {"title": "PeopleHub API", "version": "1.4"},
        "components": {"securitySchemes": {"bearer": {"type": "http", "scheme": "bearer"}},
                       "schemas": {"Employee": emp}},
        "security": [{"bearer": []}],
        "paths": {
            "/api/employees": {
                "get": {"operationId": "listEmployees", "summary": "List or search employees",
                        "x-entity": "employee", "x-op": "read",
                        "parameters": [{"name": n, "in": "query", "schema": {"type": "string"}}
                                       for n in ["q", "title", "team", "status", "start_after", "start_before"]],
                        "responses": {"200": {"description": "employees"}}},
                "post": {"operationId": "createEmployee", "summary": "Create an employee record",
                         "x-entity": "employee", "x-op": "create",
                         "requestBody": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/Employee"}}}},
                         "responses": {"201": {"description": "created"}}},
            },
            "/api/employees/{id}": {
                "get": {"operationId": "getEmployee", "x-entity": "employee", "x-op": "read",
                        "parameters": [{"name": "id", "in": "path", "required": True, "schema": {"type": "integer"}}],
                        "responses": {"200": {"description": "employee"}}},
                "patch": {"operationId": "updateEmployee", "summary": "Update fields of an employee",
                          "x-entity": "employee", "x-op": "update",
                          "parameters": [{"name": "id", "in": "path", "required": True, "schema": {"type": "integer"}}],
                          "requestBody": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/Employee"}}}},
                          "responses": {"200": {"description": "updated"}}},
            },
            "/api/teams": {"get": {"operationId": "listTeams", "x-entity": "team", "x-op": "read",
                                   "responses": {"200": {"description": "teams"}}}},
        },
    })


@app.get("/api/employees")
def list_employees():
    _auth("employees:read")
    sql, args = "SELECT * FROM employees WHERE 1=1", []
    q = request.args.get("q")
    if q:
        sql += " AND (lower(full_name) LIKE ? OR lower(preferred_name) LIKE ? OR lower(work_email) LIKE ? OR lower(chat_username) LIKE ?)"
        args += [f"%{q.lower()}%"] * 4
    for f in ["title", "team", "status"]:
        if request.args.get(f):
            sql += f" AND lower({f})=?"
            args.append(request.args[f].lower())
    if request.args.get("start_after"):
        sql += " AND start_date>=?"
        args.append(request.args["start_after"])
    if request.args.get("start_before"):
        sql += " AND start_date<=?"
        args.append(request.args["start_before"])
    with db("hr") as con:
        return jsonify(rows(con.execute(sql + " ORDER BY id", args)))


@app.get("/api/employees/<int:eid>")
def get_employee(eid):
    _auth("employees:read")
    with db("hr") as con:
        r = con.execute("SELECT * FROM employees WHERE id=?", (eid,)).fetchone()
    if not r:
        abort(404)
    return jsonify(dict(r))


@app.post("/api/employees")
def create_employee():
    actor = _auth("employees:write")
    body = request.get_json(force=True)
    if not body.get("full_name"):
        return jsonify({"error": "full_name required"}), 400
    vals = {f: body.get(f) for f in FIELDS}
    vals["status"] = vals["status"] or "offer_accepted"
    with db("hr") as con:
        cur = con.execute(f"INSERT INTO employees({','.join(FIELDS)},updated_at) VALUES({','.join('?'*len(FIELDS))},?)",
                          [vals[f] for f in FIELDS] + [now().isoformat()])
        _audit(con, actor, "create", {"id": cur.lastrowid, **vals})
        r = con.execute("SELECT * FROM employees WHERE id=?", (cur.lastrowid,)).fetchone()
    return jsonify(dict(r)), 201


@app.patch("/api/employees/<int:eid>")
def update_employee(eid):
    actor = _auth("employees:write")
    body = request.get_json(force=True)
    bad = [k for k in body if k not in FIELDS]
    if bad:
        return jsonify({"error": f"unknown fields {bad}"}), 400
    if "status" in body and body["status"] not in STATUSES:
        return jsonify({"error": "bad status"}), 400
    with db("hr") as con:
        if not con.execute("SELECT 1 FROM employees WHERE id=?", (eid,)).fetchone():
            abort(404)
        for k, v in body.items():
            con.execute(f"UPDATE employees SET {k}=?, updated_at=? WHERE id=?", (v, now().isoformat(), eid))
        _audit(con, actor, "update", {"id": eid, **body})
        r = con.execute("SELECT * FROM employees WHERE id=?", (eid,)).fetchone()
    return jsonify(dict(r))


@app.get("/api/teams")
def teams():
    _auth("employees:read")
    with db("hr") as con:
        return jsonify([r["team"] for r in con.execute("SELECT DISTINCT team FROM employees WHERE team IS NOT NULL")])


@app.get("/")
def home():
    with db("hr") as con:
        emps = rows(con.execute("SELECT id, full_name, title, team, status, start_date FROM employees ORDER BY id"))
    trs = "".join(f"<tr><td>{e['id']}</td><td>{e['full_name']}</td><td>{e['title']}</td><td>{e['team']}</td>"
                  f"<td>{e['status']}</td><td>{e['start_date']}</td></tr>" for e in emps)
    return (f"<html><head><title>PeopleHub</title></head><body><h1>PeopleHub ({DOMAIN})</h1>"
            f"<p>Read-only view. Integrations use the <a href='/openapi.json'>API</a>.</p>"
            f"<table border=1><tr><th>ID</th><th>Name</th><th>Title</th><th>Team</th><th>Status</th><th>Start</th></tr>{trs}</table></body></html>")


with db("hr") as _c:
    init(_c)
