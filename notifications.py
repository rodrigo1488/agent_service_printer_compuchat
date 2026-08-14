"""Notificações do Windows via ícone da bandeja (pystray)."""
import sys
import threading
import time
from typing import Optional

_lock = threading.Lock()
_tray_icon = None
_last_notify_at = {}
DEFAULT_COOLDOWN_SECONDS = 15


def set_tray_icon(icon) -> None:
    """Registra o ícone da bandeja para exibir toasts."""
    global _tray_icon
    with _lock:
        _tray_icon = icon


def clear_tray_icon() -> None:
    global _tray_icon
    with _lock:
        _tray_icon = None


def _should_skip(key: str, cooldown_seconds: int) -> bool:
    now = time.time()
    last = _last_notify_at.get(key, 0)
    if now - last < cooldown_seconds:
        return True
    _last_notify_at[key] = now
    return False


def notify(title: str, message: str, *, key: Optional[str] = None, cooldown_seconds: int = DEFAULT_COOLDOWN_SECONDS) -> bool:
    """
    Exibe toast do Windows quando a bandeja estiver ativa.
    Retorna True se tentou exibir; False se indisponível ou em cooldown.
    """
    title = str(title or "Print Agent").strip()[:64]
    message = str(message or "").strip()
    if not message:
        return False

    dedupe_key = key or f"{title}|{message[:120]}"
    with _lock:
        if _should_skip(dedupe_key, max(0, int(cooldown_seconds or 0))):
            return False
        icon = _tray_icon

    if icon is None:
        return False

    try:
        if getattr(icon, "HAS_NOTIFICATION", False):
            icon.notify(message[:240], title)
            return True
    except Exception as exc:
        print(f"[WARN] Falha ao exibir notificação Windows: {exc}")

    return False


def notify_print_failure(message: str, *, protocol: str = "", job_id=None) -> bool:
    """Toast padronizado para falha de impressão."""
    if sys.platform != "win32":
        return False

    title = "Falha na impressão"
    body = message
    if protocol:
        body = f"{message}\nPedido: {protocol}"
    key = f"print-fail|{job_id or protocol or message[:80]}"
    return notify(title, body, key=key)
