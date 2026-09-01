"""Ingestão de delivery Compuchat no PDV Mais Gestão via LAN API."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple


class MaisGestaoPermanentError(Exception):
    """Erro permanente (não retry): config, produto, validação."""


class MaisGestaoOperationalError(Exception):
    """Erro operacional (retry): rede, PDV offline, timeout."""


def get_erp_target(db_module) -> str:
    raw = (db_module.get_config("erp_target") or "uniplus").strip().lower()
    if raw in ("maisgestao", "mais_gestao", "pdv"):
        return "maisgestao"
    return "uniplus"


def is_maisgestao_enabled(db_module) -> bool:
    if get_erp_target(db_module) != "maisgestao":
        return False
    url = (db_module.get_config("pdv_lan_url") or "").strip()
    return bool(url)


def _pdv_config(db_module) -> Dict[str, str]:
    return {
        "base_url": (db_module.get_config("pdv_lan_url") or "").strip().rstrip("/"),
        "email": (db_module.get_config("pdv_lan_email") or "").strip(),
        "password": (db_module.get_config("pdv_lan_password") or "").strip(),
        "token": (db_module.get_config("pdv_lan_token") or "").strip(),
    }


def _http_json(
    method: str,
    url: str,
    body: Optional[dict] = None,
    token: Optional[str] = None,
    timeout: float = 30.0,
) -> Tuple[int, Any]:
    data = None
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if body is not None:
        data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8") or "{}"
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                parsed = {"raw": raw}
            return resp.status, parsed
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace") if e.fp else ""
        try:
            parsed = json.loads(raw) if raw else {"error": str(e)}
        except json.JSONDecodeError:
            parsed = {"error": raw or str(e)}
        return e.code, parsed
    except urllib.error.URLError as e:
        raise MaisGestaoOperationalError(f"ERR_PDV_LAN: {e.reason}") from e
    except TimeoutError as e:
        raise MaisGestaoOperationalError("ERR_PDV_LAN: timeout") from e


def login_pdv(db_module) -> str:
    cfg = _pdv_config(db_module)
    if not cfg["base_url"]:
        raise MaisGestaoPermanentError(
            "ERR_PDV_CONFIG: pdv_lan_url não configurada"
        )
    if cfg["token"]:
        return cfg["token"]
    if not cfg["email"] or not cfg["password"]:
        raise MaisGestaoPermanentError(
            "ERR_PDV_CONFIG: informe pdv_lan_email/password ou pdv_lan_token"
        )
    status, body = _http_json(
        "POST",
        f"{cfg['base_url']}/pos/login",
        {"email": cfg["email"], "password": cfg["password"]},
        timeout=20.0,
    )
    if status >= 400:
        msg = body.get("error") if isinstance(body, dict) else str(body)
        raise MaisGestaoPermanentError(f"ERR_PDV_LOGIN: {msg}")
    token = (body or {}).get("token") if isinstance(body, dict) else None
    if not token:
        raise MaisGestaoPermanentError("ERR_PDV_LOGIN: token ausente na resposta")
    try:
        db_module.set_config("pdv_lan_token", str(token))
    except Exception:
        pass
    return str(token)


def validate_maisgestao_connection(db_module) -> Tuple[bool, str]:
    try:
        cfg = _pdv_config(db_module)
        if not cfg["base_url"]:
            return False, "Informe a URL da LAN do PDV (ex: http://127.0.0.1:5050)"
        status, body = _http_json(
            "GET", f"{cfg['base_url']}/pos/health", timeout=8.0
        )
        if status >= 400:
            return False, f"Health falhou ({status}): {body}"
        token = login_pdv(db_module)
        status2, ident = _http_json(
            "GET",
            f"{cfg['base_url']}/pos/pdv/identidade",
            token=token,
            timeout=8.0,
        )
        if status2 >= 400:
            return False, f"Identidade falhou ({status2}): {ident}"
        app = (ident or {}).get("app") if isinstance(ident, dict) else None
        if app and app != "pdv-mais-gestao":
            return False, f"Resposta inesperada de identidade: {app}"
        return True, "OK — PDV Mais Gestão alcançável"
    except MaisGestaoPermanentError as e:
        return False, str(e)
    except MaisGestaoOperationalError as e:
        return False, str(e)
    except Exception as e:
        return False, str(e)


def handle_maisgestao_job(db_module, conteudo: Dict[str, Any]) -> Dict[str, Any]:
    """Traduz uniplus_job / payload Compuchat → POST /pos/delivery/ingest."""
    if not is_maisgestao_enabled(db_module):
        raise MaisGestaoPermanentError(
            "ERR_PDV_CONFIG: erp_target=maisgestao e pdv_lan_url obrigatórios"
        )

    contamesa = conteudo.get("contamesa") or {}
    itens = conteudo.get("itens") or []
    protocol = str(
        conteudo.get("protocol")
        or contamesa.get("orderidintegracao")
        or ""
    ).strip()
    if not protocol:
        raise MaisGestaoPermanentError("ERR_PDV_PROTOCOL: protocolo ausente")
    if not itens:
        raise MaisGestaoPermanentError("ERR_PDV_ITENS: pedido sem itens")

    retiradanobalcao = contamesa.get("retiradanobalcao")
    modalidade = "retirada" if str(retiradanobalcao) in ("1", "true", "True") else "delivery"

    payload = {
        "protocol": protocol,
        "formResponseId": conteudo.get("formResponseId"),
        "modalidade": modalidade,
        "contamesa": contamesa,
        "itens": itens,
    }

    token = login_pdv(db_module)
    cfg = _pdv_config(db_module)
    url = f"{cfg['base_url']}/pos/delivery/ingest"

    status, body = _http_json("POST", url, payload, token=token, timeout=45.0)
    if status == 401:
        try:
            db_module.set_config("pdv_lan_token", "")
        except Exception:
            pass
        token = login_pdv(db_module)
        status, body = _http_json("POST", url, payload, token=token, timeout=45.0)

    if status >= 500:
        msg = body.get("error") if isinstance(body, dict) else str(body)
        raise MaisGestaoOperationalError(f"ERR_PDV_LAN: {msg}")
    if status >= 400:
        msg = body.get("error") if isinstance(body, dict) else str(body)
        raise MaisGestaoPermanentError(f"ERR_PDV_INGEST: {msg}")

    action = (body or {}).get("maisGestaoAction") or (body or {}).get("action") or "created"
    conta_id = (body or {}).get("maisGestaoContaId") or (body or {}).get("idconta")
    conta = (body or {}).get("conta") or {}
    return {
        "action": action,
        "conta_id": conta_id,
        "protocol": protocol,
        "cliente": contamesa.get("nomecliente") or contamesa.get("nome"),
        "itens_count": len(itens),
        "valortotal": contamesa.get("valortotal"),
        "senha": conta.get("senha_chamada"),
        "message": "already_exists" if action == "already_exists" else "created",
    }


def format_maisgestao_log_message(result: Dict[str, Any]) -> str:
    return (
        f"MaisGestão {result.get('action')} conta={result.get('conta_id')} "
        f"protocol={result.get('protocol')} itens={result.get('itens_count')}"
    )


CODIGO_COMPUCHAT_MAX = 20


def codigo_compuchat_from_pdv_produto(produto: Dict[str, Any]) -> Optional[str]:
    ean = str(produto.get("ean") or "").strip()
    if ean and len(ean) <= CODIGO_COMPUCHAT_MAX:
        return ean
    raw = produto.get("codigo")
    if raw is not None and str(raw).strip() != "":
        try:
            if isinstance(raw, float):
                codigo = str(int(raw)) if raw == int(raw) else str(raw).strip()
            else:
                codigo = (
                    str(int(raw))
                    if str(raw).strip().isdigit()
                    else str(raw).strip()
                )
        except (TypeError, ValueError):
            codigo = str(raw).strip()
        if codigo and len(codigo) <= CODIGO_COMPUCHAT_MAX and not codigo.startswith("?"):
            return codigo
    return None


def fetch_pdv_catalog(db_module) -> Dict[str, Any]:
    if not is_maisgestao_enabled(db_module):
        raise MaisGestaoPermanentError(
            "ERR_PDV_CONFIG: configure erp_target=maisgestao e pdv_lan_url"
        )
    token = login_pdv(db_module)
    cfg = _pdv_config(db_module)
    url = f"{cfg['base_url']}/pos/pdv/catalogo"
    status, body = _http_json("GET", url, token=token, timeout=60.0)
    if status == 401:
        try:
            db_module.set_config("pdv_lan_token", "")
        except Exception:
            pass
        token = login_pdv(db_module)
        status, body = _http_json("GET", url, token=token, timeout=60.0)
    if status >= 500:
        msg = body.get("error") if isinstance(body, dict) else str(body)
        raise MaisGestaoOperationalError(f"ERR_PDV_CATALOGO: {msg}")
    if status >= 400:
        msg = body.get("error") if isinstance(body, dict) else str(body)
        raise MaisGestaoPermanentError(f"ERR_PDV_CATALOGO: {msg}")
    return body if isinstance(body, dict) else {}


def list_pdv_products(
    db_module, q: str = "", limit: int = 500
) -> List[Dict[str, Any]]:
    catalog = fetch_pdv_catalog(db_module)
    produtos = catalog.get("produtos") or []
    q_norm = (q or "").strip().lower()
    limit = max(1, min(int(limit or 500), 5000))
    out: List[Dict[str, Any]] = []
    for raw in produtos:
        if not isinstance(raw, dict):
            continue
        codigo = codigo_compuchat_from_pdv_produto(raw)
        if not codigo:
            continue
        nome = str(raw.get("descricao") or "").strip()
        try:
            preco = float(raw.get("preco") or 0)
        except (TypeError, ValueError):
            preco = 0.0
        atualizado = raw.get("atualizadoem") or catalog.get("atualizadoem")
        row = {
            "codigo": codigo,
            "nome": nome,
            "preco": preco,
            "dataalteracao": atualizado,
            "inativo": 0,
            "id_pdv": str(raw.get("id") or "").strip() or None,
            "ean": str(raw.get("ean") or "").strip() or None,
            "codigo_interno": raw.get("codigo"),
            "fingerprint": f"{nome}|{preco:.4f}|{atualizado or ''}",
        }
        if q_norm:
            hay = f"{codigo} {nome} {row.get('ean') or ''} {row.get('id_pdv') or ''}".lower()
            if q_norm not in hay:
                continue
        out.append(row)
        if len(out) >= limit:
            break
    return out


def fetch_pdv_product(db_module, codigo: str) -> Optional[Dict[str, Any]]:
    codigo = str(codigo or "").strip()
    if not codigo:
        return None
    for p in list_pdv_products(db_module, q=codigo, limit=5000):
        if str(p.get("codigo") or "").strip() == codigo:
            return p
    return None

