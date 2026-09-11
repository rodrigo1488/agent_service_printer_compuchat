"""Pizza R$ 85 + borda R$ 10 deve fechar R$ 95 no cupom e no Uniplus."""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from printer_service import _item_dry_unit  # noqa: E402
from receipt_formatter import format_order_receipt  # noqa: E402
from pos_api import _build_uniplus_items  # noqa: E402


class PizzaBordaPricingTest(unittest.TestCase):
    def test_formatter_pizza_borda_seco_85_total_95(self):
        receipt = format_order_receipt(
            {
                "formName": "Pedido mesa",
                "protocol": "POS-1",
                "menuItems": [
                    {
                        "productName": "Pizza",
                        "grupo": "Pizzas",
                        "quantity": 1,
                        "productValue": 85,
                        "addons": [{"label": "Borda", "value": 10}],
                    }
                ],
            }
        )
        pizza = receipt["items_by_group"]["Pizzas"][0]
        self.assertEqual(pizza["base_value"], 85)
        self.assertEqual(pizza["value"], 95)
        self.assertEqual(receipt["total"], 95)
        self.assertEqual(_item_dry_unit(pizza), 85)

    def test_dry_unit_fallback_quando_value_ja_e_combinado(self):
        item = {
            "value": 95,
            "addons": [{"label": "Borda", "value": 10}],
        }
        self.assertEqual(_item_dry_unit(item), 85)

    def test_uniplus_borda_vinculada_soma_95(self):
        itens = _build_uniplus_items(
            [
                {
                    "productName": "Pizza",
                    "productValue": 85,
                    "quantity": 1,
                    "addons": [
                        {"label": "Borda", "value": 10, "idUniplus": "B10"},
                    ],
                }
            ],
            "PROT",
        )
        total = round(sum(float(i["valortotal"]) for i in itens), 2)
        self.assertEqual(total, 95.0)
        precos = sorted(float(i["precounitario"]) for i in itens)
        self.assertEqual(precos, [10.0, 85.0])

    def test_uniplus_borda_sem_codigo_embute_95(self):
        itens = _build_uniplus_items(
            [
                {
                    "productName": "Pizza",
                    "productValue": 85,
                    "quantity": 1,
                    "addons": [{"label": "Borda", "value": 10}],
                }
            ],
            "PROT",
        )
        self.assertEqual(len(itens), 1)
        self.assertEqual(float(itens[0]["precounitario"]), 95.0)
        self.assertEqual(float(itens[0]["valortotal"]), 95.0)


if __name__ == "__main__":
    unittest.main()
