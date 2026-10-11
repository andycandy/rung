"""Start the whole sandbox company: PeopleHub (HR), Chatter (chat), Mailroom (SMTP + API).

  python -m sandbox.run_all          # foreground
"""
from __future__ import annotations

import logging
import threading
import time

from werkzeug.serving import make_server

from sandbox import chat_app, hr_app, mail_app
from sandbox.common import PORTS


def serve(app, port):
    srv = make_server("127.0.0.1", port, app, threaded=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def start():
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    servers = [serve(hr_app.app, PORTS["hr"]), serve(chat_app.app, PORTS["chat"]), serve(mail_app.app, PORTS["mail"])]
    smtp = mail_app.start_smtp()
    return servers, smtp


if __name__ == "__main__":
    start()
    print("sandbox up: hr :%(hr)d  chat :%(chat)d  mail :%(mail)d  smtp :%(smtp)d" % PORTS, flush=True)
    while True:
        time.sleep(3600)
