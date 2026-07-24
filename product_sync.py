"""Sync inteligente de produtos UniPlus → Compuchat."""
from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

import db
from uniplus_handler import (
    _safe_ident,
    is_uniplus_enabled,
    psycopg2,
    RealDictCursor,
)

logger = logging.getLogger("product_sync")

POLL_INTERVAL_SEC = 30
_sync_thread: Optional[threading.Thread] = None
_should_stop = False
_lock = threading.Lock()


def _api_base_from_ws(ws_url: str) -> str:
    u = (ws_url or "").strip()
    if u.startswith("wss://"):
        u = "https://" + u[6:]
    elif u.startswith("ws://"):
        u = "http://" + u[5:]
    if "/ws/print" in u:
        u = u.split("/ws/print")[0]
    return u.rstrip("/")


def _device_auth() -> Tuple[Optional[str], Optional[str]]:
    printers = db.get_printers()
    for p in printers:
        device_id = (p.get("device_id") or "").strip()
        token = (p.get("token") or "").strip()
        if device_id and token:
            return device_id, token
    return None, None


def _produto_cfg() -> Dict[str, str]:
    return {
        "table": _safe_ident(
            db.get_config("uniplus_produto_table") or "produto", "produto"
        ),
        "codigo": _safe_ident(
            db.get_config("uniplus_produto_codigo_column") or "codigo", "codigo"
        ),
        "nome": _safe_ident(
            db.get_config("uniplus_produto_nome_column") or "nome", "nome"
        ),
        "preco": _safe_ident(
            db.get_config("uniplus_produto_preco_column") or "preco", "preco"
        ),
    }


def _connect_uniplus():
    if psycopg2 is None:
        raise RuntimeError("psycopg2 não instalado")
    dsn = (db.get_config("uniplus_connection_string") or "").strip()
    if not dsn:
        raise RuntimeError("uniplus_connection_string vazio")
    conn = psycopg2.connect(dsn, connect_timeout=8)
    try:
        with conn.cursor() as cur:
            cur.execute('SET search_path TO public, unico, "$user"')
    except Exception:
        pass
    return conn


def make_fingerprint(nome: str, preco: float, dataalteracao: Any = None) -> str:
    da = ""
    if dataalteracao is not None:
        da = str(dataalteracao)
    return f"{nome}|{float(preco):.4f}|{da}"


def list_uniplus_products(q: str = "", limit: int = 500) -> List[Dict[str, Any]]:
    """Lista produtos do Postgres UniPlus (ativos e inativos)."""
    if not is_uniplus_enabled(db):
        return []
    cfg = _produto_cfg()
    limit = max(1, min(int(limit or 500), 5000))
    q = (q or "").strip()
    conn = _connect_uniplus()
    try:
        factory = RealDictCursor if RealDictCursor else None
        with conn.cursor(cursor_factory=factory) as cur:
            # Colunas em qualquer schema (não só current_schema)
            cur.execute(
                """
                SELECT lower(column_name) AS col
                FROM information_schema.columns
                WHERE lower(table_name) = lower(%s)
                  AND table_schema NOT IN ('pg_catalog', 'information_schema')
                  AND lower(column_name) IN (
                    'inativo', 'dataalteracao', 'nome', 'codigo', 'preco', 'id'
                  )
                """,
                (cfg["table"],),
            )
            cols = {
                str(r["col"] if isinstance(r, dict) else r[0]).lower()
                for r in (cur.fetchall() or [])
            }

            # Fallback de colunas se o schema não reportou
            nome_col = cfg["nome"] if cfg["nome"] else "nome"
            codigo_col = cfg["codigo"] if cfg["codigo"] else "codigo"
            preco_col = cfg["preco"] if cfg["preco"] else "preco"
            if "nome" not in cols and nome_col not in cols:
                # tenta nomes comuns
                for candidate in ("nome", "descricao", "produto"):
                    if candidate in cols:
                        nome_col = candidate
                        break
            if "preco" not in cols and preco_col not in cols:
                for candidate in ("preco", "precovenda", "valor"):
                    if candidate in cols:
                        preco_col = candidate
                        break

            where = []
            params: list = []
            # Por padrão lista todos; inativos ficam no fim e marcados na UI
            if q:
                where.append(
                    f"(CAST({codigo_col} AS text) ILIKE %s OR CAST({nome_col} AS text) ILIKE %s)"
                )
                like = f"%{q}%"
                params.extend([like, like])
            where_sql = (" WHERE " + " AND ".join(where)) if where else ""

            has_da = "dataalteracao" in cols
            has_inativo = "inativo" in cols
            da_sel = ", dataalteracao" if has_da else ", NULL::timestamp AS dataalteracao"
            inativo_sel = ", COALESCE(inativo, 0) AS inativo" if has_inativo else ", 0 AS inativo"
            order_inativo = "COALESCE(inativo, 0) ASC, " if has_inativo else ""

            sql = (
                f"SELECT CAST({codigo_col} AS text) AS codigo, "
                f"CAST({nome_col} AS text) AS nome, "
                f"COALESCE({preco_col}, 0)::float AS preco"
                f"{da_sel}{inativo_sel} "
                f"FROM {cfg['table']}{where_sql} "
                f"ORDER BY {order_inativo}{nome_col} ASC NULLS LAST, {codigo_col} ASC "
                f"LIMIT {limit}"
            )
            try:
                cur.execute(sql, params)
            except Exception as first_err:
                # Query simplificada (sem NULLS LAST / cast float) para PG mais antigo
                logger.warning("list_uniplus_products SQL principal falhou: %s", first_err)
                conn.rollback()
                sql_simple = (
                    f"SELECT CAST({codigo_col} AS text) AS codigo, "
                    f"CAST({nome_col} AS text) AS nome, "
                    f"COALESCE({preco_col}, 0) AS preco "
                    f"FROM {cfg['table']}{where_sql} "
                    f"ORDER BY {nome_col}, {codigo_col} "
                    f"LIMIT {limit}"
                )
                cur.execute(sql_simple, params)
                rows = cur.fetchall() or []
                out = []
                for row in rows:
                    if isinstance(row, dict):
                        codigo = str(row.get("codigo") or "").strip()
                        nome = str(row.get("nome") or "").strip()
                        preco = float(row.get("preco") or 0)
                    else:
                        codigo = str(row[0] or "").strip()
                        nome = str(row[1] or "").strip()
                        preco = float(row[2] or 0)
                    if not codigo and not nome:
                        continue
                    if not codigo:
                        codigo = f"?{nome[:18]}"
                    out.append(
                        {
                            "codigo": codigo[:20],
                            "nome": nome or codigo,
                            "preco": preco,
                            "dataalteracao": None,
                            "inativo": 0,
                            "fingerprint": make_fingerprint(nome, preco, None),
                        }
                    )
                return out

            rows = cur.fetchall() or []
            out = []
            for row in rows:
                if isinstance(row, dict):
                    codigo = str(row.get("codigo") or "").strip()
                    nome = str(row.get("nome") or "").strip()
                    preco = float(row.get("preco") or 0)
                    da = row.get("dataalteracao")
                    inativo = int(row.get("inativo") or 0)
                else:
                    codigo = str(row[0] or "").strip()
                    nome = str(row[1] or "").strip()
                    preco = float(row[2] or 0)
                    da = row[3] if len(row) > 3 else None
                    inativo = int(row[4] or 0) if len(row) > 4 else 0
                if not codigo and not nome:
                    continue
                if not codigo:
                    continue  # sync exige codigo
                out.append(
                    {
                        "codigo": codigo[:20],
                        "nome": nome or codigo,
                        "preco": preco,
                        "dataalteracao": da,
                        "inativo": inativo,
                        "fingerprint": make_fingerprint(nome, preco, da),
                    }
                )
            return out
    finally:
        conn.close()


def fetch_uniplus_product(codigo: str) -> Optional[Dict[str, Any]]:
    codigo = str(codigo or "").strip()
    if not codigo:
        return None
    cfg = _produto_cfg()
    conn = _connect_uniplus()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT lower(column_name) AS col
                FROM information_schema.columns
                WHERE lower(table_name) = lower(%s)
                  AND table_schema = current_schema()
                  AND lower(column_name) = 'dataalteracao'
                """,
                (cfg["table"],),
            )
            has_da = bool(cur.fetchall())
            da_sel = ", dataalteracao" if has_da else ", NULL AS dataalteracao"
            cur.execute(
                f"""
                SELECT CAST({cfg['codigo']} AS text) AS codigo,
                       CAST({cfg['nome']} AS text) AS nome,
                       COALESCE({cfg['preco']}, 0) AS preco
                       {da_sel}
                FROM {cfg['table']}
                WHERE CAST({cfg['codigo']} AS text) = %s
                LIMIT 1
                """,
                (codigo,),
            )
            row = cur.fetchone()
            if not row:
                return None
            if isinstance(row, dict):
                nome = str(row.get("nome") or "").strip()
                preco = float(row.get("preco") or 0)
                da = row.get("dataalteracao")
                cod = str(row.get("codigo") or codigo).strip()
            else:
                cod = str(row[0] or codigo).strip()
                nome = str(row[1] or "").strip()
                preco = float(row[2] or 0)
                da = row[3] if len(row) > 3 else None
            return {
                "codigo": cod[:20],
                "nome": nome,
                "preco": preco,
                "dataalteracao": da,
                "fingerprint": make_fingerprint(nome, preco, da),
            }
    finally:
        conn.close()


def upsert_to_compuchat(products: List[Dict[str, Any]]) -> Dict[str, Any]:
    device_id, token = _device_auth()
    if not device_id or not token:
        raise RuntimeError("Configure device_id e token de uma impressora no PrintAgent")
    base = _api_base_from_ws(db.get_config("ws_url") or "")
    if not base:
        raise RuntimeError("ws_url não configurada")
    url = f"{base}/agent/products/upsert"
    payload = {
        "products": [
            {
                "codigo": str(p.get("codigo") or "").strip()[:20],
                "nome": str(p.get("nome") or "").strip(),
                "preco": float(p.get("preco") or 0),
            }
            for p in products
            if str(p.get("codigo") or "").strip()
        ]
    }
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
            "X-Device-Id": device_id,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {"results": []}
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:300]
        if e.code == 404:
            raise RuntimeError(
                "HTTP 404: rota /agent/products/upsert não existe no servidor. "
                "Faça deploy do backend Compuchat com a sync de produtos e tente de novo. "
                f"URL={url}"
            ) from e
        raise RuntimeError(f"HTTP {e.code}: {detail}") from e


def sync_one(codigo: str, force: bool = False) -> Dict[str, Any]:
    """Sincroniza um produto marcado (ou força)."""
    local = db.get_sync_product(codigo)
    if not local or (not local.get("enabled") and not force):
        return {"ok": False, "error": "produto não está em sync automático"}

    remote = fetch_uniplus_product(codigo)
    if not remote:
        err = "produto não encontrado no UniPlus"
        db.update_sync_product_state(codigo, last_error=err)
        return {"ok": False, "error": err}

    fp = remote["fingerprint"]
    if not force and local.get("fingerprint") == fp and not local.get("last_error"):
        return {"ok": True, "skipped": True, "action": "unchanged"}

    try:
        data = upsert_to_compuchat([remote])
        results = data.get("results") or []
        result = results[0] if results else {}
        if result.get("error"):
            db.update_sync_product_state(
                codigo,
                nome=remote["nome"],
                preco=remote["preco"],
                last_error=str(result["error"]),
            )
            return {"ok": False, "error": result["error"], "result": result}

        db.update_sync_product_state(
            codigo,
            nome=remote["nome"],
            preco=remote["preco"],
            fingerprint=fp,
            last_error="",
            synced=True,
        )
        return {"ok": True, "result": result, "product": remote}
    except Exception as e:
        err = str(e)
        db.update_sync_product_state(codigo, last_error=err)
        return {"ok": False, "error": err}


def enable_product(codigo: str, enabled: bool = True) -> Dict[str, Any]:
    remote = fetch_uniplus_product(codigo) if enabled else None
    nome = (remote or {}).get("nome") or ""
    preco = float((remote or {}).get("preco") or 0)
    db.set_sync_product_enabled(codigo, enabled, nome=nome, preco=preco)
    if not enabled:
        return {"ok": True, "enabled": False}
    # Primeiro upsert imediato
    return sync_one(codigo, force=True)


def poll_once() -> int:
    """Sincroniza produtos enabled com fingerprint alterado. Retorna qtd enviada."""
    if not is_uniplus_enabled(db):
        return 0
    enabled = db.list_sync_products(enabled_only=True)
    if not enabled:
        return 0
    sent = 0
    for item in enabled:
        codigo = item["codigo"]
        try:
            remote = fetch_uniplus_product(codigo)
            if not remote:
                db.update_sync_product_state(
                    codigo, last_error="produto não encontrado no UniPlus"
                )
                continue
            if item.get("fingerprint") == remote["fingerprint"] and not item.get(
                "last_error"
            ):
                continue
            res = sync_one(codigo, force=True)
            if res.get("ok"):
                sent += 1
                logger.info(
                    "product_sync ok codigo=%s action=%s",
                    codigo,
                    (res.get("result") or {}).get("action"),
                )
            else:
                logger.warning(
                    "product_sync fail codigo=%s err=%s", codigo, res.get("error")
                )
        except Exception as e:
            logger.warning("product_sync poll codigo=%s: %s", codigo, e)
            db.update_sync_product_state(codigo, last_error=str(e))
    return sent


def _poll_loop():
    global _should_stop
    logger.info("product_sync worker iniciado (intervalo=%ss)", POLL_INTERVAL_SEC)
    while not _should_stop:
        try:
            poll_once()
        except Exception as e:
            logger.warning("product_sync poll_once: %s", e)
        for _ in range(POLL_INTERVAL_SEC * 2):
            if _should_stop:
                break
            time.sleep(0.5)
    logger.info("product_sync worker parado")


def start_product_sync_thread() -> None:
    global _sync_thread, _should_stop
    with _lock:
        if _sync_thread and _sync_thread.is_alive():
            return
        _should_stop = False
        _sync_thread = threading.Thread(
            target=_poll_loop, name="uniplus-product-sync", daemon=True
        )
        _sync_thread.start()


def stop_product_sync_thread() -> None:
    global _should_stop
    _should_stop = True
