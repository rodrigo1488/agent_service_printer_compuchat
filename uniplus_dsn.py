"""DSN do Postgres UniPlus a partir de host, porta, usuário, senha e banco.

A interface grava os campos separados. O URI é montado aqui e também
persistido em uniplus_connection_string para o código que ainda lê o DSN.
"""
from __future__ import annotations

import re
from typing import Callable, Dict
from urllib.parse import parse_qs, quote, unquote, urlparse

DEFAULT_PORT = 5432
DEFAULT_DATABASE = "unico"

_KW_RE = re.compile(
    r"""([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?:'((?:\\'|[^'])*)'|"((?:\\"|[^"])*)"|(\S+))"""
)


def normalize_uniplus_port(port) -> int:
    text = str(DEFAULT_PORT if port is None else port).strip()
    if not text:
        return DEFAULT_PORT
    try:
        number = int(text)
    except (TypeError, ValueError):
        raise ValueError("Porta inválida") from None
    if not 1 <= number <= 65535:
        raise ValueError("Porta inválida")
    return number


def _host_for_uri(host: str) -> str:
    value = (host or "").strip()
    if not value:
        return ""
    if any(ch in value for ch in " \t\r\n@/?#"):
        raise ValueError("Host inválido")
    if value.startswith("[") and value.endswith("]"):
        return value
    if value.count(":") == 1:
        raise ValueError("Informe a porta no campo Porta, não no host")
    if ":" in value:
        return f"[{value}]"
    return value


def build_uniplus_dsn(
    host: str,
    port=DEFAULT_PORT,
    user: str = "",
    password: str = "",
    database: str = DEFAULT_DATABASE,
) -> str:
    """Monta um URI postgresql:// com usuário e senha percent-encoded."""
    host_part = _host_for_uri(host)
    if not host_part:
        return ""
    port_n = normalize_uniplus_port(port)
    db_name = (database or "").strip() or DEFAULT_DATABASE
    db_part = quote(db_name, safe="")
    user_s = user or ""
    password_s = password or ""
    if user_s or password_s:
        user_q = quote(user_s, safe="")
        if password_s:
            auth = f"{user_q}:{quote(password_s, safe='')}@"
        else:
            auth = f"{user_q}@"
    else:
        auth = ""
    return f"postgresql://{auth}{host_part}:{port_n}/{db_part}"


def _empty_fields() -> Dict[str, str]:
    return {
        "host": "",
        "port": "",
        "user": "",
        "password": "",
        "database": "",
    }


def _q1(params: dict, *keys: str) -> str:
    for key in keys:
        values = params.get(key)
        if values:
            return unquote(values[0] or "")
    return ""


def _parse_uri(dsn: str) -> Dict[str, str]:
    parsed = urlparse(dsn)
    if parsed.scheme not in ("postgresql", "postgres"):
        return _empty_fields()
    params = parse_qs(parsed.query, keep_blank_values=True)
    user = unquote(parsed.username) if parsed.username else _q1(params, "user", "username")
    if parsed.password is None:
        password = _q1(params, "password")
    else:
        password = unquote(parsed.password)
    host = parsed.hostname or _q1(params, "host", "hostaddr")
    try:
        port = parsed.port
    except ValueError:
        port = None
    if port is None:
        raw_port = _q1(params, "port")
        port = raw_port if raw_port else str(DEFAULT_PORT)
    database = ""
    if parsed.path and parsed.path not in ("/", ""):
        database = unquote(parsed.path.lstrip("/").split("/")[0])
    if not database:
        database = _q1(params, "dbname", "database") or DEFAULT_DATABASE
    return {
        "host": host or "",
        "port": str(port),
        "user": user or "",
        "password": password or "",
        "database": database or DEFAULT_DATABASE,
    }


def _parse_keywords(dsn: str) -> Dict[str, str]:
    found = {}
    for match in _KW_RE.finditer(dsn):
        key = match.group(1).lower()
        if match.group(2) is not None:
            value = match.group(2).replace("\\'", "'")
        elif match.group(3) is not None:
            value = match.group(3).replace('\\"', '"')
        else:
            value = match.group(4) or ""
        found[key] = value
    if "host" not in found and "hostaddr" not in found:
        return _empty_fields()
    return {
        "host": found.get("host") or found.get("hostaddr") or "",
        "port": found.get("port") or str(DEFAULT_PORT),
        "user": found.get("user") or found.get("username") or "",
        "password": found.get("password") or "",
        "database": found.get("dbname") or found.get("database") or DEFAULT_DATABASE,
    }


def parse_uniplus_dsn(dsn: str) -> Dict[str, str]:
    """Lê um URI ou um conninfo libpq. Não registra o valor."""
    raw = (dsn or "").strip()
    if not raw:
        return _empty_fields()
    if "://" in raw:
        return _parse_uri(raw)
    if "=" in raw:
        return _parse_keywords(raw)
    return _empty_fields()


def resolve_uniplus_password(submitted: str, stored: str) -> str:
    """Campo de senha vazio mantém a senha já gravada."""
    if submitted is None or submitted == "":
        return stored or ""
    return submitted


def ensure_uniplus_fields_from_connection_string(get_config: Callable, set_config: Callable) -> bool:
    """Preenche os campos uma vez a partir do DSN antigo, sem reescrever o DSN."""
    host = (get_config("uniplus_host") or "").strip()
    user = (get_config("uniplus_user") or "").strip()
    password = get_config("uniplus_password") or ""
    if host or user or password:
        return False
    dsn = (get_config("uniplus_connection_string") or "").strip()
    if not dsn:
        return False
    parsed = parse_uniplus_dsn(dsn)
    if not (parsed.get("host") or "").strip():
        return False
    set_config("uniplus_host", parsed["host"])
    set_config("uniplus_port", str(parsed.get("port") or DEFAULT_PORT))
    set_config("uniplus_user", parsed.get("user") or "")
    set_config("uniplus_password", parsed.get("password") or "")
    set_config("uniplus_database", parsed.get("database") or DEFAULT_DATABASE)
    return True


def current_uniplus_dsn(db_module) -> str:
    """DSN efetivo: montado dos campos quando o host existe; senão o valor salvo."""
    stored = (db_module.get_config("uniplus_connection_string") or "").strip()
    host = (db_module.get_config("uniplus_host") or "").strip()
    if not host:
        return stored
    try:
        built = build_uniplus_dsn(
            host,
            db_module.get_config("uniplus_port") or DEFAULT_PORT,
            db_module.get_config("uniplus_user") or "",
            db_module.get_config("uniplus_password") or "",
            db_module.get_config("uniplus_database") or DEFAULT_DATABASE,
        )
    except ValueError:
        return stored
    return built or stored


def redact_connection_details(text: str, dsn: str = "", password: str = "") -> str:
    """Tira DSN e senha de mensagens antes de mostrar ou gravar log."""
    out = str(text or "")
    if dsn:
        out = out.replace(dsn, "postgresql://…")
    if password and len(password) >= 3:
        out = out.replace(password, "****")
        encoded = quote(password, safe="")
        if encoded and encoded != password:
            out = out.replace(encoded, "****")
    return out
