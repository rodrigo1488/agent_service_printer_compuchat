"""Sync inteligente de produtos UniPlus → Compuchat."""
from __future__ import annotations

import json
import logging
import ssl
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
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


def _ssl_unverified_context() -> ssl.SSLContext:
    """Mesmo critério do WebSocket: cert autoassinado/cadeia incompleta no SaaS."""
    ctx = ssl._create_unverified_context()
    ctx.check_hostname = False
    return ctx

# Intervalo alto de propósito — poll contínuo conflita com o Unico no mesmo Postgres.
POLL_INTERVAL_SEC = 300
UPSERT_CHUNK_SIZE = 100
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


def _compuchat_request(
    method: str,
    path: str,
    *,
    body: Optional[Dict[str, Any]] = None,
    query: Optional[Dict[str, str]] = None,
    timeout: int = 30,
) -> Dict[str, Any]:
    device_id, token = _device_auth()
    if not device_id or not token:
        raise RuntimeError("Configure device_id e token de uma impressora no PrintAgent")
    base = _api_base_from_ws(db.get_config("ws_url") or "")
    if not base:
        raise RuntimeError("ws_url não configurada")
    url = f"{base}{path}"
    if query:
        qs = urllib.parse.urlencode(
            {k: v for k, v in query.items() if v is not None and v != ""}
        )
        if qs:
            url = f"{url}?{qs}"
    data = None
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Device-Id": device_id,
    }
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(
            req, timeout=max(15, int(timeout)), context=_ssl_unverified_context()
        ) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:400]
        raise RuntimeError(f"HTTP {e.code}: {detail}") from e


def list_compuchat_catalog(
    *, q: str = "", limit: int = 1000
) -> Dict[str, Any]:
    """Uma única carga de GET /agent/products: products, addOns flat e addOnGroups."""
    data = _compuchat_request(
        "GET",
        "/agent/products",
        query={"q": (q or "").strip(), "limit": str(limit)},
        timeout=30,
    )
    return {
        "products": list(data.get("products") or []),
        "addOns": list(data.get("addOns") or []),
        "addOnGroups": list(data.get("addOnGroups") or []),
    }


def list_compuchat_products(
    *, q: str = "", limit: int = 1000
) -> List[Dict[str, Any]]:
    """Lista Products Compuchat (para escolher produto pai/avulso e anotar status)."""
    return list_compuchat_catalog(q=q, limit=limit)["products"]


def list_compuchat_addons(*, limit: int = 1000) -> List[Dict[str, Any]]:
    """Lista AddOnItems (adicionais) Compuchat, achatados (grupo/subgrupo já
    resolvidos), pra escolher no modo "Adicional" do modal de vínculo."""
    return list_compuchat_catalog(limit=limit)["addOns"]


def attach_variation_to_parent(
    *,
    codigo: str,
    parent_product_id: int,
    variation_name: str = "Tamanho",
    option_label: str = "",
    preco: Optional[float] = None,
    parent_grupo: str = "",
) -> Dict[str, Any]:
    """Anexa codigo UniPlus como opção de variação no produto pai Compuchat."""
    body: Dict[str, Any] = {
        "codigo": str(codigo or "").strip()[:20],
        "parentProductId": int(parent_product_id),
        "variationName": (variation_name or "Tamanho").strip() or "Tamanho",
        "optionLabel": (option_label or "").strip(),
    }
    if preco is not None:
        body["preco"] = float(preco)
    if (parent_grupo or "").strip():
        body["parentGrupo"] = parent_grupo.strip()
    return _compuchat_request(
        "POST", "/agent/products/attach-variation", body=body, timeout=45
    )


def link_standalone(
    *,
    codigo: str,
    nome: str,
    preco: float,
    grupo: str = "",
    product_id: Optional[int] = None,
) -> Dict[str, Any]:
    """Vincula codigo UniPlus a um produto avulso (novo ou existente) no Compuchat."""
    body: Dict[str, Any] = {
        "codigo": str(codigo or "").strip()[:20],
        "nome": str(nome or "").strip(),
        "preco": float(preco or 0),
        "grupo": (grupo or "").strip(),
    }
    if product_id:
        body["productId"] = int(product_id)
    return _compuchat_request(
        "POST", "/agent/products/link-standalone", body=body, timeout=45
    )


def link_addon(
    *,
    codigo: str,
    add_on_item_id: Optional[int] = None,
    add_on_group_id: Optional[int] = None,
    label: str = "",
    value: Optional[float] = None,
) -> Dict[str, Any]:
    """Vincula codigo UniPlus a um adicional existente, ou cria item num grupo."""
    body: Dict[str, Any] = {
        "codigo": str(codigo or "").strip()[:20],
    }
    if add_on_item_id:
        body["addOnItemId"] = int(add_on_item_id)
    if add_on_group_id:
        body["addOnGroupId"] = int(add_on_group_id)
    if (label or "").strip():
        body["label"] = label.strip()
    if value is not None:
        body["value"] = float(value)
    return _compuchat_request(
        "POST", "/agent/products/link-addon", body=body, timeout=45
    )


def unlink_codigo(*, codigo: str) -> Dict[str, Any]:
    """Remove qualquer vinculo (avulso ou variação) do codigo no Compuchat."""
    body: Dict[str, Any] = {"codigo": str(codigo or "").strip()[:20]}
    return _compuchat_request(
        "POST", "/agent/products/unlink", body=body, timeout=30
    )


def create_parent_product(
    *, nome: str, grupo: str = "", preco: float = 0.0
) -> Dict[str, Any]:
    """Cria um Product Compuchat sem codigo próprio, só pra servir de pai de variação."""
    body: Dict[str, Any] = {
        "nome": str(nome or "").strip(),
        "grupo": (grupo or "").strip(),
        "preco": float(preco or 0),
    }
    return _compuchat_request(
        "POST", "/agent/products/create-parent", body=body, timeout=30
    )


def suggest_option_label(nome: str, codigo: str = "") -> str:
    tokens = [t for t in str(nome or "").strip().split() if t]
    last = tokens[-1] if tokens else ""
    if last and last.upper() in {"P", "M", "G", "GG", "PP", "XG", "XP"}:
        return last.upper()
    return last or str(codigo or "").strip()


def annotate_link_status(
    products: List[Dict[str, Any]],
    parents: List[Dict[str, Any]],
    addons: Optional[List[Dict[str, Any]]] = None,
) -> None:
    """
    Cruza cada produto UniPlus com o catálogo Compuchat (parents/addons, já
    carregados) e anota em-place `p["link_status"]`:
      - {"kind": "unlinked"}
      - {"kind": "standalone", "productId", "productName", "grupo"}
      - {"kind": "variation", "parentId", "parentName", "parentGrupo",
         "variationId", "variationName", "optionId", "optionLabel"}
      - {"kind": "addon", "addOnItemId", "label", "groupName", "subgroupName"}
    """
    standalone_map: Dict[str, Dict[str, Any]] = {}
    option_map: Dict[str, Dict[str, Any]] = {}
    addon_map: Dict[str, Dict[str, Any]] = {}
    for addon in addons or []:
        a_codigo = str(addon.get("idUniplus") or "").strip()
        if not a_codigo:
            continue
        addon_map[a_codigo] = {
            "addOnItemId": addon.get("id"),
            "label": addon.get("label"),
            "groupName": addon.get("groupName"),
            "subgroupName": addon.get("subgroupName"),
        }
    for parent in parents:
        p_codigo = str(parent.get("idUniplus") or "").strip()
        if p_codigo:
            standalone_map[p_codigo] = {
                "productId": parent.get("id"),
                "productName": parent.get("name"),
                "grupo": parent.get("grupo"),
            }
        for variation in parent.get("variations") or []:
            for option in variation.get("options") or []:
                o_codigo = str(option.get("idUniplus") or "").strip()
                if not o_codigo:
                    continue
                option_map[o_codigo] = {
                    "parentId": parent.get("id"),
                    "parentName": parent.get("name"),
                    "parentGrupo": parent.get("grupo"),
                    "variationId": variation.get("id"),
                    "variationName": variation.get("name"),
                    "optionId": option.get("id"),
                    "optionLabel": option.get("label"),
                }

    for p in products:
        codigo = str(p.get("codigo") or "").strip()
        if codigo in option_map:
            p["link_status"] = {"kind": "variation", **option_map[codigo]}
        elif codigo in standalone_map:
            p["link_status"] = {"kind": "standalone", **standalone_map[codigo]}
        elif codigo in addon_map:
            p["link_status"] = {"kind": "addon", **addon_map[codigo]}
        else:
            p["link_status"] = {"kind": "unlinked"}


def split_pending_and_linked(
    products: List[Dict[str, Any]]
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Separa produtos UniPlus (já anotados por annotate_link_status) em
    (pendentes, vinculados) conforme link_status.kind."""
    pending: List[Dict[str, Any]] = []
    linked: List[Dict[str, Any]] = []
    for p in products:
        status = p.get("link_status") or {"kind": "unlinked"}
        if status.get("kind") == "unlinked":
            pending.append(p)
        else:
            linked.append(p)
    return pending, linked


def build_linked_cards(linked_products: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Agrupa produtos UniPlus já vinculados pelo productId Compuchat *real*
    (productId do avulso ou parentId da variação — o que for o mesmo produto
    cai no mesmo card). Elimina a duplicação visual de um mesmo produto
    Compuchat aparecendo em duas linhas separadas (avulso + pai de variação).

    Adicionais (kind == "addon") ganham um card próprio por addOnItemId —
    são um catálogo separado de Product, então nunca se misturam com
    variações/avulsos de produto.

    Retorna cards no formato:
      {"productId", "productName", "grupo", "standalone": <item ou None>,
       "variations": [{"variationId", "variationName", "options": [...items...]}],
       "isAddOn": bool}
    """
    cards: Dict[Any, Dict[str, Any]] = {}

    for p in linked_products:
        status = p.get("link_status") or {}
        kind = status.get("kind")
        if kind == "addon":
            product_id = ("addon", status.get("addOnItemId"))
            name = status.get("label") or f"Adicional #{status.get('addOnItemId')}"
            group_bits = [
                b for b in [status.get("groupName"), status.get("subgroupName")] if b
            ]
            grupo = " / ".join(group_bits) or None
            card = {
                "productId": product_id,
                "productName": name,
                "grupo": grupo,
                "standalone": p,
                "variations": [],
                "isAddOn": True,
                "codesCount": 1,
            }
            cards[product_id] = card
            continue
        if kind == "standalone":
            product_id = status.get("productId")
            name = status.get("productName") or f"Produto #{product_id}"
            grupo = status.get("grupo")
        elif kind == "variation":
            product_id = status.get("parentId")
            name = status.get("parentName") or f"Produto #{product_id}"
            grupo = status.get("parentGrupo")
        else:
            continue

        card = cards.get(product_id)
        if not card:
            card = {
                "productId": product_id,
                "productName": name,
                "grupo": grupo,
                "standalone": None,
                "variations": [],
            }
            cards[product_id] = card
        # Nome sempre reflete a fonte mais recente (standalone tende a ser a
        # mais confiável, já que "é" o próprio produto).
        if kind == "standalone" or not card.get("productName"):
            card["productName"] = name
        if grupo:
            card["grupo"] = grupo

        if kind == "standalone":
            card["standalone"] = p
        else:
            variation_id = status.get("variationId")
            variation_group = next(
                (v for v in card["variations"] if v.get("variationId") == variation_id),
                None,
            )
            if not variation_group:
                variation_group = {
                    "variationId": variation_id,
                    "variationName": status.get("variationName") or "Variação",
                    "options": [],
                }
                card["variations"].append(variation_group)
            variation_group["options"].append(p)

    def option_sort_key(item: Dict[str, Any]) -> Tuple[int, int, float]:
        label = (item.get("link_status") or {}).get("optionLabel") or ""
        rank = SIZE_RANK.get(_normalize_size_token(label))
        return (
            0 if rank is not None else 1,
            rank if rank is not None else 0,
            float(item.get("preco") or 0),
        )

    result = list(cards.values())
    for card in result:
        card["variations"].sort(key=lambda v: str(v.get("variationName") or "").lower())
        for variation_group in card["variations"]:
            variation_group["options"].sort(key=option_sort_key)
        card["codesCount"] = (1 if card.get("standalone") else 0) + sum(
            len(v["options"]) for v in card["variations"]
        )
    result.sort(key=lambda c: str(c.get("productName") or "").lower())
    return result


# Tokens de tamanho conhecidos (normalizados: maiúsculas, sem acento) e sua
# ordem relativa, usada pra ordenar clusters/opções (P < M < G < GG). Tokens
# fora dessa lista nunca formam cluster — heurística conservadora, sem
# falso-positivo.
SIZE_RANK: Dict[str, int] = {
    "PP": 0,
    "XP": 0,
    "P": 1,
    "PEQUENO": 1,
    "PEQUENA": 1,
    "BROTINHO": 1,
    "INDIVIDUAL": 1,
    "M": 2,
    "MEDIO": 2,
    "MEDIA": 2,
    "G": 3,
    "GRANDE": 3,
    "GG": 4,
    "XG": 4,
    "JUMBO": 4,
    "FAMILIA": 4,
}


def _normalize_size_token(token: str) -> str:
    token = str(token or "").strip().strip(".,-").upper()
    token = unicodedata.normalize("NFKD", token)
    token = "".join(ch for ch in token if not unicodedata.combining(ch))
    return token


def detect_size_token(nome: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Procura um token de tamanho conhecido no fim do nome (ex.: "Pizza Calabresa
    Grande" → ("Pizza Calabresa", "Grande")). Retorna (None, None) se o último
    token não for um tamanho reconhecido, evitando falso-positivo.
    """
    nome = str(nome or "").strip()
    if not nome:
        return None, None
    tokens = nome.split()
    if len(tokens) < 2:
        return None, None
    last = tokens[-1]
    if _normalize_size_token(last) not in SIZE_RANK:
        return None, None
    base = " ".join(tokens[:-1]).strip()
    if not base:
        return None, None
    return base, last


def suggest_flavor_clusters(pending_products: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Olha só itens pendentes (link_status.kind == "unlinked") e agrupa por
    nome-base normalizado quando 2+ códigos compartilham o mesmo nome-base
    com tamanhos diferentes — sugestão de "isso parece ser o mesmo sabor em
    tamanhos diferentes". Cada cluster já vem ordenado por tamanho
    (P < M < G < GG, fallback por preço).

    Retorna [{"baseName", "codes": [...items anotados com size_label...]}].
    """
    groups: Dict[str, Dict[str, Any]] = {}

    for p in pending_products:
        status = p.get("link_status") or {}
        if status.get("kind") != "unlinked":
            continue
        base, size_label = detect_size_token(p.get("nome") or "")
        if not base or not size_label:
            continue
        key = _normalize_size_token(base) or base.strip().upper()
        group = groups.get(key)
        if not group:
            group = {"baseName": base, "codes": []}
            groups[key] = group
        group["codes"].append({**p, "size_label": size_label})

    def sort_key(item: Dict[str, Any]) -> Tuple[int, int, float]:
        rank = SIZE_RANK.get(_normalize_size_token(item.get("size_label") or ""))
        return (
            0 if rank is not None else 1,
            rank if rank is not None else 0,
            float(item.get("preco") or 0),
        )

    clusters = [g for g in groups.values() if len(g["codes"]) >= 2]
    for cluster in clusters:
        cluster["codes"].sort(key=sort_key)
    clusters.sort(key=lambda c: str(c.get("baseName") or "").lower())
    return clusters


def distinct_grupos(parents: List[Dict[str, Any]]) -> List[str]:
    """Lista ordenada de grupos (categorias) já usados, para sugestão no combo."""
    grupos = {
        str(p.get("grupo") or "").strip()
        for p in parents
        if str(p.get("grupo") or "").strip()
    }
    return sorted(grupos, key=lambda s: s.lower())


def upsert_to_compuchat(
    products: List[Dict[str, Any]], *, timeout: int = 30
) -> Dict[str, Any]:
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
        with urllib.request.urlopen(
            req, timeout=max(15, int(timeout)), context=_ssl_unverified_context()
        ) as resp:
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


def _upsert_chunk_and_update_local(
    products: List[Dict[str, Any]],
) -> Tuple[int, int, List[str]]:
    """Envia lote ao Compuchat e atualiza SQLite. Retorna (ok, fail, erros_amostra)."""
    if not products:
        return 0, 0, []
    ok = 0
    fail = 0
    errors: List[str] = []
    timeout = min(120, 20 + len(products) * 2)
    try:
        data = upsert_to_compuchat(products, timeout=timeout)
        by_code = {
            str(r.get("codigo") or "").strip(): r for r in (data.get("results") or [])
        }
        for p in products:
            codigo = str(p.get("codigo") or "").strip()
            result = by_code.get(codigo) or {}
            if result.get("error"):
                fail += 1
                err = str(result["error"])
                db.update_sync_product_state(
                    codigo,
                    nome=p.get("nome") or "",
                    preco=float(p.get("preco") or 0),
                    last_error=err,
                )
                if len(errors) < 5:
                    errors.append(f"{codigo}: {err}")
            else:
                ok += 1
                db.update_sync_product_state(
                    codigo,
                    nome=p.get("nome") or "",
                    preco=float(p.get("preco") or 0),
                    fingerprint=p.get("fingerprint") or make_fingerprint(
                        p.get("nome") or "", float(p.get("preco") or 0), p.get("dataalteracao")
                    ),
                    last_error="",
                    synced=True,
                )
    except Exception as e:
        err = str(e)
        fail = len(products)
        for p in products:
            db.update_sync_product_state(
                str(p.get("codigo") or "").strip(), last_error=err
            )
        errors.append(err)
    return ok, fail, errors


def upsert_many(products: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Upsert em chunks de até 100 (limite do backend Compuchat)."""
    total_ok = 0
    total_fail = 0
    errors: List[str] = []
    for i in range(0, len(products), UPSERT_CHUNK_SIZE):
        chunk = products[i : i + UPSERT_CHUNK_SIZE]
        ok, fail, chunk_errors = _upsert_chunk_and_update_local(chunk)
        total_ok += ok
        total_fail += fail
        for err in chunk_errors:
            if len(errors) < 5:
                errors.append(err)
    return {
        "ok": total_fail == 0,
        "synced": total_ok,
        "failed": total_fail,
        "total": len(products),
        "errors": errors,
    }


def enable_all_products(q: str = "", limit: int = 2000) -> Dict[str, Any]:
    """Marca sync ON em todos os produtos listados (filtro q) e faz upsert imediato."""
    if not is_uniplus_enabled(db):
        raise RuntimeError("UniPlus desativado na configuração")
    products = list_uniplus_products(q=q, limit=limit)
    # Sync exige código válido (não placeholders)
    products = [
        p
        for p in products
        if str(p.get("codigo") or "").strip()
        and not str(p.get("codigo") or "").startswith("?")
    ]
    if not products:
        return {
            "ok": True,
            "enabled": 0,
            "synced": 0,
            "failed": 0,
            "total": 0,
            "errors": [],
            "message": "Nenhum produto para adicionar",
        }

    for p in products:
        db.set_sync_product_enabled(
            p["codigo"],
            True,
            nome=p.get("nome") or "",
            preco=float(p.get("preco") or 0),
        )

    result = upsert_many(products)
    result["enabled"] = len(products)
    return result


def sync_all_products(q: str = "", limit: int = 2000) -> Dict[str, Any]:
    """
    Força sync de todos os produtos com sync automático ON.
    Se q for informado, restringe aos códigos que batem com a busca UniPlus atual.
    """
    if not is_uniplus_enabled(db):
        raise RuntimeError("UniPlus desativado na configuração")

    enabled = db.list_sync_products(enabled_only=True)
    if not enabled:
        return {
            "ok": True,
            "synced": 0,
            "failed": 0,
            "total": 0,
            "errors": [],
            "message": "Nenhum produto em sync automático",
        }

    if q:
        listed = {
            str(p.get("codigo") or "").strip()
            for p in list_uniplus_products(q=q, limit=limit)
        }
        enabled = [item for item in enabled if item["codigo"] in listed]

    if not enabled:
        return {
            "ok": True,
            "synced": 0,
            "failed": 0,
            "total": 0,
            "errors": [],
            "message": "Nenhum produto em sync automático no filtro atual",
        }

    remotes: List[Dict[str, Any]] = []
    missing = 0
    for item in enabled:
        codigo = item["codigo"]
        remote = fetch_uniplus_product(codigo)
        if not remote:
            missing += 1
            db.update_sync_product_state(
                codigo, last_error="produto não encontrado no UniPlus"
            )
            continue
        remotes.append(remote)

    result = upsert_many(remotes)
    result["failed"] = int(result.get("failed") or 0) + missing
    result["total"] = len(enabled)
    result["missing"] = missing
    if missing and len(result.get("errors") or []) < 5:
        result.setdefault("errors", []).append(
            f"{missing} produto(s) não encontrado(s) no UniPlus"
        )
    result["ok"] = int(result.get("failed") or 0) == 0
    return result


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


def is_product_sync_poll_enabled() -> bool:
    """Poll contínuo fica OFF por padrão — Unico reclama de conexão concorrente no Postgres."""
    raw = (db.get_config("uniplus_product_sync_poll") or "false").lower()
    return raw in ("true", "1", "yes", "on") and is_uniplus_enabled(db)


def start_product_sync_thread() -> None:
    """Só inicia o poller se explicitamente habilitado na config."""
    global _sync_thread, _should_stop
    if not is_product_sync_poll_enabled():
        logger.info(
            "product_sync poll desligado (uniplus_product_sync_poll=false) — "
            "sync só sob demanda na tela Produtos / jobs UniPlus"
        )
        return
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


def refresh_product_sync_thread() -> None:
    """Liga/desliga o poller conforme a config atual (após salvar)."""
    if is_product_sync_poll_enabled():
        start_product_sync_thread()
    else:
        stop_product_sync_thread()
        logger.info("product_sync poll parado")
