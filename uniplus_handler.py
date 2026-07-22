"""Handler UniPlus: grava delivery aberto em CONTAMESA / CONTAMESAITEM no Postgres local."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger("uniplus")

try:
    import psycopg2
    from psycopg2.extras import RealDictCursor
except ImportError:
    psycopg2 = None


def _pad_hash(value: str) -> str:
    s = (value or "").replace("-", "")
    return (s + (" " * 40))[:40]


def _cfg(db_module) -> Dict[str, str]:
    return {
        "enabled": (db_module.get_config("uniplus_enabled") or "false").lower(),
        "connection_string": (db_module.get_config("uniplus_connection_string") or "").strip(),
        "produto_table": (db_module.get_config("uniplus_produto_table") or "produto").strip(),
        "produto_codigo_column": (db_module.get_config("uniplus_produto_codigo_column") or "codigo").strip(),
        "produto_id_column": (db_module.get_config("uniplus_produto_id_column") or "id").strip(),
        "contamesa_table": (db_module.get_config("uniplus_contamesa_table") or "contamesa").strip(),
        "contamesaitem_table": (db_module.get_config("uniplus_contamesaitem_table") or "contamesaitem").strip(),
    }


def is_uniplus_enabled(db_module) -> bool:
    cfg = _cfg(db_module)
    return cfg["enabled"] in ("true", "1", "yes", "on") and bool(cfg["connection_string"])


def _resolve_produto_id(cur, cfg: Dict[str, str], codigo: str) -> int:
    table = cfg["produto_table"]
    codigo_col = cfg["produto_codigo_column"]
    id_col = cfg["produto_id_column"]
    cur.execute(
        f"SELECT {id_col} AS id FROM {table} WHERE {codigo_col} = %s LIMIT 1",
        (codigo,),
    )
    row = cur.fetchone()
    if not row:
        raise RuntimeError(f"Produto UniPlus não encontrado para codigo={codigo}")
    return int(row["id"] if isinstance(row, dict) else row[0])


def handle_uniplus_job(db_module, conteudo: Dict[str, Any]) -> Tuple[int, str]:
    """
    Insere delivery aberto. Retorna (conta_id, message).
    Idempotente por orderidintegracao (= protocol Compuchat).
    """
    if psycopg2 is None:
        raise RuntimeError("psycopg2 não instalado. Rode: pip install psycopg2-binary")

    cfg = _cfg(db_module)
    if cfg["enabled"] not in ("true", "1", "yes", "on"):
        raise RuntimeError("UniPlus desabilitado na config do agente")
    if not cfg["connection_string"]:
        raise RuntimeError("uniplus_connection_string não configurada")

    protocol = (
        conteudo.get("protocol")
        or (conteudo.get("contamesa") or {}).get("orderidintegracao")
    )
    if not protocol:
        raise RuntimeError("Payload sem protocol/orderidintegracao")

    contamesa = conteudo.get("contamesa") or {}
    itens = conteudo.get("itens") or []
    if not contamesa or not itens:
        raise RuntimeError("Payload incompleto: contamesa/itens")

    mesa_table = cfg["contamesa_table"]
    item_table = cfg["contamesaitem_table"]
    now = datetime.now(timezone.utc)

    conn = psycopg2.connect(cfg["connection_string"])
    try:
        with conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    f"SELECT id FROM {mesa_table} WHERE orderidintegracao = %s LIMIT 1",
                    (str(protocol)[:40],),
                )
                existing = cur.fetchone()
                if existing:
                    return int(existing["id"]), "already_exists"

                hash_val = _pad_hash(str(contamesa.get("hash") or ""))
                insert_mesa = f"""
                    INSERT INTO {mesa_table} (
                        tipopedido, status, situacao, numeromesa,
                        idfilial, idusuario, idcliente, codigocliente,
                        nomecliente, telefone, documento,
                        endereco, endereconumero, enderecobairro,
                        enderecocomplemento, enderecoreferencia,
                        valorentrega, valortotal, valorcombinado,
                        valordinheiro, valorcartao, valorpix,
                        valorcarteiradigital, valoroutros, valorcheque,
                        tipointegracao, nomeintegracao, orderidintegracao,
                        hash, statussinc, cupomcancelado,
                        retiradanobalcao, retirabalcaodepois, paraviagem,
                        numeropessoas, desconto, obs,
                        data, horaabertura, horaultimoconsumo,
                        currenttimemillis, timestampalteracao
                    ) VALUES (
                        %s,%s,%s,%s,
                        %s,%s,%s,%s,
                        %s,%s,%s,
                        %s,%s,%s,
                        %s,%s,
                        %s,%s,%s,
                        %s,%s,%s,
                        %s,%s,%s,
                        %s,%s,%s,
                        %s,%s,%s,
                        %s,%s,%s,
                        %s,%s,%s,
                        %s,%s,%s,
                        %s,%s
                    ) RETURNING id
                """
                mesa_values = (
                    int(contamesa.get("tipopedido") or 0),
                    int(contamesa.get("status") or 1),
                    int(contamesa.get("situacao") or 0),
                    int(contamesa.get("numeromesa") or 1),
                    int(contamesa.get("idfilial") or 1),
                    int(contamesa.get("idusuario") or 1),
                    int(contamesa.get("idcliente") or 0),
                    str(contamesa.get("codigocliente") or "")[:14],
                    str(contamesa.get("nomecliente") or "Cliente")[:60],
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
                    str(protocol)[:40],
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
                    contamesa.get("horaabertura") or now.isoformat(),
                    contamesa.get("horaultimoconsumo") or now.isoformat(),
                    int(contamesa.get("currenttimemillis") or int(now.timestamp() * 1000)),
                    int(contamesa.get("timestampalteracao") or int(now.timestamp() * 1000)),
                )
                cur.execute(insert_mesa, mesa_values)
                conta_id = int(cur.fetchone()["id"])

                for item in itens:
                    codigo = str(item.get("codigoproduto") or "").strip()
                    if not codigo:
                        raise RuntimeError("Item sem codigoproduto")
                    idproduto = _resolve_produto_id(cur, cfg, codigo)
                    qty = float(item.get("quantidade") or 1)
                    precounitario = float(item.get("precounitario") or 0)
                    valortotal = float(item.get("valortotal") or (precounitario * qty))
                    cur.execute(
                        f"""
                        INSERT INTO {item_table} (
                            idcontamesa, idproduto, quantidade, precounitario, valortotal,
                            cancelado, observacao, codigoproduto, nomeproduto, unidademedida,
                            orderidintegracao, tipointegracao, confirmado,
                            data, horaabertura, datahoralancamento, currenttimemillis,
                            numeroconta, decimaisquantidade, decimaispreco
                        ) VALUES (
                            %s,%s,%s,%s,%s,
                            0,%s,%s,%s,%s,
                            %s,0,1,
                            %s,%s,%s,%s,
                            %s,0,2
                        )
                        """,
                        (
                            conta_id,
                            idproduto,
                            qty,
                            precounitario,
                            valortotal,
                            str(item.get("observacao") or "")[:255],
                            codigo[:20],
                            str(item.get("nomeproduto") or "")[:120],
                            str(item.get("unidademedida") or "UN")[:6],
                            str(protocol)[:40],
                            contamesa.get("data") or now.date().isoformat(),
                            contamesa.get("horaabertura") or now.isoformat(),
                            now.isoformat(),
                            int(now.timestamp() * 1000),
                            int(contamesa.get("numeromesa") or 1),
                        ),
                    )

                return conta_id, "created"
    finally:
        conn.close()
