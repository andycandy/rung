"""Headless browser with traffic capture.

Every JSON API call the web app makes while the agent operates its UI is captured
(method, URL, body, status, response). Those recordings are what the promoter
turns into direct API tools after a verified success.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

SNAPSHOT_JS = r"""
() => {
  let n = 0; const out = [];
  const vis = e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0 && getComputedStyle(e).visibility !== 'hidden'; };
  const labelOf = e => {
    if (e.id) { const l = document.querySelector(`label[for="${e.id}"]`); if (l) return l.innerText.trim(); }
    const p = e.closest('label'); if (p) return p.innerText.trim();
    if (e.getAttribute('aria-label')) return e.getAttribute('aria-label');
    if (e.placeholder) return e.placeholder;
    const prev = e.previousElementSibling; if (prev && prev.tagName === 'LABEL') return prev.innerText.trim();
    return e.name || '';
  };
  document.querySelectorAll('a[href], button, input, select, textarea').forEach(e => {
    if (!vis(e) || e.type === 'hidden') return;
    const id = String(++n); e.setAttribute('data-rung-id', id);
    const t = e.tagName.toLowerCase();
    const item = {id, tag: t, name: e.name || '', form: e.form ? [...document.forms].indexOf(e.form) : -1};
    if (t === 'a') { item.text = e.innerText.trim(); item.href = e.getAttribute('href'); }
    else if (t === 'button') { item.text = e.innerText.trim(); item.context = (e.closest('li,p,form,tr')||e.parentElement).innerText.trim().slice(0,120); }
    else if (t === 'select') { item.label = labelOf(e); item.options = [...e.options].map(o => o.textContent.trim()); }
    else { item.type = e.type || 'text'; item.label = labelOf(e); item.value = e.type==='checkbox' ? e.checked : e.value;
           if (e.type === 'submit' || e.type === 'button') item.text = e.value; }
    out.push(item);
  });
  const text = (document.querySelector('main')||document.body).innerText.slice(0, 1500);
  const forms = [...document.forms].map(f => (f.getAttribute('action') || location.pathname));
  return {url: location.pathname + location.search, title: document.title, elements: out, text, forms,
          alert: (document.getElementById('flash')||{}).innerText || ''};
}
"""


class AppBrowser:
    def __init__(self, ctx, base_url: str, shots: Path):
        self.ctx, self.base, self.shots = ctx, base_url.rstrip("/"), shots
        self.page = ctx.new_page()
        self.page.set_default_timeout(10000)   # a missing element should cost seconds, not half a minute
        self.probe_page = None
        self.recorders: dict = {}          # page -> (tag, list) ; probes record separately from exploration
        ctx.route("**/api/**", self._route)

    def get_probe_page(self):
        if self.probe_page is None:
            self.probe_page = self.ctx.new_page()
        return self.probe_page

    # ---------------------------------------------------------------- capture
    def _route(self, route):
        req = route.request
        try:
            resp = route.fetch()
            body = resp.body()
        except Exception:  # noqa: BLE001 -- page navigated away; let the request go
            route.continue_()
            return
        try:
            page = req.frame.page
        except Exception:  # noqa: BLE001
            page = self.page
        rec = self.recorders.get(page)
        if rec is not None:
            try:
                data = json.loads(body) if body else None
            except Exception:  # noqa: BLE001
                data = None
            try:
                post = req.post_data_json if req.post_data else None
            except Exception:  # noqa: BLE001
                post = req.post_data
            rec[1].append({"tag": rec[0], "t": time.time(), "method": req.method,
                                   "url": req.url.replace(self.base, ""), "headers": dict(req.headers),
                                   "body": post, "status": resp.status, "response": data})
        route.fulfill(response=resp, body=body)

    def start_recording(self, tag: str, page=None):
        self.recorders[page or self.page] = (tag, [])

    def stop_recording(self, page=None) -> list[dict]:
        rec = self.recorders.pop(page or self.page, None)
        return rec[1] if rec else []

    # ---------------------------------------------------------------- actions
    def frame(self):
        """Demo only (RUNG_DEMO_FRAMES=<dir>): the latest view of the page being operated, for a live viewer."""
        d = os.environ.get("RUNG_DEMO_FRAMES")
        if not d:
            return
        try:
            out = Path(d)
            out.mkdir(parents=True, exist_ok=True)
            self.page.screenshot(path=str(out / "frame.tmp.jpg"), type="jpeg", quality=80)
            os.replace(out / "frame.tmp.jpg", out / "frame.jpg")
            (out / "frame.json").write_text(json.dumps({"url": self.page.url, "t": time.time()}))
        except Exception:  # noqa: BLE001 -- a demo frame must never break a run
            pass

    def goto(self, path: str):
        self.page.goto(self.base + path if path.startswith("/") else path, wait_until="networkidle")
        self.frame()

    def settle(self):
        try:
            self.page.wait_for_load_state("networkidle", timeout=5000)
        except Exception:  # noqa: BLE001
            pass
        self.page.wait_for_timeout(150)

    def snapshot(self) -> dict:
        return self.page.evaluate(SNAPSHOT_JS)

    def el(self, rid: str):
        return self.page.locator(f'[data-rung-id="{rid}"]')

    def click(self, rid):
        self.el(rid).click()
        self.settle()
        self.frame()

    def fill(self, rid, value):
        self.el(rid).fill(str(value))
        self.frame()

    def select(self, rid, label):
        self.el(rid).select_option(label=label)
        self.frame()

    def check(self, rid, on=True):
        (self.el(rid).check if on else self.el(rid).uncheck)()
        self.settle()
        self.frame()

    def screenshot(self, name: str) -> str:
        p = self.shots / f"{name}.png"
        self.page.screenshot(path=str(p))
        return str(p)

    def cookies(self) -> dict:
        return {c["name"]: c["value"] for c in self.ctx.cookies()}


class Browser:
    """One headless Chromium, one context per app (separate cookie jars)."""

    def __init__(self, shots: Path):
        self.shots = shots
        self._pw = None
        self._browser = None
        self.apps: dict[str, AppBrowser] = {}

    def app(self, name: str, base_url: str) -> AppBrowser:
        if name not in self.apps:
            if not self._browser:
                self._pw = sync_playwright().start()
                self._browser = self._pw.chromium.launch(headless=True)
            self.apps[name] = AppBrowser(self._browser.new_context(), base_url, self.shots)
        return self.apps[name]

    def close(self):
        if self._browser:
            self._browser.close()
            self._pw.stop()
        self._browser = None
        self.apps = {}
