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
    submitted_at = data.get("submittedAt") or data.get("submitted_at") or datetime.now(timezone.utc).isoformat()
    delivery_scan_token = data.get("deliveryScanToken", "")
    delivery_scan_url = data.get("deliveryScanUrl", "")

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

        items_by_group[grupo].append({
            "name": item.get("productName") or "Produto",
            "quantity": quantity,
            "value": value,
            "total": item_total,
            "addons": addons_list,
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
    }
