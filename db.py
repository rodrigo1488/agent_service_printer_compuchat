"""Módulo de banco de dados SQLite para o Print Agent."""
import sqlite3
import json
import os
from datetime import datetime
from typing import List, Dict, Any

DB_FILE = "agent.db"
DEFAULT_CONFIG = {
    "printer_ip": "192.168.1.100",
    "printer_port": "9100",
    "printer_type": "raw",
    "paper_width": "32",
    "printer_encoding": "cp850",
    "ws_url": "wss://api.dominio.com/ws/print",
    "token": "",
    "device_id": "",
}
PRINTER_KEYS = ("device_id", "token", "printer_ip", "printer_port", "printer_type", "paper_width", "printer_encoding", "name", "connection_type", "printer_name_local")


def _get_connection():
    """Retorna conexão com o banco."""
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
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
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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
    finally:
        conn.close()


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
    conn = _get_connection()
    try:
        print(f"[DEBUG] set_config: key={key}, value_length={len(str(value))}")
        conn.execute(
            "INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)",
            (key, str(value)),
        )
        conn.commit()
        print(f"[DEBUG] set_config: valor salvo com sucesso")
    except Exception as e:
        print(f"[ERROR] set_config: erro ao salvar - {str(e)}")
        raise
    finally:
        conn.close()


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


def add_print_log(job_id: int, status: str, message: str = None) -> None:
    """Adiciona registro de impressão ao log."""
    conn = _get_connection()
    try:
        conn.execute(
            "INSERT INTO print_logs (job_id, status, message) VALUES (?, ?, ?)",
            (job_id, status, message or ""),
        )
        conn.commit()
    finally:
        conn.close()


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
    print(f"[DEBUG] set_printers chamado com {len(printers)} impressora(s)")
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
        for p in printers
    ]
    json_data = json.dumps(list_)
    print(f"[DEBUG] JSON a ser salvo: {json_data}")
    set_config("printers", json_data)
    print(f"[DEBUG] Configuração salva no banco de dados")


def get_print_logs(limit: int = 50) -> list:
    """Retorna últimos registros de impressão."""
    conn = _get_connection()
    try:
        cursor = conn.execute(
            "SELECT id, job_id, status, message, created_at FROM print_logs "
            "ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
        return [
            {
                "id": row[0],
                "job_id": row[1],
                "status": row[2],
                "message": row[3] or "",
                "created_at": row[4],
            }
            for row in cursor.fetchall()
        ]
    finally:
        conn.close()
