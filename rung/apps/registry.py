"""App registry. Adding an app = adding an adapter module here; core never imports app code."""
from __future__ import annotations

_APPS = {}


def get(name: str):
    if not _APPS:
        from rung.apps.chat import ChatApp
        from rung.apps.docs import DocsApp
        from rung.apps.hr import HRApp
        from rung.apps.mail import MailApp
        from rung.apps.sso import SSOApp
        for a in (HRApp(), ChatApp(), MailApp(), DocsApp(), SSOApp()):
            _APPS[a.name] = a
    return _APPS[name]


def names():
    get("hr")
    return list(_APPS)
