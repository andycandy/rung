"""Documents on the company Drive, produced with LibreOffice (a desktop app).

Ladder outcome: discover_interface runs `soffice --help`, finds --headless and
--convert-to, and uses LibreOffice's own CLI as a script rung instead of driving
its GUI. (Honest asymmetry: unlike the web side, nothing is synthesized here --
the "learned" part is discovering and parameterizing an existing interface.)
"""
from __future__ import annotations

import html
import shutil
import subprocess
import tempfile
from pathlib import Path

from rung.apps.base import App, Candidate
from rung.discover import discover_cli
from rung.types import Delta
from sandbox.common import DRIVE


def drive_path(p: str) -> Path:
    q = (DRIVE / p.lstrip("/")).resolve()
    if DRIVE.resolve() not in q.parents and q != DRIVE.resolve():
        raise PermissionError(f"{p} is outside the company drive")
    return q


def pdf_text(p: Path) -> str:
    try:
        return subprocess.run(["pdftotext", str(p), "-"], capture_output=True, text=True, timeout=30).stdout
    except Exception:  # noqa: BLE001
        return ""


class DocsApp(App):
    name = "docs"
    kind = "desktop"
    projection = {"document": {"key": ["path"], "compare": ["exists", "text"]}}

    def probe(self, ctx, entity, key):
        p = drive_path(key["path"])
        if not p.exists():
            return None, "verified"
        text = pdf_text(p) if p.suffix == ".pdf" else p.read_text(errors="ignore")[:20000]
        label = "verified" if text.strip() else "unverified"
        return {"exists": True, "text": text, "bytes": p.stat().st_size}, label

    def read(self, path: str) -> str:
        p = drive_path(path)
        return p.read_text(errors="ignore") if p.exists() else ""

    def listdir(self, path: str) -> list[str]:
        p = drive_path(path)
        return sorted(x.name for x in p.iterdir()) if p.exists() else []

    def render(self, ctx, template: str, fields: dict, out: str, cli: dict):
        src = drive_path(template).read_text()
        for k, v in fields.items():
            src = src.replace("{{" + k + "}}", html.escape(str(v)).replace("\n", "<text:line-break/>"))
        dest = drive_path(out)
        dest.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory() as tmp:
            stem = dest.stem
            (Path(tmp) / f"{stem}.fodt").write_text(src)
            r = subprocess.run([cli["binary"], "--headless", "--convert-to", dest.suffix.lstrip("."), "--outdir", tmp,
                                str(Path(tmp) / f"{stem}.fodt")], capture_output=True, text=True, timeout=120,
                               env={"HOME": tmp, "PATH": "/usr/bin:/bin"})
            produced = Path(tmp) / f"{stem}{dest.suffix}"
            if not produced.exists():
                raise RuntimeError(f"LibreOffice produced nothing: {r.stderr[-300:]}")
            shutil.move(str(produced), dest)
        return {"path": str(out)}

    def candidates(self, ctx, delta: Delta, args: dict):
        if delta.entity != "document":
            return []
        if delta.expect.get("exists") is False:
            return [Candidate("script:fs", lambda _c: drive_path(delta.key["path"]).unlink(missing_ok=True))]
        d = discover_cli(self.name, ["soffice", "libreoffice"])
        ctx.log("discover", {"app": self.name, "found": (d.get("cli") or {}).get("capabilities"), "tried": d["tried"]})
        cli = d.get("cli")
        if cli and "--convert-to" in cli["capabilities"] and args.get("template"):
            return [Candidate("script:soffice", lambda _c: self.render(ctx, args["template"], args.get("fields", {}), delta.key["path"], cli))]
        return []
