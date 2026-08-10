"""Handler UniPlus: grava delivery aberto em CONTAMESA / CONTAMESAITEM no Postgres local."""
from __future__ import annotations

import logging
import re
import uuid
import unicodedata
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Set, Tuple

logger = logging.getLogger("uniplus")

try:
    import psycopg2
    from psycopg2 import OperationalError, InterfaceError, IntegrityError
    from psycopg2.extras import RealDictCursor
except ImportError:
    psycopg2 = None
    OperationalError = Exception  # type: ignore
    InterfaceError = Exception  # type: ignore
    IntegrityError = Exception  # type: ignore
    RealDictCursor = None  # type: ignore

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
CONNECT_TIMEOUT_SEC = 8
STATEMENT_TIMEOUT_MS = 15000
# Fallback CONTAMESAITEM.idunidademedida quando o produto UniPlus não tem unidade
DEFAULT_IDUNIDADEMEDIDA = 30


class UniplusPermanentError(RuntimeError):
    """Erro que não deve ser retentado pelo SaaS."""


def _pad_hash(value: str) -> str:
    """UUID com hífens (Java UUID.fromString) + pad CHAR(40).

    Hash sem hífen falha no parse do UniPlus → ContaCliente.hash=null →
    NPE em OperacaoDAO.existeOperacaoPendenteUnichef ao abrir/finalizar delivery.
    """
    s = (value or "").strip()
    hex_only = s.replace("-", "").replace(" ", "")
    if len(hex_only) == 32 and all(c in "0123456789abcdefABCDEF" for c in hex_only):
        s = (
            f"{hex_only[:8]}-{hex_only[8:12]}-{hex_only[12:16]}-"
            f"{hex_only[16:20]}-{hex_only[20:32]}"
        )
    elif not s:
        s = str(uuid.uuid4())
    return (s + (" " * 40))[:40]


def _safe_ident(name: str, default: str) -> str:
    value = (name or default or "").strip()
    if not _IDENT_RE.match(value):
        raise UniplusPermanentError(
            f"ERR_UNIPLUS_CONFIG: identificador SQL inválido '{name}'"
        )
    return value


def _cfg(db_module) -> Dict[str, str]:
    return {
        "enabled": (db_module.get_config("uniplus_enabled") or "false").lower(),
        "connection_string": (db_module.get_config("uniplus_connection_string") or "").strip(),
        "produto_table": _safe_ident(
            db_module.get_config("uniplus_produto_table") or "produto", "produto"
        ),
        "produto_codigo_column": _safe_ident(
            db_module.get_config("uniplus_produto_codigo_column") or "codigo", "codigo"
        ),
        "produto_id_column": _safe_ident(
            db_module.get_config("uniplus_produto_id_column") or "id", "id"
        ),
        "contamesa_table": _safe_ident(
            db_module.get_config("uniplus_contamesa_table") or "contamesa", "contamesa"
        ),
        "contamesaitem_table": _safe_ident(
            db_module.get_config("uniplus_contamesaitem_table") or "contamesaitem",
            "contamesaitem",
        ),
    }


def is_uniplus_enabled(db_module) -> bool:
    enabled = (db_module.get_config("uniplus_enabled") or "false").lower()
    dsn = (db_module.get_config("uniplus_connection_string") or "").strip()
    return enabled in ("true", "1", "yes", "on") and bool(dsn)


def validate_uniplus_connection(
    connection_string: str,
    contamesa_table: str = "contamesa",
    contamesaitem_table: str = "contamesaitem",
) -> Tuple[bool, str]:
    """Testa DSN + existência das tabelas. Usado no save/health do agente."""
    if psycopg2 is None:
        return False, "psycopg2 não instalado"
    dsn = (connection_string or "").strip()
    if not dsn:
        return False, "connection string vazia"
    try:
        mesa = _safe_ident(contamesa_table, "contamesa")
        item = _safe_ident(contamesaitem_table, "contamesaitem")
    except UniplusPermanentError as e:
        return False, str(e)
    try:
        conn = psycopg2.connect(dsn, connect_timeout=CONNECT_TIMEOUT_SEC)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.execute(
                    """
                    SELECT to_regclass(%s) AS mesa, to_regclass(%s) AS item
                    """,
                    (mesa, item),
                )
                row = cur.fetchone()
                if not row or not row[0]:
                    return False, f"tabela {mesa} não encontrada"
                if not row[1]:
                    return False, f"tabela {item} não encontrada"
        finally:
            conn.close()
        return True, "ok"
    except Exception as e:
        return False, str(e)


def _connect(dsn: str):
    """Abre conexão curta. Sempre fechar no finally do caller — Unico não tolera sessão concorrente."""
    conn = psycopg2.connect(dsn, connect_timeout=CONNECT_TIMEOUT_SEC)
    with conn.cursor() as cur:
        cur.execute(f"SET statement_timeout = {int(STATEMENT_TIMEOUT_MS)}")
        try:
            cur.execute('SET search_path TO public, unico, "$user"')
        except Exception:
            pass
        try:
            cur.execute("SET application_name = 'compuchat_print_agent'")
        except Exception:
            pass
    return conn


def _normalize_nome(nome: str) -> str:
    s = unicodedata.normalize("NFD", str(nome or ""))
    s = "".join(ch for ch in s if unicodedata.category(ch) != "Mn")
    s = s.lower()
    s = re.sub(r"\b\d+\s*ml\b", " ", s)
    s = re.sub(r"\b\d+\s*l\b", " ", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _produto_nome_column(cur, table: str) -> str:
    """Detecta coluna de nome no cadastro de produto."""
    cur.execute(
        """
        SELECT lower(column_name) AS col
        FROM information_schema.columns
        WHERE lower(table_name) = lower(%s)
          AND table_schema = current_schema()
          AND lower(column_name) IN ('nome', 'descricao', 'produto', 'nomefantasia')
        """,
        (table,),
    )
    rows = cur.fetchall() or []
    cols = set()
    for row in rows:
        cols.add(str(row["col"] if isinstance(row, dict) else row[0]).lower())
    for preferred in ("nome", "descricao", "produto", "nomefantasia"):
        if preferred in cols:
            return preferred
    return ""


def _produto_has_column(cur, table: str, column: str) -> bool:
    cur.execute(
        """
        SELECT 1
        FROM information_schema.columns
        WHERE lower(table_name) = lower(%s)
          AND table_schema NOT IN ('pg_catalog', 'information_schema')
          AND lower(column_name) = lower(%s)
        LIMIT 1
        """,
        (table, column),
    )
    return bool(cur.fetchone())


def _fetch_produto_unidademedida(cur, table: str, id_col: str, produto_id: int) -> int:
    """Retorna idunidademedida do produto; fallback DEFAULT_IDUNIDADEMEDIDA (30)."""
    if not _produto_has_column(cur, table, "idunidademedida"):
        return DEFAULT_IDUNIDADEMEDIDA
    cur.execute(
        f"SELECT idunidademedida FROM {table} WHERE {id_col} = %s LIMIT 1",
        (produto_id,),
    )
    row = cur.fetchone()
    if not row:
        return DEFAULT_IDUNIDADEMEDIDA
    val = row["idunidademedida"] if isinstance(row, dict) else row[0]
    if val is None:
        return DEFAULT_IDUNIDADEMEDIDA
    try:
        return int(val)
    except (TypeError, ValueError):
        return DEFAULT_IDUNIDADEMEDIDA


def _resolve_produto_id(
    cur, cfg: Dict[str, str], codigo: str, nome: str = ""
) -> Tuple[int, str]:
    """
    Resolve id do produto UniPlus a partir do campo visível `codigo`.

    Ordem:
    1) Match exato em produto.codigo (campo visível ao usuário)
    2) Se o valor bater no id interno, erro orientando a usar o codigo
    3) Fallback por nome (último recurso)
    """
    table = cfg["produto_table"]
    codigo_col = cfg["produto_codigo_column"]
    id_col = cfg["produto_id_column"]
    codigo = str(codigo or "").strip()
    nome = str(nome or "").strip()

    if codigo:
        cur.execute(
            f"SELECT {id_col} AS id, {codigo_col} AS codigo FROM {table} WHERE CAST({codigo_col} AS text) = %s LIMIT 1",
            (codigo,),
        )
        row = cur.fetchone()
        if row:
            rid = int(row["id"] if isinstance(row, dict) else row[0])
            rc = str(row["codigo"] if isinstance(row, dict) else row[1]).strip()
            return rid, rc

        # Valor parece ser o id interno (ex.: 177) em vez do codigo visível (ex.: 1080)
        if codigo.isdigit():
            cur.execute(
                f"SELECT {id_col} AS id, {codigo_col} AS codigo FROM {table} WHERE {id_col} = %s LIMIT 1",
                (int(codigo),),
            )
            by_id = cur.fetchone()
            if by_id:
                rid = int(by_id["id"] if isinstance(by_id, dict) else by_id[0])
                rc = str(
                    by_id["codigo"] if isinstance(by_id, dict) else by_id[1]
                ).strip()
                raise UniplusPermanentError(
                    "ERR_UNIPLUS_USE_CODIGO_NOT_ID: "
                    f"informado id interno={rid}; use o codigo visível={rc or '-'} "
                    "(campo codigo do cadastro UniPlus)"
                )

    needle = _normalize_nome(nome)
    nome_col = _produto_nome_column(cur, table) if needle else ""
    if needle and nome_col:
        token = needle.split(" ")[0]
        if len(token) >= 3:
            cur.execute(
                f"""
                SELECT {id_col} AS id, {codigo_col} AS codigo, {nome_col} AS nome
                FROM {table}
                WHERE lower(CAST({nome_col} AS text)) LIKE %s
                LIMIT 50
                """,
                (f"%{token}%",),
            )
            rows = cur.fetchall() or []
            best = None
            best_len = -1
            for row in rows:
                if isinstance(row, dict):
                    rid = int(row["id"])
                    rc = str(row.get("codigo") or "").strip()
                    rnome = str(row.get("nome") or "")
                else:
                    rid = int(row[0])
                    rc = str(row[1] or "").strip()
                    rnome = str(row[2] or "")
                pname = _normalize_nome(rnome)
                if not pname:
                    continue
                if needle == pname or needle in pname or pname in needle:
                    if len(pname) > best_len:
                        best = (rid, rc or codigo or token)
                        best_len = len(pname)
            if best:
                return best

    raise UniplusPermanentError(
        f"ERR_UNIPLUS_PRODUCT_NOT_FOUND: codigo={codigo or '-'} nome={nome or '-'} "
        "(valide o campo codigo do UniPlus, não o id interno)"
    )


def _blank_hash40() -> str:
    return " " * 40


def _insert_contamesaitem(
    cur,
    item_table: str,
    item_cols: Set[str],
    *,
    conta_id: int,
    numeromesa: int,
    idproduto: int,
    codigo: str,
    nome: str,
    qty: float,
    precounitario: float,
    valortotal: float,
    observacao: str,
    protocol_key: str,
    item_hash: str,
    data_val: Any,
    hora_abertura: Any,
    now: datetime,
    idunidademedida: int,
    cnpjfilial: str,
) -> None:
    """INSERT item alinhado ao padrão nativo do UniPlus (defaults não-NULL)."""
    row: Dict[str, Any] = {
        "idcontamesa": conta_id,
        "idproduto": idproduto,
        "quantidade": qty,
        "precounitario": precounitario,
        "valortotal": valortotal,
        "valorliquido": valortotal,
        "cancelado": 0,
        "canceladoantesdeconfirmar": 0,
        "observacao": (observacao or "")[:255],
        "codigoproduto": codigo[:20],
        "codigoprodutodigitado": codigo[:20],
        "nomeproduto": nome,
        "unidademedida": "UN",
        "idunidademedida": int(idunidademedida or DEFAULT_IDUNIDADEMEDIDA),
        "orderidintegracao": protocol_key,
        "tipointegracao": 0,
        "confirmado": 1,
        "hash": item_hash,
        "hashop": _blank_hash40(),
        "hashkit": _blank_hash40(),
        "hashimpressao": 0,
        "data": data_val,
        "horaabertura": hora_abertura,
        "datahoralancamento": now.isoformat(),
        "currenttimemillis": int(now.timestamp() * 1000),
        "numeroconta": numeromesa,
        "decimaisquantidade": 0,
        "decimaispreco": 2,
        "imprimir": 1,
        "kit": 0,
        "lancamentoautomatico": 0,
        "mesatransferida": 0,
        "fatorconversao": 1.0,
        "descontomanual": 0,
        "descontopromocao": 0.0,
        "descontounitario": 0.0,
        "entregue": 0,
        "idgrupo": 0,
        "couvertartistico": 0,
        "naocobrartaxaservico": 0,
        "taxaservico": 0,
        "valortaxaservico": 0.0,
        "nummaxcombinacao": 0,
        "pauta": 0,
        "precoalterado": 0,
        "prontonaop": 0,
        "quantidadeimpressa": 0.0,
        "quantidadepaga": 0.0,
        "tiporodizio": 0,
        "truncarpreco": 1,
        "vendainteiragourmet": 0,
        "motivocancelamento": "",
        "supervisorcancelamento": "",
        "usuariocancelamento": "",
        "cnpjfilial": (cnpjfilial or "")[:18],
    }

    cols = [c for c in row.keys() if c in item_cols]
    if "idcontamesa" not in cols or "idproduto" not in cols:
        raise RuntimeError(
            f"ERR_UNIPLUS_SCHEMA: {item_table} sem idcontamesa/idproduto"
        )
    values = [row[c] for c in cols]
    placeholders = ",".join(["%s"] * len(cols))
    cur.execute(
        f"INSERT INTO {item_table} ({', '.join(cols)}) VALUES ({placeholders})",
        tuple(values),
    )


def _summarize_payload(
    protocol: str,
    contamesa: Dict[str, Any],
    itens: list,
    form_response_id: Any = None,
) -> Dict[str, Any]:
    items_summary = []
    for item in itens:
        qty = float(item.get("quantidade") or 1)
        codigo = str(item.get("codigoproduto") or "").strip()
        nome = str(item.get("nomeproduto") or "").strip()
        valortotal = float(item.get("valortotal") or 0)
        items_summary.append(
            {
                "codigo": codigo,
                "nome": nome,
                "qtd": qty,
                "total": valortotal,
            }
        )
    return {
        "protocol": str(protocol)[:40],
        "formResponseId": form_response_id,
        "cliente": str(contamesa.get("nomecliente") or "")[:60],
        "telefone": str(contamesa.get("telefone") or "")[:20],
        "endereco": " ".join(
            p
            for p in [
                str(contamesa.get("endereco") or "").strip(),
                str(contamesa.get("endereconumero") or "").strip(),
                str(contamesa.get("enderecobairro") or "").strip(),
            ]
            if p
        )[:120],
        "valortotal": float(contamesa.get("valortotal") or 0),
        "valorentrega": float(contamesa.get("valorentrega") or 0),
        "itens_count": len(items_summary),
        "itens": items_summary,
    }


def _next_numeromesa(cur, mesa_table: str, *, open_delivery_only: bool = True) -> int:
    """
    Próximo card delivery.
    open_delivery_only=True: só entre contas abertas tipopedido=0 (comportamento Gourmet).
    open_delivery_only=False: MAX global — usado no retry após colisão de unique.
    """
    if open_delivery_only:
        cur.execute(
            f"""
            SELECT COALESCE(MAX(numeromesa), 0) + 1 AS next_num
            FROM {mesa_table}
            WHERE status = 1 AND tipopedido = 0
            """
        )
    else:
        cur.execute(
            f"""
            SELECT COALESCE(MAX(numeromesa), 0) + 1 AS next_num
            FROM {mesa_table}
            """
        )
    row = cur.fetchone()
    next_num = int(row["next_num"] if isinstance(row, dict) else row[0])
    return max(next_num, 1)


def _existing_by_protocol(cur, mesa_table: str, protocol: str, summary: Dict[str, Any]) -> Dict[str, Any]:
    cur.execute(
        f"""
        SELECT id, numeromesa, status
        FROM {mesa_table}
        WHERE orderidintegracao = %s
        ORDER BY id DESC
        LIMIT 1
        """,
        (str(protocol)[:40],),
    )
    existing = cur.fetchone()
    if not existing:
        return None
    conta_id = int(existing["id"])
    status = int(existing["status"] if existing.get("status") is not None else -1)
    numeromesa_existing = (
        int(existing["numeromesa"])
        if existing.get("numeromesa") is not None
        else None
    )
    if status != 1:
        raise UniplusPermanentError(
            f"ERR_UNIPLUS_PROTOCOL_CLOSED: protocol={protocol} conta={conta_id} status={status}"
        )
    return {
        **summary,
        "conta_id": conta_id,
        "numeromesa": numeromesa_existing,
        "action": "already_exists",
        "message": "already_exists",
    }


def format_uniplus_log_message(result: Dict[str, Any]) -> str:
    action = result.get("action") or "?"
    conta_id = result.get("conta_id")
    protocol = result.get("protocol") or "?"
    cliente = result.get("cliente") or "?"
    total = result.get("valortotal")
    n_itens = result.get("itens_count") or 0
    mesa = result.get("numeromesa")
    mesa_txt = f" mesa={mesa}" if mesa is not None else ""
    if action == "already_exists":
        return (
            f"UniPlus REUSO (já existia) conta={conta_id}{mesa_txt} protocol={protocol} "
            f"cliente={cliente} total={total} itens={n_itens}"
        )
    itens = result.get("itens") or []
    itens_txt = ", ".join(
        f"{it.get('qtd')}x {it.get('nome') or it.get('codigo')} (R$ {it.get('total')})"
        for it in itens[:8]
    )
    if len(itens) > 8:
        itens_txt += f" +{len(itens) - 8} itens"
    return (
        f"UniPlus INSERT conta={conta_id}{mesa_txt} protocol={protocol} "
        f"cliente={cliente} total={total} · {itens_txt}"
    )


def _table_columns(cur, table: str) -> set:
    """Colunas da tabela no schema atual (lowercase)."""
    cur.execute(
        """
        SELECT lower(column_name) AS col
        FROM information_schema.columns
        WHERE lower(table_name) = lower(%s)
          AND table_schema = current_schema()
        """,
        (table,),
    )
    rows = cur.fetchall() or []
    cols = set()
    for row in rows:
        if isinstance(row, dict):
            cols.add(str(row.get("col") or "").lower())
        else:
            cols.add(str(row[0]).lower())
    return cols


def _is_undefined_column_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    pgcode = getattr(exc, "pgcode", None)
    return pgcode == "42703" or "undefined column" in msg or "does not exist" in msg


def _insert_contamesa(
    cur,
    mesa_table: str,
    contamesa: Dict[str, Any],
    protocol_key: str,
    numeromesa: int,
    hash_val: str,
    now: datetime,
    include_optional: bool,
    table_cols: Optional[Set[str]] = None,
) -> int:
    """INSERT CONTAMESA. include_optional controla statusagendamento/pautaunica."""
    cliente = str(
        contamesa.get("nomecliente")
        or contamesa.get("nome")
        or "Cliente"
    )[:60]
    hora_abert = contamesa.get("horaabertura") or now.isoformat()
    hora_pedido = contamesa.get("horapedidoefetuado") or hora_abert

    base_cols = [
        "tipopedido", "status", "situacao", "numeromesa",
        "idfilial", "idusuario", "idcliente", "codigocliente",
        "nomecliente", "telefone", "documento",
        "endereco", "endereconumero", "enderecobairro",
        "enderecocomplemento", "enderecoreferencia",
        "valorentrega", "valortotal", "valorcombinado",
        "valordinheiro", "valorcartao", "valorpix",
        "valorcarteiradigital", "valoroutros", "valorcheque",
        "tipointegracao", "nomeintegracao", "orderidintegracao",
        "hash", "statussinc", "cupomcancelado",
        "retiradanobalcao", "retirabalcaodepois", "paraviagem",
        "numeropessoas", "desconto", "obs",
        "data", "horaabertura", "horaultimoconsumo",
        "currenttimemillis", "timestampalteracao",
    ]
    base_values = [
        int(contamesa.get("tipopedido") or 0),
        int(contamesa.get("status") or 1),
        int(contamesa.get("situacao") or 0),
        numeromesa,
        int(contamesa.get("idfilial") or 1),
        int(contamesa.get("idusuario") or 1),
        int(contamesa.get("idcliente") or 0),
        str(contamesa.get("codigocliente") or "")[:14],
        cliente,
        str(contamesa.get("telefone") or "")[:20],
        str(contamesa.get("documento") or "")[:18],
        str(contamesa.get("endereco") or "")[:60],
        str(contamesa.get("endereconumero") or "")[:12],
        str(contamesa.get("enderecobairro") or "")[:255],
        str(contamesa.get("enderecocomplemento") or "")[:255],
        str(contamesa.get("enderecoreferencia") or "")[:255],
        float(contamesa.get("valorentrega") or 0),
        float(contamesa.get("valortotal") or 0),
        float(contamesa.get("valorcombinado") or contamesa.get("valortotal") or 0),
        float(contamesa.get("valordinheiro") or 0),
        float(contamesa.get("valorcartao") or 0),
        float(contamesa.get("valorpix") or 0),
        float(contamesa.get("valorcarteiradigital") or 0),
        float(contamesa.get("valoroutros") or 0),
        float(contamesa.get("valorcheque") or 0),
        int(contamesa.get("tipointegracao") or 0),
        str(contamesa.get("nomeintegracao") or "")[:64],
        protocol_key,
        hash_val,
        int(contamesa.get("statussinc") or 1),
        int(contamesa.get("cupomcancelado") or 0),
        int(contamesa.get("retiradanobalcao") or 0),
        int(contamesa.get("retirabalcaodepois") or 0),
        int(contamesa.get("paraviagem") or 0),
        int(contamesa.get("numeropessoas") or 1),
        float(contamesa.get("desconto") or 0),
        str(contamesa.get("obs") or "")[:255],
        contamesa.get("data") or now.date().isoformat(),
        hora_abert,
        contamesa.get("horaultimoconsumo") or hora_abert,
        int(contamesa.get("currenttimemillis") or int(now.timestamp() * 1000)),
        int(contamesa.get("timestampalteracao") or int(now.timestamp() * 1000)),
    ]

    cols = list(base_cols)
    values = list(base_values)

    # Unico lista delivery pelo campo `nome` (além de nomecliente).
    known_cols = table_cols or set()
    if not known_cols or "nome" in known_cols:
        cols.append("nome")
        values.append(cliente)
    if not known_cols or "horapedidoefetuado" in known_cols:
        cols.append("horapedidoefetuado")
        values.append(hora_pedido)
    # Inserção nativa UniPlus usa abertaoffline=1
    if not known_cols or "abertaoffline" in known_cols:
        cols.append("abertaoffline")
        values.append(
            int(
                contamesa.get("abertaoffline")
                if contamesa.get("abertaoffline") is not None
                else 1
            )
        )
    if (not known_cols or "cnpjfilial" in known_cols) and contamesa.get("cnpjfilial"):
        cols.append("cnpjfilial")
        values.append(str(contamesa.get("cnpjfilial") or "")[:18])

    if include_optional:
        cols.extend(["statusagendamento", "pautaunica"])
        values.append(int(contamesa.get("statusagendamento") or 3))
        values.append(
            int(
                contamesa.get("pautaunica")
                if contamesa.get("pautaunica") is not None
                else 1
            )
        )

    placeholders = ",".join(["%s"] * len(values))
    sql = (
        f"INSERT INTO {mesa_table} ({', '.join(cols)}) "
        f"VALUES ({placeholders}) RETURNING id"
    )
    cur.execute(sql, tuple(values))
    row = cur.fetchone()
    return int(row["id"] if isinstance(row, dict) else row[0])


def handle_uniplus_job(db_module, conteudo: Dict[str, Any]) -> Dict[str, Any]:
    """
    Insere delivery aberto.
    Idempotente por orderidintegracao (= protocol Compuchat), com advisory lock.
    """
    if psycopg2 is None:
        raise UniplusPermanentError(
            "ERR_UNIPLUS_CONFIG: psycopg2 não instalado. Rode: pip install psycopg2-binary"
        )

    cfg = _cfg(db_module)
    if cfg["enabled"] not in ("true", "1", "yes", "on"):
        raise UniplusPermanentError("ERR_UNIPLUS_CONFIG: UniPlus desabilitado na config do agente")
    if not cfg["connection_string"]:
        raise UniplusPermanentError("ERR_UNIPLUS_CONFIG: uniplus_connection_string não configurada")

    protocol = (
        conteudo.get("protocol")
        or (conteudo.get("contamesa") or {}).get("orderidintegracao")
    )
    if not protocol:
        raise UniplusPermanentError("ERR_UNIPLUS_PAYLOAD: sem protocol/orderidintegracao")

    contamesa = conteudo.get("contamesa") or {}
    itens = conteudo.get("itens") or []
    if not contamesa or not itens:
        raise UniplusPermanentError("ERR_UNIPLUS_PAYLOAD: incompleto (contamesa/itens)")

    summary = _summarize_payload(
        str(protocol),
        contamesa,
        itens,
        form_response_id=conteudo.get("formResponseId"),
    )
    summary["table_contamesa"] = cfg["contamesa_table"]
    summary["table_itens"] = cfg["contamesaitem_table"]
    mesa_table = cfg["contamesa_table"]
    item_table = cfg["contamesaitem_table"]
    now = datetime.now(timezone.utc)
    protocol_key = str(protocol)[:40]

    conn = _connect(cfg["connection_string"])
    try:
        with conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                # Lock global de numeromesa + lock do protocol (idempotência)
                cur.execute("SELECT pg_advisory_xact_lock(%s)", (872014001,))
                cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (protocol_key,))

                existing_result = _existing_by_protocol(cur, mesa_table, protocol_key, summary)
                if existing_result:
                    logger.info(
                        "UniPlus already_exists conta=%s mesa=%s protocol=%s",
                        existing_result.get("conta_id"),
                        existing_result.get("numeromesa"),
                        protocol_key,
                    )
                    return existing_result

                cols = _table_columns(cur, mesa_table)
                include_optional = (
                    "statusagendamento" in cols and "pautaunica" in cols
                )
                if not include_optional:
                    logger.warning(
                        "UniPlus: colunas statusagendamento/pautaunica ausentes em %s — "
                        "INSERT sem elas (compatibilidade)",
                        mesa_table,
                    )

                hash_val = _pad_hash(str(contamesa.get("hash") or ""))
                conta_id = None
                numeromesa = None
                last_exc = None

                for attempt in range(5):
                    cur.execute("SAVEPOINT uniplus_ins")
                    # Após colisão, usa MAX global para não repetir número ocupado
                    numeromesa = _next_numeromesa(
                        cur, mesa_table, open_delivery_only=(attempt == 0)
                    )
                    try:
                        conta_id = _insert_contamesa(
                            cur,
                            mesa_table,
                            contamesa,
                            protocol_key,
                            numeromesa,
                            hash_val,
                            now,
                            include_optional=include_optional,
                            table_cols=cols,
                        )
                        cur.execute("RELEASE SAVEPOINT uniplus_ins")
                        last_exc = None
                        break
                    except Exception as exc:
                        cur.execute("ROLLBACK TO SAVEPOINT uniplus_ins")

                        # Schema sem colunas opcionais (se detection falhou)
                        if include_optional and _is_undefined_column_error(exc):
                            logger.warning(
                                "UniPlus: INSERT falhou por coluna ausente (%s). "
                                "Retentando sem statusagendamento/pautaunica.",
                                exc,
                            )
                            include_optional = False
                            cur.execute("SAVEPOINT uniplus_ins")
                            try:
                                conta_id = _insert_contamesa(
                                    cur,
                                    mesa_table,
                                    contamesa,
                                    protocol_key,
                                    numeromesa,
                                    hash_val,
                                    now,
                                    include_optional=False,
                                    table_cols=cols,
                                )
                                cur.execute("RELEASE SAVEPOINT uniplus_ins")
                                last_exc = None
                                break
                            except Exception as exc2:
                                cur.execute("ROLLBACK TO SAVEPOINT uniplus_ins")
                                last_exc = exc2
                                if isinstance(exc2, IntegrityError):
                                    reused = _existing_by_protocol(
                                        cur, mesa_table, protocol_key, summary
                                    )
                                    if reused:
                                        return reused
                                    continue
                                raise

                        if isinstance(exc, IntegrityError):
                            reused = _existing_by_protocol(
                                cur, mesa_table, protocol_key, summary
                            )
                            if reused:
                                return reused
                            last_exc = exc
                            logger.warning(
                                "UniPlus IntegrityError na tentativa %s mesa=%s protocol=%s: %s",
                                attempt + 1,
                                numeromesa,
                                protocol_key,
                                exc,
                            )
                            continue
                        raise

                if conta_id is None:
                    raise last_exc or RuntimeError(
                        "ERR_UNIPLUS_INSERT: falha ao inserir CONTAMESA"
                    )

                item_cols = _table_columns(cur, item_table)
                cnpjfilial = str(contamesa.get("cnpjfilial") or "").strip()
                data_val = contamesa.get("data") or now.date().isoformat()
                hora_abert = contamesa.get("horaabertura") or now.isoformat()

                inserted_items = []
                for item in itens:
                    codigo = str(item.get("codigoproduto") or "").strip()
                    nome = str(item.get("nomeproduto") or "")[:120]
                    if not codigo and not nome:
                        raise UniplusPermanentError(
                            "ERR_UNIPLUS_PAYLOAD: Item sem codigoproduto/nomeproduto"
                        )
                    idproduto, codigo_resolvido = _resolve_produto_id(
                        cur, cfg, codigo, nome
                    )
                    codigo = str(codigo_resolvido or codigo).strip()
                    if not codigo:
                        raise UniplusPermanentError(
                            f"ERR_UNIPLUS_PRODUCT_NOT_FOUND: nome={nome or '-'}"
                        )
                    qty = float(item.get("quantidade") or 1)
                    precounitario = float(item.get("precounitario") or 0)
                    valortotal = float(item.get("valortotal") or (precounitario * qty))
                    # contamesitem_uk1 UNIQUE(hash) — hash vazio/espaço colide no Unichef
                    item_hash = _pad_hash(str(item.get("hash") or ""))
                    id_un = _fetch_produto_unidademedida(
                        cur, cfg["produto_table"], cfg["produto_id_column"], idproduto
                    )
                    _insert_contamesaitem(
                        cur,
                        item_table,
                        item_cols,
                        conta_id=conta_id,
                        numeromesa=numeromesa,
                        idproduto=idproduto,
                        codigo=codigo,
                        nome=nome or codigo,
                        qty=qty,
                        precounitario=precounitario,
                        valortotal=valortotal,
                        observacao=str(item.get("observacao") or ""),
                        protocol_key=protocol_key,
                        item_hash=item_hash,
                        data_val=data_val,
                        hora_abertura=hora_abert,
                        now=now,
                        idunidademedida=id_un,
                        cnpjfilial=cnpjfilial,
                    )
                    inserted_items.append(
                        {
                            "codigo": codigo[:20],
                            "nome": nome,
                            "idproduto": idproduto,
                            "idunidademedida": id_un,
                            "qtd": qty,
                            "precounitario": precounitario,
                            "total": valortotal,
                            "hash": item_hash if "hash" in item_cols else None,
                        }
                    )

                result = {
                    **summary,
                    "conta_id": conta_id,
                    "numeromesa": numeromesa,
                    "action": "created",
                    "message": "created",
                    "itens": inserted_items,
                    "itens_count": len(inserted_items),
                }
                logger.info(
                    "UniPlus INSERT conta=%s mesa=%s protocol=%s cliente=%s itens=%s total=%s",
                    conta_id,
                    numeromesa,
                    protocol_key,
                    summary.get("cliente"),
                    len(inserted_items),
                    summary.get("valortotal"),
                )
                return result
    finally:
        conn.close()
