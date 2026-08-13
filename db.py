"""Módulo de banco de dados SQLite para o Print Agent."""
import sqlite3
import json
import os
from datetime import datetime
from typing import List, Dict, Any, Optional

from error_recovery import (
    DatabaseRecovery,
    retry_with_backoff,
    RetryConfig,
)

DB_FILE = "agent.db"
DEFAULT_CONFIG = {
    "printer_ip": "192.168.1.100",
    "printer_port": "9100",
    "printer_type": "raw",
    "paper_width": "32",
    "printer_encoding": "cp850",
    "ws_url": "ws://localhost:4000/ws/print",
    "token": "",
    "device_id": "",
    "restart_service_on_save": "true",
    "uniplus_enabled": "false",
    "uniplus_connection_string": "",
    "uniplus_produto_table": "produto",
    "uniplus_produto_codigo_column": "codigo",
    "uniplus_produto_id_column": "id",
    "uniplus_contamesa_table": "contamesa",
    "uniplus_contamesaitem_table": "contamesaitem",
    "uniplus_produto_preco_column": "preco",
    "uniplus_produto_nome_column": "nome",
    "uniplus_last_error": "",
    "pos_api_token": "",
    "pos_catalog_version": "0",
    "pos_catalog_updated_at": "",
    "pos_last_sync_error": "",
    "uniplus_mesa_tipopedido": "1",
}
PRINTER_KEYS = ("device_id", "token", "printer_ip", "printer_port", "printer_type", "paper_width", "printer_encoding", "name", "connection_type", "printer_name_local")


def _get_connection():
    """Retorna conexão com o banco."""
    # Validar conexão antes de retornar
    if not DatabaseRecovery.validate_db_connection(DB_FILE):
        # Tentar criar backup e recriar banco se necessário
        print(f"[WARN] Problema detectado no banco de dados. Tentando recuperar...")
        DatabaseRecovery.backup_db(DB_FILE)
    
    conn = sqlite3.connect(DB_FILE, timeout=10.0)  # Timeout aumentado
    conn.row_factory = sqlite3.Row
    # Habilitar WAL mode para melhor concorrência
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    """Inicializa tabelas do banco de dados."""
    conn = _get_connection()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS config (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS print_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id INTEGER NOT NULL,
                status TEXT NOT NULL,
                message TEXT,
                kind TEXT DEFAULT 'print',
                detail TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Migração leve: colunas novas em bancos antigos
        cols = {
            row[1]
            for row in conn.execute("PRAGMA table_info(print_logs)").fetchall()
        }
        if "kind" not in cols:
            conn.execute("ALTER TABLE print_logs ADD COLUMN kind TEXT DEFAULT 'print'")
        if "detail" not in cols:
            conn.execute("ALTER TABLE print_logs ADD COLUMN detail TEXT")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS uniplus_sync_products (
                codigo TEXT PRIMARY KEY,
                nome TEXT,
                preco REAL,
                fingerprint TEXT,
                enabled INTEGER NOT NULL DEFAULT 1,
                last_synced_at TIMESTAMP,
                last_error TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS pos_users (
                id INTEGER PRIMARY KEY,
                name TEXT,
                pin TEXT,
                profile TEXT,
                payload TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS pos_mesas (
                id INTEGER PRIMARY KEY,
                number TEXT,
                name TEXT,
                type TEXT,
                status TEXT,
                form_id INTEGER,
                contact_name TEXT,
                display_order INTEGER,
                section TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS pos_products (
                id INTEGER PRIMARY KEY,
                payload TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS pos_groups (
                id INTEGER PRIMARY KEY,
                kind TEXT,
                payload TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS pos_printers (
                id INTEGER PRIMARY KEY,
                device_id TEXT,
                name TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS pos_images (
                id TEXT PRIMARY KEY,
                url TEXT,
                hash TEXT,
                path TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS pos_orders_queue (
                client_order_id TEXT PRIMARY KEY,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                result_json TEXT
            )
        """)
        conn.commit()

        cursor = conn.execute("SELECT COUNT(*) FROM config")
        if cursor.fetchone()[0] == 0:
            for key, value in DEFAULT_CONFIG.items():
                conn.execute(
                    "INSERT INTO config (key, value) VALUES (?, ?)",
                    (key, str(value)),
                )
            conn.commit()
        else:
            # Garante chaves novas (ex.: UniPlus) em bancos já existentes
            for key, value in DEFAULT_CONFIG.items():
                conn.execute(
                    "INSERT OR IGNORE INTO config (key, value) VALUES (?, ?)",
                    (key, str(value)),
                )
            conn.commit()
        # Instalação antiga gravou 0 (delivery). O PDV de mesa do Uniplus usa 1.
        row = conn.execute(
            "SELECT value FROM config WHERE key = 'uniplus_mesa_tipopedido'"
        ).fetchone()
        if row is not None and str(row[0]).strip() == "0":
            conn.execute(
                "UPDATE config SET value = '1' WHERE key = 'uniplus_mesa_tipopedido'"
            )
            conn.commit()
    finally:
        conn.close()


# --- Sync de produtos UniPlus → Compuchat ---


def list_sync_products(enabled_only: bool = False) -> List[Dict[str, Any]]:
    conn = _get_connection()
    try:
        sql = (
            "SELECT codigo, nome, preco, fingerprint, enabled, last_synced_at, last_error "
            "FROM uniplus_sync_products"
        )
        if enabled_only:
            sql += " WHERE enabled = 1"
        sql += " ORDER BY nome COLLATE NOCASE, codigo"
        rows = conn.execute(sql).fetchall()
        return [
            {
                "codigo": r[0],
                "nome": r[1] or "",
                "preco": float(r[2] or 0),
                "fingerprint": r[3] or "",
                "enabled": bool(r[4]),
                "last_synced_at": r[5],
                "last_error": r[6] or "",
            }
            for r in rows
        ]
    finally:
        conn.close()


def get_sync_product(codigo: str) -> Optional[Dict[str, Any]]:
    conn = _get_connection()
    try:
        row = conn.execute(
            "SELECT codigo, nome, preco, fingerprint, enabled, last_synced_at, last_error "
            "FROM uniplus_sync_products WHERE codigo = ?",
            (str(codigo).strip(),),
        ).fetchone()
        if not row:
            return None
        return {
            "codigo": row[0],
            "nome": row[1] or "",
            "preco": float(row[2] or 0),
            "fingerprint": row[3] or "",
            "enabled": bool(row[4]),
            "last_synced_at": row[5],
            "last_error": row[6] or "",
        }
    finally:
        conn.close()


def set_sync_product_enabled(
    codigo: str,
    enabled: bool,
    *,
    nome: str = "",
    preco: float = 0.0,
) -> None:
    codigo = str(codigo).strip()
    if not codigo:
        return
    conn = _get_connection()
    try:
        existing = conn.execute(
            "SELECT codigo FROM uniplus_sync_products WHERE codigo = ?", (codigo,)
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE uniplus_sync_products SET enabled = ?, nome = COALESCE(NULLIF(?, ''), nome), "
                "preco = CASE WHEN ? >= 0 THEN ? ELSE preco END WHERE codigo = ?",
                (1 if enabled else 0, nome or "", float(preco), float(preco), codigo),
            )
        else:
            conn.execute(
                "INSERT INTO uniplus_sync_products "
                "(codigo, nome, preco, fingerprint, enabled, last_synced_at, last_error) "
                "VALUES (?, ?, ?, '', ?, NULL, '')",
                (codigo, nome or "", float(preco or 0), 1 if enabled else 0),
            )
        conn.commit()
    finally:
        conn.close()


def update_sync_product_state(
    codigo: str,
    *,
    nome: Optional[str] = None,
    preco: Optional[float] = None,
    fingerprint: Optional[str] = None,
    last_error: Optional[str] = None,
    synced: bool = False,
) -> None:
    codigo = str(codigo).strip()
    if not codigo:
        return
    conn = _get_connection()
    try:
        sets = []
        params: list = []
        if nome is not None:
            sets.append("nome = ?")
            params.append(nome)
        if preco is not None:
            sets.append("preco = ?")
            params.append(float(preco))
        if fingerprint is not None:
            sets.append("fingerprint = ?")
            params.append(fingerprint)
        if last_error is not None:
            sets.append("last_error = ?")
            params.append(last_error)
        if synced:
            sets.append("last_synced_at = CURRENT_TIMESTAMP")
        if not sets:
            return
        params.append(codigo)
        conn.execute(
            f"UPDATE uniplus_sync_products SET {', '.join(sets)} WHERE codigo = ?",
            params,
        )
        conn.commit()
    finally:
        conn.close()


def get_enabled_sync_codigos() -> List[str]:
    return [p["codigo"] for p in list_sync_products(enabled_only=True)]


def get_config(key: str) -> str:
    """Retorna valor de uma chave de configuração."""
    conn = _get_connection()
    try:
        cursor = conn.execute(
            "SELECT value FROM config WHERE key = ?", (key,)
        )
        row = cursor.fetchone()
        if row:
            return row[0]
        default = DEFAULT_CONFIG.get(key, "")
        return default
    finally:
        conn.close()


def set_config(key: str, value: str) -> None:
    """Define valor de uma chave de configuração."""
    @retry_with_backoff(RetryConfig(
        max_retries=3,
        initial_delay=0.5,
        max_delay=5.0,
        retryable_exceptions=(sqlite3.OperationalError, sqlite3.DatabaseError)
    ))
    def _save_config():
        conn = _get_connection()
        try:
            print(f"[DEBUG] set_config: key={key}, value_length={len(str(value))}")
            conn.execute(
                "INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)",
                (key, str(value)),
            )
            conn.commit()
            print(f"[DEBUG] set_config: valor salvo com sucesso")
        finally:
            conn.close()
    
    try:
        _save_config()
    except Exception as e:
        print(f"[ERROR] set_config: erro ao salvar após múltiplas tentativas - {str(e)}")
        raise


def get_all_config() -> dict:
    """Retorna todas as configurações como dicionário."""
    conn = _get_connection()
    try:
        cursor = conn.execute("SELECT key, value FROM config")
        rows = cursor.fetchall()
        result = dict(DEFAULT_CONFIG)
        for row in rows:
            result[row[0]] = row[1]
        return result
    finally:
        conn.close()


def add_print_log(
    job_id: int,
    status: str,
    message: str = None,
    kind: str = "print",
    detail: Any = None,
) -> None:
    """Adiciona registro de impressão/UniPlus ao log."""
    kind_val = (kind or "print").strip().lower() or "print"
    if isinstance(detail, (dict, list)):
        detail_val = json.dumps(detail, ensure_ascii=False, default=str)
    elif detail is None:
        detail_val = None
    else:
        detail_val = str(detail)

    @retry_with_backoff(RetryConfig(
        max_retries=2,
        initial_delay=0.3,
        max_delay=2.0,
        retryable_exceptions=(sqlite3.OperationalError, sqlite3.DatabaseError)
    ))
    def _save_log():
        conn = _get_connection()
        try:
            conn.execute(
                "INSERT INTO print_logs (job_id, status, message, kind, detail) VALUES (?, ?, ?, ?, ?)",
                (job_id, status, message or "", kind_val, detail_val),
            )
            conn.commit()
        finally:
            conn.close()
    
    try:
        _save_log()
    except Exception as e:
        # Log de erro mas não falhar completamente
        print(f"[ERROR] Falha ao salvar log no banco (job_id={job_id}): {e}")


def get_printers() -> List[Dict[str, Any]]:
    """Retorna lista de impressoras. Se não houver lista salva, retorna uma impressora a partir das chaves legadas."""
    raw = get_config("printers")
    if raw and raw.strip():
        try:
            data = json.loads(raw)
            if isinstance(data, list) and len(data) > 0:
                return [
                    {
                        "device_id": p.get("device_id", ""),
                        "token": p.get("token", ""),
                        "printer_ip": p.get("printer_ip", "192.168.1.100"),
                        "printer_port": int(p.get("printer_port") or 9100),
                        "printer_type": p.get("printer_type") or "raw",
                        "paper_width": str(p.get("paper_width") or "32"),
                        "printer_encoding": p.get("printer_encoding") or "cp850",
                        "name": p.get("name") or "",
                        "connection_type": p.get("connection_type") or "network",
                        "printer_name_local": p.get("printer_name_local") or "",
                    }
                    for p in data
                ]
        except (json.JSONDecodeError, TypeError):
            pass
    device_id = get_config("device_id")
    token = get_config("token")
    if device_id and token:
        return [
            {
                "device_id": device_id,
                "token": token,
                "printer_ip": get_config("printer_ip") or "192.168.1.100",
                "printer_port": int(get_config("printer_port") or 9100),
                "printer_type": get_config("printer_type") or "raw",
                "paper_width": get_config("paper_width") or "32",
                "printer_encoding": get_config("printer_encoding") or "cp850",
                "name": "",
                "connection_type": "network",
                "printer_name_local": "",
            }
        ]
    return []


def set_printers(printers: List[Dict[str, Any]]) -> None:
    """Salva lista de impressoras como JSON."""
    from error_recovery import DataValidator
    
    print(f"[DEBUG] set_printers chamado com {len(printers)} impressora(s)")
    
    # Sanitizar configurações antes de salvar
    sanitized_printers = []
    for p in printers:
        sanitized = DataValidator.sanitize_printer_config(p)
        sanitized_printers.append(sanitized)
    
    list_ = [
        {
            "device_id": p.get("device_id", ""),
            "token": p.get("token", ""),
            "printer_ip": p.get("printer_ip", "192.168.1.100"),
            "printer_port": int(p.get("printer_port") or 9100),
            "printer_type": p.get("printer_type") or "raw",
            "paper_width": str(p.get("paper_width") or "32"),
            "printer_encoding": p.get("printer_encoding") or "cp850",
            "name": p.get("name") or "",
            "connection_type": p.get("connection_type") or "network",
            "printer_name_local": p.get("printer_name_local") or "",
        }
        for p in sanitized_printers
    ]
    json_data = json.dumps(list_)
    print(f"[DEBUG] JSON a ser salvo: {json_data}")
    set_config("printers", json_data)
    print(f"[DEBUG] Configuração salva no banco de dados")


def get_print_logs(
    limit: int = 50,
    status: str = None,
    q: str = None,
    kind: str = None,
) -> list:
    """Retorna registros com filtro opcional de status, texto e tipo (print|uniplus)."""
    conn = _get_connection()
    try:
        sql = (
            "SELECT id, job_id, status, message, created_at, "
            "ifnull(kind, 'print') AS kind, detail "
            "FROM print_logs WHERE 1=1"
        )
        params = []
        if status and str(status).strip() and str(status).strip().lower() != "all":
            sql += " AND lower(status) = ?"
            params.append(str(status).strip().lower())
        if kind and str(kind).strip() and str(kind).strip().lower() != "all":
            sql += " AND lower(ifnull(kind, 'print')) = ?"
            params.append(str(kind).strip().lower())
        if q and str(q).strip():
            sql += (
                " AND (CAST(job_id AS TEXT) LIKE ? OR ifnull(message,'') LIKE ?"
                " OR ifnull(detail,'') LIKE ?)"
            )
            like = f"%{str(q).strip()}%"
            params.extend([like, like, like])
        sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
        params.append(max(1, min(int(limit or 50), 500)))
        cursor = conn.execute(sql, params)
        logs = []
        for row in cursor.fetchall():
            detail_raw = row[6]
            detail_obj = None
            if detail_raw:
                try:
                    detail_obj = json.loads(detail_raw)
                except Exception:
                    detail_obj = {"raw": detail_raw}
            logs.append(
                {
                    "id": row[0],
                    "job_id": row[1],
                    "status": row[2],
                    "message": row[3] or "",
                    "created_at": row[4],
                    "kind": row[5] or "print",
                    "detail": detail_obj,
                }
            )
        return logs
    finally:
        conn.close()


def get_print_log_stats() -> dict:
    """Contadores rápidos para o painel de logs."""
    conn = _get_connection()
    try:
        cursor = conn.execute(
            """
            SELECT
              COUNT(*) AS total,
              SUM(CASE WHEN lower(status) = 'done' THEN 1 ELSE 0 END) AS done,
              SUM(CASE WHEN lower(status) = 'error' THEN 1 ELSE 0 END) AS error,
              SUM(CASE WHEN lower(status) NOT IN ('done','error') THEN 1 ELSE 0 END) AS other
            FROM print_logs
            """
        )
        row = cursor.fetchone()
        return {
            "total": int(row[0] or 0),
            "done": int(row[1] or 0),
            "error": int(row[2] or 0),
            "other": int(row[3] or 0),
        }
    finally:
        conn.close()


def _json_load(raw: Optional[str], default=None):
    if not raw:
        return default if default is not None else {}
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return default if default is not None else {}


def replace_pos_catalog(catalog: Dict[str, Any]) -> None:
    """Substitui o snapshot POS local pelo catálogo da cloud."""
    conn = _get_connection()
    try:
        conn.execute("DELETE FROM pos_users")
        conn.execute("DELETE FROM pos_mesas")
        conn.execute("DELETE FROM pos_products")
        conn.execute("DELETE FROM pos_groups")
        conn.execute("DELETE FROM pos_printers")
        for u in catalog.get("users") or []:
            conn.execute(
                "INSERT INTO pos_users (id, name, pin, profile, payload) VALUES (?, ?, ?, ?, ?)",
                (
                    int(u.get("id")),
                    str(u.get("name") or ""),
                    str(u.get("pin") or ""),
                    str(u.get("profile") or ""),
                    json.dumps(u, ensure_ascii=False),
                ),
            )
        for m in catalog.get("mesas") or []:
            conn.execute(
                "INSERT INTO pos_mesas (id, number, name, type, status, form_id, contact_name, display_order, section) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    int(m.get("id")),
                    str(m.get("number") or ""),
                    str(m.get("name") or ""),
                    str(m.get("type") or "mesa"),
                    str(m.get("status") or "livre"),
                    m.get("formId"),
                    m.get("contactName"),
                    int(m.get("displayOrder") or 0),
                    m.get("section"),
                ),
            )
        for p in catalog.get("products") or []:
            conn.execute(
                "INSERT INTO pos_products (id, payload) VALUES (?, ?)",
                (int(p.get("id")), json.dumps(p, ensure_ascii=False)),
            )
        for g in catalog.get("groups") or []:
            conn.execute(
                "INSERT INTO pos_groups (id, kind, payload) VALUES (?, ?, ?)",
                (int(g.get("id")), "addon", json.dumps(g, ensure_ascii=False)),
            )
        for pr in catalog.get("printers") or []:
            conn.execute(
                "INSERT INTO pos_printers (id, device_id, name) VALUES (?, ?, ?)",
                (int(pr.get("id")), str(pr.get("deviceId") or ""), str(pr.get("name") or "")),
            )
        conn.commit()
        set_config("pos_catalog_version", str(catalog.get("catalogVersion") or 0))
        set_config("pos_catalog_updated_at", str(catalog.get("updatedAt") or ""))
        set_config(
            "pos_product_groups",
            json.dumps(catalog.get("productGroups") or [], ensure_ascii=False),
        )
        set_config(
            "pos_grupo_addon",
            json.dumps(catalog.get("grupoAddOn") or [], ensure_ascii=False),
        )
        set_config(
            "pos_print_routes",
            json.dumps(catalog.get("printRoutes") or [], ensure_ascii=False),
        )
    finally:
        conn.close()


def upsert_pos_image(image_id: str, url: str, hash_val: str, path: str) -> None:
    conn = _get_connection()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO pos_images (id, url, hash, path) VALUES (?, ?, ?, ?)",
            (image_id, url, hash_val, path),
        )
        conn.commit()
    finally:
        conn.close()


def get_pos_image(image_id: str) -> Optional[Dict[str, Any]]:
    conn = _get_connection()
    try:
        row = conn.execute(
            "SELECT id, url, hash, path FROM pos_images WHERE id = ?",
            (image_id,),
        ).fetchone()
        if not row:
            return None
        return {"id": row[0], "url": row[1], "hash": row[2], "path": row[3]}
    finally:
        conn.close()


def list_pos_images() -> List[Dict[str, Any]]:
    conn = _get_connection()
    try:
        rows = conn.execute("SELECT id, url, hash, path FROM pos_images").fetchall()
        return [{"id": r[0], "url": r[1], "hash": r[2], "path": r[3]} for r in rows]
    finally:
        conn.close()


def list_pos_users() -> List[Dict[str, Any]]:
    conn = _get_connection()
    try:
        rows = conn.execute(
            "SELECT id, name, pin, profile, payload FROM pos_users ORDER BY name COLLATE NOCASE"
        ).fetchall()
        out = []
        for r in rows:
            payload = _json_load(r[4], {})
            payload.update({"id": r[0], "name": r[1] or "", "pin": r[2] or "", "profile": r[3] or ""})
            out.append(payload)
        return out
    finally:
        conn.close()


def get_pos_user(user_id: int) -> Optional[Dict[str, Any]]:
    conn = _get_connection()
    try:
        row = conn.execute(
            "SELECT id, name, pin, profile, payload FROM pos_users WHERE id = ?",
            (int(user_id),),
        ).fetchone()
        if not row:
            return None
        payload = _json_load(row[4], {})
        payload.update({"id": row[0], "name": row[1] or "", "pin": row[2] or "", "profile": row[3] or ""})
        return payload
    finally:
        conn.close()


def list_pos_mesas() -> List[Dict[str, Any]]:
    conn = _get_connection()
    try:
        rows = conn.execute(
            "SELECT id, number, name, type, status, form_id, contact_name, display_order, section "
            "FROM pos_mesas ORDER BY display_order, number COLLATE NOCASE"
        ).fetchall()
        return [
            {
                "id": r[0],
                "number": r[1] or "",
                "name": r[2] or "",
                "type": r[3] or "mesa",
                "status": r[4] or "livre",
                "formId": r[5],
                "contactName": r[6],
                "displayOrder": r[7] or 0,
                "section": r[8],
            }
            for r in rows
        ]
    finally:
        conn.close()


def get_pos_mesa(mesa_id: int) -> Optional[Dict[str, Any]]:
    conn = _get_connection()
    try:
        row = conn.execute(
            "SELECT id, number, name, type, status, form_id, contact_name, display_order, section "
            "FROM pos_mesas WHERE id = ?",
            (int(mesa_id),),
        ).fetchone()
        if not row:
            return None
        return {
            "id": row[0],
            "number": row[1] or "",
            "name": row[2] or "",
            "type": row[3] or "mesa",
            "status": row[4] or "livre",
            "formId": row[5],
            "contactName": row[6],
            "displayOrder": row[7] or 0,
            "section": row[8],
        }
    finally:
        conn.close()


def update_pos_mesa(mesa_id: int, *, status: str, contact_name: Optional[str] = None) -> Optional[Dict[str, Any]]:
    conn = _get_connection()
    try:
        conn.execute(
            "UPDATE pos_mesas SET status = ?, contact_name = ? WHERE id = ?",
            (status, contact_name, int(mesa_id)),
        )
        conn.commit()
    finally:
        conn.close()
    return get_pos_mesa(mesa_id)


def list_pos_products() -> List[Dict[str, Any]]:
    conn = _get_connection()
    try:
        rows = conn.execute("SELECT payload FROM pos_products").fetchall()
        return [_json_load(r[0], {}) for r in rows]
    finally:
        conn.close()


def get_pos_product(product_id: int) -> Optional[Dict[str, Any]]:
    conn = _get_connection()
    try:
        row = conn.execute(
            "SELECT payload FROM pos_products WHERE id = ?", (int(product_id),)
        ).fetchone()
        return _json_load(row[0], {}) if row else None
    finally:
        conn.close()


def list_pos_addon_groups() -> List[Dict[str, Any]]:
    conn = _get_connection()
    try:
        rows = conn.execute(
            "SELECT payload FROM pos_groups WHERE kind = 'addon'"
        ).fetchall()
        return [_json_load(r[0], {}) for r in rows]
    finally:
        conn.close()


def list_pos_print_routes() -> List[Dict[str, Any]]:
    rows = _json_load(get_config("pos_print_routes"), [])
    return rows if isinstance(rows, list) else []


def list_pos_printers() -> List[Dict[str, Any]]:
    conn = _get_connection()
    try:
        rows = conn.execute("SELECT id, device_id, name FROM pos_printers").fetchall()
        return [{"id": r[0], "deviceId": r[1], "name": r[2]} for r in rows]
    finally:
        conn.close()


def get_pos_order(client_order_id: str) -> Optional[Dict[str, Any]]:
    conn = _get_connection()
    try:
        row = conn.execute(
            "SELECT result_json FROM pos_orders_queue WHERE client_order_id = ?",
            (client_order_id,),
        ).fetchone()
        return _json_load(row[0], None) if row else None
    finally:
        conn.close()


def save_pos_order(client_order_id: str, result: Dict[str, Any]) -> None:
    conn = _get_connection()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO pos_orders_queue (client_order_id, result_json) VALUES (?, ?)",
            (client_order_id, json.dumps(result, ensure_ascii=False, default=str)),
        )
        conn.commit()
    finally:
        conn.close()


def build_pos_sync_payload() -> Dict[str, Any]:
    return {
        "catalogVersion": int(get_config("pos_catalog_version") or 0),
        "updatedAt": get_config("pos_catalog_updated_at") or "",
        "users": [
            {"id": u["id"], "name": u["name"], "pin": bool(u.get("pin")), "profile": u.get("profile")}
            for u in list_pos_users()
        ],
        "mesas": list_pos_mesas(),
        "products": list_pos_products(),
        "groups": list_pos_addon_groups(),
        "productGroups": _json_load(get_config("pos_product_groups"), []),
        "grupoAddOn": _json_load(get_config("pos_grupo_addon"), []),
        "printRoutes": _json_load(get_config("pos_print_routes"), []),
        "printers": list_pos_printers(),
        "images": [
            {"id": i["id"], "hash": i["hash"], "url": f"/pos/media/{i['id']}"}
            for i in list_pos_images()
        ],
    }
