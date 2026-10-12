"""Record the demo: fresh sandbox, viewer up, a headless browser films the viewer while the story runs.

  RUNG_MODEL=nim NVIDIA_API_KEY=... python scripts/demo_video/record.py [out_dir]

The sandbox must be running. Writes <out_dir>/rung-demo.webm (default var/demo_video/).
"""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "var" / "demo_video"
os.environ.setdefault("RUNG_DEMO_FRAMES", str(ROOT / "var" / "demo"))


def wait_port(port, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.3)
    return False


def main():
    from playwright.sync_api import sync_playwright
    subprocess.run([sys.executable, str(HERE / "story.py"), "--setup"], check=True, cwd=ROOT)
    viewer = subprocess.Popen([sys.executable, str(HERE / "viewer.py")], cwd=ROOT,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        if not wait_port(7190):
            sys.exit("viewer did not start")
        shutil.rmtree(OUT, ignore_errors=True)
        OUT.mkdir(parents=True)
        with sync_playwright() as p:
            browser = p.chromium.launch()
            ctx = browser.new_context(viewport={"width": 1600, "height": 900}, record_video_dir=str(OUT),
                                      record_video_size={"width": 1600, "height": 900})
            page = ctx.new_page()
            page.goto(f"http://127.0.0.1:7190/?speed={os.environ.get('DEMO_SPEED', '2')}")
            page.wait_for_timeout(1500)
            # every model call of the live take is recorded, so the video can be re-rendered by replay if needed
            env = {**os.environ, "RUNG_RECORD": os.environ.get("RUNG_RECORD", str(OUT / "demo_run.jsonl"))}
            story = subprocess.run([sys.executable, str(HERE / "story.py")], cwd=ROOT, env=env)
            page.wait_for_timeout(6000)
            video = page.video.path()
            ctx.close()
            browser.close()
        final = OUT / "rung-demo.webm"
        Path(video).rename(final)
        print(f"story exit {story.returncode}; video: {final} ({final.stat().st_size // 1024} KB)")
    finally:
        viewer.terminate()


if __name__ == "__main__":
    main()
