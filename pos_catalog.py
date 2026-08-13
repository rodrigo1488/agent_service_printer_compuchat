"""Sync do catálogo POS: Compuchat cloud → SQLite local + imagens."""
from __future__ import annotations

import logging
import os
import threading
import urllib.request
from typing import Any, Dict, Optional

import db
from product_sync import (
    _api_base_from_ws,
    _compuchat_request,
    _device_auth,
    _ssl_unverified_context,
)

logger = logging.getLogger("pos_catalog")

MEDIA_DIR = "pos_media"
_sync_lock = threading.Lock()


def media_dir() -> str:
    path = os.path.abspath(MEDIA_DIR)
    os.makedirs(path, exist_ok=True)
    return path


def fetch_cloud_catalog() -> Dict[str, Any]:
    return _compuchat_request("GET", "/agent/pos/catalog", timeout=45)


def _download_image(url: str, dest: str) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "Compuchat-PrintAgent"})
    with urllib.request.urlopen(
        req, timeout=20, context=_ssl_unverified_context()
    ) as resp:
        data = resp.read()
    tmp = dest + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.replace(tmp, dest)


def _sync_images(images: list) -> None:
    folder = media_dir()
    for img in images or []:
        image_id = str(img.get("id") or img.get("hash") or "").strip()
        url = str(img.get("url") or "").strip()
        hash_val = str(img.get("hash") or image_id)
        if not image_id or not url:
            continue
        dest = os.path.join(folder, image_id)
        existing = db.get_pos_image(image_id)
        if existing and existing.get("hash") == hash_val and existing.get("path") and os.path.isfile(existing["path"]):
            continue
        try:
            _download_image(url, dest)
            db.upsert_pos_image(image_id, url, hash_val, dest)
        except Exception as exc:
            logger.warning("Falha ao baixar imagem %s: %s", url, exc)


def sync_catalog_from_cloud() -> Dict[str, Any]:
    with _sync_lock:
        catalog = fetch_cloud_catalog()
        db.replace_pos_catalog(catalog)
        _sync_images(catalog.get("images") or [])
        db.set_config("pos_last_sync_error", "")
        return {
            "ok": True,
            "catalogVersion": catalog.get("catalogVersion"),
            "users": len(catalog.get("users") or []),
            "mesas": len(catalog.get("mesas") or []),
            "products": len(catalog.get("products") or []),
        }


def cloud_ocupar_mesa(mesa_id: int, customer_name: str) -> Optional[Dict[str, Any]]:
    try:
        return _compuchat_request(
            "PUT",
            f"/agent/pos/mesas/{int(mesa_id)}/ocupar",
            body={"customerName": customer_name},
            timeout=15,
        )
    except Exception as exc:
        logger.warning("Cloud ocupar mesa %s falhou: %s", mesa_id, exc)
        return None


def cloud_liberar_mesa(mesa_id: int) -> Optional[Dict[str, Any]]:
    try:
        return _compuchat_request(
            "PUT",
            f"/agent/pos/mesas/{int(mesa_id)}/liberar",
            timeout=15,
        )
    except Exception as exc:
        logger.warning("Cloud liberar mesa %s falhou: %s", mesa_id, exc)
        return None


def cloud_reachable() -> bool:
    try:
        device_id, token = _device_auth()
        return bool(device_id and token and _api_base_from_ws(db.get_config("ws_url") or ""))
    except Exception:
        return False
