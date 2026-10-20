"""SSL permissivo para SaaS Compuchat com certificado autoassinado / cadeia incompleta.

O WebSocket já usa CERT_NONE em agent.py. As chamadas HTTPS (urllib) da tela
de produtos e do sync precisam do mesmo critério — inclusive se algum caller
esquecer context= no urlopen.
"""
from __future__ import annotations

import ssl
import urllib.request

_installed = False


def unverified_ssl_context() -> ssl.SSLContext:
    ctx = ssl._create_unverified_context()
    ctx.check_hostname = False
    try:
        ctx.verify_mode = ssl.CERT_NONE
    except Exception:
        pass
    return ctx


def install_insecure_ssl() -> None:
    """Patch global: default HTTPS context + opener urllib sem verificação."""
    global _installed
    if _installed:
        return
    ctx = unverified_ssl_context()
    ssl._create_default_https_context = unverified_ssl_context  # type: ignore[assignment]
    try:
        opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx))
        urllib.request.install_opener(opener)
    except Exception:
        pass
    _installed = True
