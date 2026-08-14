"""API local do POS (tablet) — /pos/*."""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from functools import wraps
from typing import Any, Dict, List, Optional

from flask import Blueprint, jsonify, request, send_file

import db
from pos_catalog import media_dir, sync_catalog_from_cloud
from uniplus_handler import (
    get_open_mesa_conta,
    handle_uniplus_job,
    is_uniplus_enabled,
    list_open_contas,
    list_pedidos_dia,
    parse_numeromesa,
    set_item_entregue,
)

pos_bp = Blueprint("pos", __name__)


def _require_pos_token(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        expected = (db.get_config("pos_api_token") or "").strip()
        if expected:
            got = (
                request.headers.get("X-Pos-Token")
                or request.args.get("token")
                or request.headers.get("Authorization", "").replace("Bearer ", "")
            ).strip()
            if got != expected:
                return jsonify({"error": "unauthorized"}), 401
        return fn(*args, **kwargs)

    return wrapper


def _find_product(product_id: int) -> Optional[Dict[str, Any]]:
    return db.get_pos_product(int(product_id))


def _option_codigo(product: Dict[str, Any], option_id: Any) -> str:
    if not option_id:
        return str(product.get("idUniplus") or "")
    for variation in product.get("variations") or []:
        for opt in variation.get("options") or []:
            if int(opt.get("id") or 0) == int(option_id):
                return str(opt.get("idUniplus") or product.get("idUniplus") or "")
    return str(product.get("idUniplus") or "")


def _addon_codigo(addon_id: Any) -> str:
    if not addon_id:
        return ""
    for group in db.list_pos_addon_groups():
        for item in group.get("items") or []:
            if int(item.get("id") or 0) == int(addon_id):
                return str(item.get("idUniplus") or "")
        for sg in group.get("subgroups") or []:
            for item in sg.get("items") or []:
                if int(item.get("id") or 0) == int(addon_id):
                    return str(item.get("idUniplus") or "")
    return ""


def _user_observation(item: Dict[str, Any]) -> str:
    raw = item.get("observation") or item.get("observacao") or ""
    return " ".join(str(raw).split())[:180]


def _build_uniplus_items(menu_items: List[Dict[str, Any]], protocol: str) -> List[Dict[str, Any]]:
    itens = []
    for item in menu_items or []:
        qty = float(item.get("quantity") or 1)
        if qty <= 0:
            continue
        product = _find_product(int(item.get("productId") or 0)) if item.get("productId") else None
        nome = str(item.get("productName") or (product or {}).get("name") or "Item")[:120]
        codigo = str(item.get("idUniplus") or "")
        if not codigo and product:
            codigo = _option_codigo(
                product,
                item.get("optionId")
                or item.get("variationOptionId")
                or item.get("baseOptionId"),
            )
        unit = float(item.get("productValue") or (product or {}).get("value") or 0)
        addons = item.get("addons") or []
        addons_total = 0.0
        addon_obs = []
        for addon in addons:
            if not isinstance(addon, dict):
                continue
            aval = float(addon.get("value") or 0)
            addons_total += aval
            addon_obs.append(addon.get("label") or "Adicional")
            acodigo = str(addon.get("idUniplus") or _addon_codigo(addon.get("addOnItemId")))
            if acodigo:
                itens.append(
                    {
                        "codigoproduto": acodigo[:20],
                        "nomeproduto": str(addon.get("label") or "Adicional")[:120],
                        "quantidade": qty,
                        "precounitario": aval,
                        "valortotal": round(aval * qty, 2),
                        "unidademedida": "UN",
                        "observacao": f"Adicional de {nome}"[:255],
                        "orderidintegracao": protocol,
                        "hash": str(uuid.uuid4()),
                    }
                )
        obs_parts = []
        if item.get("type") == "halfAndHalf":
            h1 = item.get("half1Name") or item.get("half1ProductId")
            h2 = item.get("half2Name") or item.get("half2ProductId")
            obs_parts.append(f"Meio a meio: {h1} / {h2}")
        if addon_obs:
            obs_parts.append("Adicionais: " + ", ".join(addon_obs[:8]))
        user_obs = _user_observation(item)
        if user_obs:
            obs_parts.append(user_obs)
        itens.append(
            {
                "codigoproduto": (codigo or "")[:20],
                "nomeproduto": nome,
                "quantidade": qty,
                "precounitario": unit,
                "valortotal": round(unit * qty, 2),
                "unidademedida": "UN",
                "observacao": " | ".join(obs_parts)[:255],
                "orderidintegracao": protocol,
                "hash": str(uuid.uuid4()),
            }
        )
    return itens


def _item_grupo(item: Dict[str, Any]) -> str:
    grupo = str(item.get("grupo") or "").strip()
    if grupo:
        return grupo
    product_id = item.get("productId")
    if product_id:
        product = _find_product(int(product_id))
        if product and product.get("grupo"):
            return str(product.get("grupo")).strip()
    return "Outros"


def _print_routes() -> Dict[str, List[str]]:
    rows = db.list_pos_print_routes()
    if not isinstance(rows, list):
        return {}
    by_device: Dict[str, List[str]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        device_id = str(row.get("deviceId") or "").strip().lower()
        if not device_id:
            continue
        names = [
            str(g).strip()
            for g in (row.get("groupNames") or [])
            if str(g or "").strip()
        ]
        if not names:
            continue
        current = by_device.setdefault(device_id, [])
        for name in names:
            if name not in current:
                current.append(name)
    return by_device


def _items_for_printer(items: List[Dict[str, Any]], group_names: List[str]) -> List[Dict[str, Any]]:
    if any(name == "*" for name in group_names):
        return items
    want = {name.lower() for name in group_names}
    return [item for item in items if str(item.get("grupo") or "Outros").strip().lower() in want]


def _grupo_by_codigo() -> Dict[str, str]:
    out: Dict[str, str] = {}
    for product in db.list_pos_products():
        if not isinstance(product, dict):
            continue
        grupo = str(product.get("grupo") or "Outros").strip() or "Outros"
        codigo = str(product.get("idUniplus") or "").strip()
        if codigo:
            out[codigo] = grupo
        for variation in product.get("variations") or []:
            if not isinstance(variation, dict):
                continue
            for opt in variation.get("options") or []:
                if not isinstance(opt, dict):
                    continue
                oc = str(opt.get("idUniplus") or "").strip()
                if oc:
                    out[oc] = grupo
        name = str(product.get("name") or "").strip().lower()
        if name and name not in out:
            out[f"name:{name}"] = grupo
    return out


def _printer_catalog() -> List[Dict[str, Any]]:
    printers = []
    for idx, p in enumerate(db.get_printers() or []):
        device_id = str(p.get("device_id") or "").strip()
        name = str(p.get("name") or "").strip() or device_id or f"Impressora {idx + 1}"
        printers.append({"deviceId": device_id, "name": name})
    return printers


def _assign_printer(grupo: str, printers: List[Dict[str, Any]], routes: Dict[str, List[str]]) -> Dict[str, str]:
    g = (grupo or "Outros").strip().lower()
    if not printers:
        return {"printerDeviceId": "", "printerName": "Pedidos"}
    if not routes:
        first = printers[0]
        return {"printerDeviceId": first["deviceId"], "printerName": first["name"]}
    leftover = None
    for p in printers:
        did = str(p.get("deviceId") or "").strip().lower()
        groups = routes.get(did) or []
        names = {str(x).strip().lower() for x in groups}
        if "*" in names:
            leftover = leftover or p
        if g in names:
            return {"printerDeviceId": p["deviceId"], "printerName": p["name"]}
    target = leftover or printers[0]
    return {"printerDeviceId": target["deviceId"], "printerName": target["name"]}


def _send_receipt(printer_cfg: Dict[str, Any], payload: Dict[str, Any], *, timeout: int = 4) -> bool:
    from printer_service import PrinterService
    from receipt_formatter import format_order_receipt

    printer = PrinterService(
        printer_ip=printer_cfg.get("printer_ip"),
        printer_port=int(printer_cfg.get("printer_port") or 9100),
        printer_type=printer_cfg.get("printer_type") or "raw",
        paper_width=int(printer_cfg.get("paper_width") or 32),
        printer_encoding=printer_cfg.get("printer_encoding") or "cp850",
        connection_type=printer_cfg.get("connection_type") or "network",
        printer_name_local=printer_cfg.get("printer_name_local") or None,
        timeout=timeout,
        max_retries=0,
    )
    return bool(printer.print_receipt(format_order_receipt(payload)))


def _cancel_printer_queue(printer_cfg: Dict[str, Any]) -> Dict[str, Any]:
    from printer_service import PrinterService

    name = str(printer_cfg.get("name") or printer_cfg.get("device_id") or "Impressora").strip()
    device_id = str(printer_cfg.get("device_id") or "").strip()
    try:
        ok, message = PrinterService.from_config(printer_cfg, timeout=4, max_retries=0).cancel_queue()
    except Exception as exc:
        ok, message = False, str(exc)
    return {
        "deviceId": device_id,
        "name": name,
        "ok": bool(ok),
        "message": message or ("Fila cancelada" if ok else "Falha ao cancelar fila"),
    }


def _printers_matching(device_id: str = "") -> List[Dict[str, Any]]:
    printers = list(db.get_printers() or [])
    want = str(device_id or "").strip().lower()
    if not want:
        return printers
    return [
        p for p in printers
        if str(p.get("device_id") or "").strip().lower() == want
    ]


def _print_kitchen(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Tenta imprimir sem bloquear o pedido. Sempre devolve printed true/false."""
    info = {"printed": False, "attempted": 0, "failed": 0, "error": ""}
    try:
        from concurrent.futures import ThreadPoolExecutor, wait

        printers = db.get_printers()
        if not printers:
            info["error"] = "Nenhuma impressora configurada"
            return info
        items = []
        for raw in payload.get("menuItems") or []:
            if not isinstance(raw, dict):
                continue
            item = dict(raw)
            item["grupo"] = _item_grupo(item)
            items.append(item)
        if not items:
            info["printed"] = True
            return info

        routes = _print_routes()
        jobs: List[tuple] = []
        if not routes:
            jobs = [(p, items) for p in printers]
        else:
            used = set()
            for p in printers:
                device_id = str(p.get("device_id") or "").strip().lower()
                groups = routes.get(device_id)
                if not groups:
                    continue
                subset = _items_for_printer(items, groups)
                if not subset:
                    continue
                jobs.append((p, subset))
                used.update(id(it) for it in subset)
            leftover = [it for it in items if id(it) not in used]
            if leftover:
                star = [p for p in printers if "*" in (routes.get(str(p.get("device_id") or "").strip().lower()) or [])]
                targets = star or printers
                for p in targets:
                    jobs.append((p, leftover))

        if not jobs:
            info["error"] = "Nenhuma rota de impressão para os itens"
            return info

        def _run_job(printer_cfg: Dict[str, Any], subset: List[Dict[str, Any]]) -> bool:
            job = dict(payload)
            job["menuItems"] = subset
            try:
                return _send_receipt(printer_cfg, job, timeout=4)
            except Exception as exc:
                print(f"[POS] impressora falhou: {exc}")
                return False

        ok = 0
        fail = 0
        errors: List[str] = []
        with ThreadPoolExecutor(max_workers=min(4, len(jobs))) as pool:
            futures = [pool.submit(_run_job, cfg, subset) for cfg, subset in jobs]
            done, pending = wait(futures, timeout=8)
            for fut in pending:
                fail += 1
                errors.append("impressora não respondeu")
            for fut in done:
                info["attempted"] += 1
                try:
                    if fut.result():
                        ok += 1
                    else:
                        fail += 1
                except Exception:
                    fail += 1
        info["failed"] = fail
        info["printed"] = ok > 0 and fail == 0
        if fail and not ok:
            info["error"] = errors[0] if errors else "Falha ao imprimir. Verifique o papel da impressora."
        elif fail:
            info["printed"] = False
            info["error"] = "Pedido gravado, mas uma impressora não concluiu."
        return info
    except Exception as exc:
        print(f"[POS] impressão cozinha falhou: {exc}")
        info["error"] = str(exc)
        return info


@pos_bp.route("/pos/health", methods=["GET"])
@_require_pos_token
def pos_health():
    uniplus_ok = False
    uniplus_msg = "disabled"
    if is_uniplus_enabled(db):
        dsn = (db.get_config("uniplus_connection_string") or "").strip()
        uniplus_ok = bool(dsn)
        uniplus_msg = "configured" if dsn else "missing_dsn"
    return jsonify(
        {
            "ok": True,
            "catalogVersion": int(db.get_config("pos_catalog_version") or 0),
            "updatedAt": db.get_config("pos_catalog_updated_at") or "",
            "uniplus": {"ok": uniplus_ok, "status": uniplus_msg},
        }
    )


@pos_bp.route("/pos/sync", methods=["GET"])
@_require_pos_token
def pos_sync():
    refresh = str(request.args.get("refresh") or "").lower() in ("1", "true", "yes")
    if refresh:
        try:
            sync_catalog_from_cloud()
        except Exception as exc:
            db.set_config("pos_last_sync_error", str(exc))
            return jsonify({"error": str(exc), "catalog": db.build_pos_sync_payload()}), 502
    since = request.args.get("since")
    payload = db.build_pos_sync_payload()
    if since and str(payload.get("catalogVersion")) == str(since):
        return jsonify({"unchanged": True, "catalogVersion": payload["catalogVersion"]})
    return jsonify(payload)


@pos_bp.route("/pos/sync", methods=["POST"])
@_require_pos_token
def pos_sync_now():
    try:
        result = sync_catalog_from_cloud()
        return jsonify(result)
    except Exception as exc:
        db.set_config("pos_last_sync_error", str(exc))
        return jsonify({"error": str(exc)}), 502


@pos_bp.route("/pos/media/<image_id>", methods=["GET"])
@_require_pos_token
def pos_media(image_id: str):
    row = db.get_pos_image(image_id)
    path = (row or {}).get("path") or os.path.join(media_dir(), image_id)
    if not path or not os.path.isfile(path):
        return jsonify({"error": "not_found"}), 404
    return send_file(path)


@pos_bp.route("/pos/login", methods=["POST"])
@_require_pos_token
def pos_login():
    body = request.get_json(silent=True) or {}
    user_id = body.get("userId")
    pin = str(body.get("pin") or "").strip()
    user = db.get_pos_user(int(user_id)) if user_id is not None else None
    if not user:
        return jsonify({"error": "user_not_found"}), 404
    expected = str(user.get("pin") or "").strip()
    if expected and expected != pin:
        return jsonify({"error": "invalid_pin"}), 401
    return jsonify({"ok": True, "user": {"id": user["id"], "name": user["name"]}})


def _mesas_com_status_uniplus() -> List[Dict[str, Any]]:
    """Lista do catálogo local + ocupação lida do Uniplus (conta aberta)."""
    mesas = db.list_pos_mesas()
    open_by_num: Dict[int, Dict[str, Any]] = {}
    try:
        for conta in list_open_contas(db, tipopedido=1):
            open_by_num[int(conta["numeromesa"])] = conta
    except Exception as exc:
        print(f"[POS] status Uniplus indisponível: {exc}")
        return mesas

    used_nums = set()
    for mesa in mesas:
        num = parse_numeromesa(mesa.get("number")) or parse_numeromesa(mesa.get("name"))
        conta = open_by_num.get(num) if num is not None else None
        if conta:
            mesa["status"] = "ocupada"
            if conta.get("cliente"):
                mesa["contactName"] = conta["cliente"]
            mesa["valortotal"] = float(conta.get("valortotal") or 0)
            used_nums.add(num)
        else:
            mesa["status"] = "livre"
            mesa["contactName"] = None
            mesa["valortotal"] = 0
    for num, conta in open_by_num.items():
        if num in used_nums:
            continue
        mesas.append(
            {
                "id": -num,
                "number": str(num),
                "name": f"Mesa {num}",
                "type": "mesa",
                "status": "ocupada",
                "formId": None,
                "contactName": conta.get("cliente") or None,
                "valortotal": float(conta.get("valortotal") or 0),
                "displayOrder": num,
                "section": None,
            }
        )
    return mesas


@pos_bp.route("/pos/mesas", methods=["GET"])
@_require_pos_token
def pos_mesas():
    return jsonify({"mesas": _mesas_com_status_uniplus()})


@pos_bp.route("/pos/conta", methods=["GET"])
@_require_pos_token
def pos_conta():
    numeromesa = parse_numeromesa(request.args.get("numeromesa"))
    if not numeromesa:
        return jsonify({"error": "numeromesa_required"}), 400
    try:
        return jsonify(get_open_mesa_conta(db, numeromesa))
    except Exception as exc:
        return jsonify({"error": str(exc), "open": False, "itens": []}), 502


@pos_bp.route("/pos/printers/cancel-queue", methods=["POST"])
@_require_pos_token
def pos_cancel_print_queue():
    body = request.get_json(silent=True) or {}
    device_id = str(body.get("deviceId") or body.get("device_id") or "").strip()
    from agent import mark_print_queue_done

    info = mark_print_queue_done(device_id)
    if device_id and not info.get("drained"):
        return jsonify({"ok": False, "error": "printer_not_found", "results": []}), 404
    if not info.get("ok"):
        return jsonify({"ok": False, "error": info.get("message") or "no_printers", "results": []}), 400
    return jsonify({"ok": True, "message": info.get("message"), "drained": info.get("drained") or 0})


@pos_bp.route("/pos/pedidos", methods=["GET"])
@_require_pos_token
def pos_pedidos():
    try:
        itens = list_pedidos_dia(db)
    except Exception as exc:
        return jsonify({"error": str(exc), "itens": [], "printers": []}), 502
    grupos = _grupo_by_codigo()
    printers = _printer_catalog()
    routes = _print_routes()
    enriched = []
    for item in itens:
        codigo = str(item.get("codigoproduto") or "").strip()
        nome = str(item.get("nomeproduto") or "").strip().lower()
        grupo = grupos.get(codigo) or grupos.get(f"name:{nome}") or "Outros"
        assigned = _assign_printer(grupo, printers, routes)
        enriched.append(
            {
                "id": item["id"],
                "numeromesa": item["numeromesa"],
                "cliente": item.get("cliente") or "",
                "nomeproduto": item.get("nomeproduto") or "",
                "quantidade": item.get("quantidade") or 0,
                "observacao": item.get("observacao") or "",
                "hora": item.get("hora") or "",
                "grupo": grupo,
                "entregue": bool(item.get("entregue")),
                "printerDeviceId": assigned["printerDeviceId"],
                "printerName": assigned["printerName"],
            }
        )
    return jsonify({"printers": printers, "itens": enriched})


@pos_bp.route("/pos/pedidos/<int:item_id>/entregue", methods=["POST"])
@_require_pos_token
def pos_pedido_entregue(item_id: int):
    body = request.get_json(silent=True) or {}
    flag = body.get("entregue")
    if flag is None:
        flag = True
    try:
        ok = set_item_entregue(db, item_id, bool(flag))
    except Exception as exc:
        return jsonify({"error": str(exc)}), 502
    if not ok:
        return jsonify({"error": "item_not_found"}), 404
    return jsonify({"ok": True, "id": item_id, "entregue": bool(flag)})


@pos_bp.route("/pos/mesas/<int:mesa_id>/ocupar", methods=["POST"])
@_require_pos_token
def pos_ocupar(mesa_id: int):
    body = request.get_json(silent=True) or {}
    customer_name = str(body.get("customerName") or "").strip() or "Cliente"
    mesa = db.get_pos_mesa(mesa_id)
    if not mesa:
        return jsonify({"error": "mesa_not_found"}), 404
    updated = db.update_pos_mesa(mesa_id, status="ocupada", contact_name=customer_name)
    return jsonify({"mesa": updated})


@pos_bp.route("/pos/orders", methods=["POST"])
@_require_pos_token
def pos_orders():
    body = request.get_json(silent=True) or {}
    client_order_id = str(body.get("clientOrderId") or "").strip() or str(uuid.uuid4())
    existing = db.claim_pos_order(client_order_id)
    if existing:
        if existing.get("pending"):
            return jsonify({"error": "order_in_progress", "clientOrderId": client_order_id}), 409
        if existing.get("ok") and not existing.get("printed"):
            print_payload = _order_print_payload(body, existing)
            print_info = _print_kitchen(print_payload)
            existing["printed"] = bool(print_info.get("printed"))
            existing["printError"] = print_info.get("error") or ""
            db.save_pos_order(client_order_id, existing)
        return jsonify({"ok": True, "reused": True, **existing})

    menu_items = body.get("menuItems") or []
    if not menu_items:
        return jsonify({"error": "empty_order"}), 400

    mesa = None
    table_id = body.get("tableId")
    if table_id is not None:
        mesa = db.get_pos_mesa(int(table_id))
    table_number = str(
        body.get("tableNumber")
        or (mesa or {}).get("number")
        or (mesa or {}).get("name")
        or ""
    ).strip()
    table_num_int = parse_numeromesa(table_number) or parse_numeromesa(
        (mesa or {}).get("name")
    )
    customer_name = str(body.get("customerName") or (mesa or {}).get("contactName") or "Cliente").strip()
    garcom_name = str(body.get("garcomName") or "").strip()
    protocol = str(
        body.get("protocol")
        or f"POS-{table_num_int or table_number or 'M'}-{int(datetime.now().timestamp())}"
    )[:40]

    itens = _build_uniplus_items(menu_items, protocol)
    if not itens:
        return jsonify({"error": "no_items"}), 400

    total = round(sum(float(it.get("valortotal") or 0) for it in itens), 2)
    now = datetime.now(timezone.utc)
    # PDV de mesa no Uniplus usa tipopedido=1; 0 é delivery e some da sala.
    try:
        tipopedido = int(db.get_config("uniplus_mesa_tipopedido") or 1)
    except (TypeError, ValueError):
        tipopedido = 1
    if tipopedido == 0:
        tipopedido = 1
    conteudo = {
        "event": "uniplus.delivery",
        "protocol": protocol,
        "orderType": "mesa",
        "contamesa": {
            "tipopedido": tipopedido,
            "numeromesa": table_num_int,
            "nome": customer_name[:60],
            "nomecliente": customer_name[:60],
            "valortotal": total,
            "valorcombinado": total,
            "valorentrega": 0,
            "valoroutros": total,
            "obs": "",
            "hash": str(uuid.uuid4()),
            "orderidintegracao": protocol,
            "data": now.date().isoformat(),
            "horaabertura": now.isoformat(),
            "horaultimoconsumo": now.isoformat(),
            "horapedidoefetuado": now.isoformat(),
        },
        "itens": itens,
    }

    try:
        uniplus = handle_uniplus_job(db, conteudo)
    except Exception as exc:
        return jsonify({"error": str(exc), "clientOrderId": client_order_id}), 502

    print_payload = {
        "formName": "Pedido mesa",
        "protocol": protocol,
        "tableNumber": table_number,
        "garcomName": garcom_name,
        "orderType": "mesa",
        "menuItems": menu_items,
        "responder": {"name": customer_name},
        "submittedAt": now.isoformat(),
    }
    result = {
        "ok": True,
        "printed": False,
        "printError": "",
        "clientOrderId": client_order_id,
        "protocol": protocol,
        "tableNumber": table_number,
        "garcomName": garcom_name,
        "customerName": customer_name,
        "uniplus": {
            "contaId": uniplus.get("conta_id"),
            "numeromesa": uniplus.get("numeromesa"),
            "action": uniplus.get("action"),
        },
    }
    db.save_pos_order(client_order_id, result)
    print_info = _print_kitchen(print_payload)
    result["printed"] = bool(print_info.get("printed"))
    result["printError"] = print_info.get("error") or ""
    db.save_pos_order(client_order_id, result)
    return jsonify(result)


def _order_print_payload(body: Dict[str, Any], existing: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "formName": "Pedido mesa",
        "protocol": existing.get("protocol") or "",
        "tableNumber": existing.get("tableNumber") or body.get("tableNumber") or "",
        "garcomName": existing.get("garcomName") or body.get("garcomName") or "",
        "orderType": "mesa",
        "menuItems": body.get("menuItems") or [],
        "responder": {"name": existing.get("customerName") or body.get("customerName") or "Cliente"},
        "submittedAt": datetime.now(timezone.utc).isoformat(),
    }
