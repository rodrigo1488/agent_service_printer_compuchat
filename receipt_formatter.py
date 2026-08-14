"""Formata dados do pedido para impressão.
Não usa zoneinfo (evita erro no Windows sem tzdata). Conversão UTC -> Brasília por timedelta.
"""
from datetime import datetime, timezone, timedelta

# Brasília = UTC-3 (sem horário de verão). Não usar ZoneInfo aqui.
BRASIL_UTC_OFFSET_HOURS = 3


def _utc_to_brasil_str(dt_utc):
    """Converte datetime UTC para string no fuso de Brasília (dd/mm/yyyy HH:MM:SS)."""
    if dt_utc.tzinfo is not None:
        dt_utc = (dt_utc.replace(tzinfo=None) - (dt_utc.utcoffset() or timedelta(0)))
    dt_brasil = dt_utc - timedelta(hours=BRASIL_UTC_OFFSET_HOURS)
    return dt_brasil.strftime("%d/%m/%Y %H:%M:%S")


def format_order_receipt(data: dict) -> dict:
    """Formata os dados do pedido para impressão."""
    form_name = data.get("formName", "Pedido")
    protocol = data.get("protocol", "")
    table_number = data.get("tableNumber", "")
    garcom_name = data.get("garcomName", "")
    # Taxa de entrega: aceitar no topo do payload ou dentro de metadata
    _raw_fee = data.get("deliveryFee") or data.get("delivery_fee")
    if _raw_fee is None and isinstance(data.get("metadata"), dict):
        _raw_fee = (data.get("metadata") or {}).get("deliveryFee")
    delivery_fee = float(_raw_fee or 0)
    responder = data.get("responder", {})
    menu_items = data.get("menuItems", [])
    answers = data.get("answers", [])
    all_answers = data.get("allAnswers") or answers
    submitted_at = data.get("submittedAt") or data.get("submitted_at") or datetime.now(timezone.utc).isoformat()
    delivery_scan_token = data.get("deliveryScanToken", "")
    delivery_scan_url = data.get("deliveryScanUrl", "")
    try:
        qr_module_size = int(data.get("printQrModuleSize") or 10)
    except (TypeError, ValueError):
        qr_module_size = 10
    qr_module_size = min(16, max(4, qr_module_size))
    try:
        font_scale = int(
            data.get("printFontScale")
            or data.get("font_scale")
            or (isinstance(data.get("metadata"), dict) and data.get("metadata", {}).get("printFontScale"))
            or 1
        )
    except (TypeError, ValueError):
        font_scale = 1
    font_scale = min(3, max(1, font_scale))

    fulfillment_mode = str(data.get("fulfillmentMode") or "").strip().lower()
    if not fulfillment_mode and isinstance(data.get("metadata"), dict):
        fulfillment_mode = str(
            (data.get("metadata") or {}).get("fulfillmentMode") or ""
        ).strip().lower()
    is_pickup = (
        fulfillment_mode == "pickup"
        or data.get("pickup") is True
        or data.get("retirada") is True
        or str(data.get("pickup") or "").lower() in ("1", "true", "sim")
        or str(data.get("retirada") or "").lower() in ("1", "true", "sim")
    )
    if not is_pickup:
        for answer in all_answers:
            label = str(answer.get("label", "")).lower()
            if not (
                ("tipo" in label and ("pedido" in label or "entrega" in label))
                or "modalidade" in label
            ):
                continue
            val = str(answer.get("answer", "")).lower()
            if any(
                token in val
                for token in ("retirada", "balcão", "balcao", "local", "pickup", "buscar", "pegar", "levar")
            ) and not any(token in val for token in ("entrega", "delivery")):
                is_pickup = True
                break
            if "retirada" in val or "balc" in val or "local" in val:
                is_pickup = True
                break
    if is_pickup:
        delivery_scan_token = ""
        delivery_scan_url = ""

    try:
        if isinstance(submitted_at, (int, float)):
            dt = datetime.utcfromtimestamp(float(submitted_at) / 1000.0 if float(submitted_at) > 1e12 else float(submitted_at))
        else:
            s = str(submitted_at).strip().replace("Z", "+00:00")
            dt = datetime.fromisoformat(s)
        date_str = _utc_to_brasil_str(dt)
    except (ValueError, TypeError):
        date_str = str(submitted_at)

    total = 0
    items_by_group = {}

    for item in menu_items:
        quantity = int(item.get("quantity", 1) or 1)
        if quantity <= 0:
            continue
        base_value = float(item.get("productValue") or item.get("product_value") or 0)
        addons_total = float(item.get("addonsTotal") or item.get("addons_total") or 0)
        value = base_value + addons_total
        item_total = quantity * value
        total += item_total

        grupo = item.get("grupo") or "Outros"
        if grupo not in items_by_group:
            items_by_group[grupo] = []

        addons_raw = item.get("addons") or []
        addons_list = []
        for a in addons_raw if isinstance(addons_raw, list) else []:
            if not isinstance(a, dict):
                continue
            label = a.get("label") or "Adicional"
            addon_val = float(a.get("value", 0) or 0)
            addons_list.append({"label": str(label), "value": addon_val})

        combo_raw = item.get("comboItems") or item.get("combo_items") or []
        combo_list = []
        for ci in combo_raw if isinstance(combo_raw, list) else []:
            if not isinstance(ci, dict):
                continue
            ci_name = ci.get("productName") or ci.get("product_name") or "Item"
            ci_qty = int(ci.get("quantity") or 1)
            ci_val = float(ci.get("value") or 0)
            combo_list.append({"name": str(ci_name), "quantity": ci_qty, "value": ci_val})

        name = item.get("productName") or "Produto"
        item_type = str(item.get("type") or "").strip()
        half_lines = []

        if item_type == "halfAndHalf":
            h1 = str(item.get("half1Name") or item.get("half1_name") or "").strip()
            h2 = str(item.get("half2Name") or item.get("half2_name") or "").strip()
            if (not h1 or not h2) and name:
                # Fallback: "Meio a meio: Sabor A / Sabor B"
                raw = name
                for prefix in ("Meio a meio:", "MEIO A MEIO:", "meio a meio:"):
                    if raw.lower().startswith(prefix.lower()):
                        raw = raw[len(prefix):].strip()
                        break
                if " / " in raw:
                    parts = [p.strip() for p in raw.split(" / ", 1)]
                    if len(parts) == 2:
                        h1 = h1 or parts[0]
                        h2 = h2 or parts[1]
            if h1:
                half_lines.append(f"1/2 {h1.upper()}")
            if h2:
                half_lines.append(f"1/2 {h2.upper()}")
            name = "MEIO A MEIO"
        elif item_type == "combo" and "combo" not in str(name).lower():
            name = f"{name} (Combo)"

        items_by_group[grupo].append({
            "name": name,
            "quantity": quantity,
            "value": value,
            "total": item_total,
            "addons": addons_list,
            "combo_items": combo_list,
            "half_lines": half_lines,
            "type": item_type,
            "observation": str(item.get("observation") or item.get("observacao") or "").strip(),
        })

    custom_info = {}
    for answer in answers:
        label = answer.get("label", "")
        answer_value = answer.get("answer", "")
        if label.lower() not in ["nome", "telefone", "phone"] and answer_value:
            custom_info[label] = answer_value

    total_with_fee = total + delivery_fee
    return {
        "form_name": form_name,
        "protocol": protocol,
        "table_number": table_number,
        "garcom_name": garcom_name,
        "date": date_str,
        "customer": {
            "name": responder.get("name", "Cliente"),
            "phone": responder.get("phone", ""),
            "email": responder.get("email", ""),
        },
        "items_by_group": items_by_group,
        "subtotal": total,
        "delivery_fee": delivery_fee,
        "total": total_with_fee,
        "custom_info": custom_info,
        "delivery_scan_token": delivery_scan_token,
        "delivery_scan_url": delivery_scan_url,
        "qr_module_size": qr_module_size,
        "font_scale": font_scale,
        "printFontScale": font_scale,
        "is_pickup": is_pickup,
        "fulfillment_mode": fulfillment_mode or ("pickup" if is_pickup else ""),
    }
