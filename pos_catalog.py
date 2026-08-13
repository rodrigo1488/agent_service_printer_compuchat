"""Sync do catálogo POS: Compuchat cloud → SQLite local + imagens."""
from __future__ import annotations

import logging
import os
import threading
import urllib.request
from typing import Any, Dict

import db
from product_sync import _compuchat_request, _ssl_unverified_context

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


def _sync_images(images: list, *, force: bool = False) -> Dict[str, int]:
    folder = media_dir()
    downloaded = 0
    skipped = 0
    failed = 0
    for img in images or []:
        image_id = str(img.get("id") or img.get("hash") or "").strip()
        url = str(img.get("url") or "").strip()
        hash_val = str(img.get("hash") or image_id)
        if not image_id or not url:
            continue
        dest = os.path.join(folder, image_id)
        existing = db.get_pos_image(image_id)
        if (
            not force
            and existing
            and existing.get("hash") == hash_val
            and existing.get("path")
            and os.path.isfile(existing["path"])
        ):
            skipped += 1
            continue
        try:
            _download_image(url, dest)
            db.upsert_pos_image(image_id, url, hash_val, dest)
            downloaded += 1
        except Exception as exc:
            failed += 1
            logger.warning("Falha ao baixar imagem %s: %s", url, exc)
    return {"downloaded": downloaded, "skipped": skipped, "failed": failed, "total": downloaded + skipped + failed}


def sync_catalog_from_cloud() -> Dict[str, Any]:
    with _sync_lock:
        catalog = fetch_cloud_catalog()
        db.replace_pos_catalog(catalog)
        images = _sync_images(catalog.get("images") or [])
        db.set_config("pos_last_sync_error", "")
        return {
            "ok": True,
            "catalogVersion": catalog.get("catalogVersion"),
            "users": len(catalog.get("users") or []),
            "mesas": len(catalog.get("mesas") or []),
            "products": len(catalog.get("products") or []),
            "images": images,
        }


def sync_images_from_cloud(*, force: bool = True) -> Dict[str, Any]:
    """Baixa de novo as imagens do catálogo cloud (ignora cache local se force)."""
    with _sync_lock:
        catalog = fetch_cloud_catalog()
        images = _sync_images(catalog.get("images") or [], force=force)
        db.set_config("pos_last_sync_error", "")
        return {"ok": True, "images": images, "catalogVersion": catalog.get("catalogVersion")}


