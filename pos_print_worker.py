"""Impressão de cozinha do POS em background — não bloqueia HTTP/Waitress."""
from __future__ import annotations

import threading
from typing import Any, Callable, Dict

_print_lock = threading.Lock()
_print_threads = 0
_MAX_BG_PRINT_THREADS = 8


def schedule_kitchen_print(
    client_order_id: str,
    payload: Dict[str, Any],
    print_fn: Callable[[Dict[str, Any]], Dict[str, Any]],
) -> None:
    """
    Dispara impressão em thread daemon.
    Atualiza pos_orders_queue quando terminar (printed / printError).
    """
    global _print_threads

    with _print_lock:
        if _print_threads >= _MAX_BG_PRINT_THREADS:
            print(
                f"[POS] fila de impressão cheia ({_MAX_BG_PRINT_THREADS}); "
                f"pedido {client_order_id} impresso depois"
            )
        _print_threads += 1

    def _run() -> None:
        global _print_threads
        import db

        print_info = {"printed": False, "error": "Impressão não iniciada"}
        try:
            print_info = print_fn(payload)
        except Exception as exc:
            print(f"[POS] impressão async falhou: {exc}")
            print_info = {"printed": False, "error": str(exc)}
        finally:
            try:
                existing = db.get_pos_order(client_order_id)
                if isinstance(existing, dict) and existing.get("ok"):
                    existing["printed"] = bool(print_info.get("printed"))
                    existing["printError"] = print_info.get("error") or ""
                    db.save_pos_order(client_order_id, existing)
            except Exception as exc:
                print(f"[POS] falha ao gravar resultado da impressão: {exc}")
            with _print_lock:
                _print_threads = max(0, _print_threads - 1)

    threading.Thread(
        target=_run,
        daemon=True,
        name=f"pos_kitchen_print_{str(client_order_id)[:8]}",
    ).start()
