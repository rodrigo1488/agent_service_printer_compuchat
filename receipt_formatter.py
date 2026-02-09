"""Formata dados do pedido para impressão."""
from datetime import datetime


def format_order_receipt(data: dict) -> dict:
    """Formata os dados do pedido para impressão."""
    form_name = data.get("formName", "Pedido")
    protocol = data.get("protocol", "")
    table_number = data.get("tableNumber", "")
    garcom_name = data.get("garcomName", "")
    responder = data.get("responder", {})
    menu_items = data.get("menuItems", [])
    answers = data.get("answers", [])
    submitted_at = data.get("submittedAt", datetime.now().isoformat())
    delivery_scan_token = data.get("deliveryScanToken", "")
    delivery_scan_url = data.get("deliveryScanUrl", "")

    try:
        dt = datetime.fromisoformat(submitted_at.replace("Z", "+00:00"))
        date_str = dt.strftime("%d/%m/%Y %H:%M:%S")
    except (ValueError, TypeError):
        date_str = str(submitted_at)

    total = 0
    items_by_group = {}

    for item in menu_items:
        quantity = item.get("quantity", 0)
        value = float(item.get("productValue", 0))
        item_total = quantity * value
        total += item_total

        grupo = item.get("grupo", "Outros")
        if grupo not in items_by_group:
            items_by_group[grupo] = []

        items_by_group[grupo].append({
            "name": item.get("productName", "Produto"),
            "quantity": quantity,
            "value": value,
            "total": item_total,
        })

    custom_info = {}
    for answer in answers:
        label = answer.get("label", "")
        answer_value = answer.get("answer", "")
        if label.lower() not in ["nome", "telefone", "phone"] and answer_value:
            custom_info[label] = answer_value

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
        "total": total,
        "custom_info": custom_info,
        "delivery_scan_token": delivery_scan_token,
        "delivery_scan_url": delivery_scan_url,
    }
