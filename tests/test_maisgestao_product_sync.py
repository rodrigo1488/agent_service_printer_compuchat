"""Catálogo PDV Mais Gestão → códigos Compuchat e poll automático."""
import os
import sys
import unittest
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from maisgestao_handler import (  # noqa: E402
    codigo_from_pdv_product,
    map_pdv_catalog_products,
)
import product_sync  # noqa: E402


CATALOG = {
    "grupos": [{"id": "g-bebida", "nome": "Bebidas"}],
    "gruposGourmet": [{"id": "gg-pizza", "nome": "Pizzas"}],
    "produtos": [
        {
            "id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            "descricao": "Pizza Calabresa G",
            "preco": 55.5,
            "codigo": 1001,
            "ean": "7891234567890",
            "idgrupo": "g-bebida",
            "idgrupogourmet": "gg-pizza",
        },
        {
            "id": "11111111-2222-3333-4444-555555555555",
            "descricao": "Coca Lata",
            "preco": 6,
            "codigo": None,
            "ean": "7890000000017",
            "idgrupo": "g-bebida",
        },
        {
            "id": "99999999-8888-7777-6666-555555555555",
            "descricao": "Sem código nem EAN",
            "preco": 1,
            "codigo": 0,
            "ean": None,
        },
        {
            "id": "abc",
            "descricao": "Água",
            "preco": 3,
            "codigo": 42,
            "idgrupo": "g-bebida",
        },
    ],
}


class CodigoFromPdvProductTest(unittest.TestCase):
    def test_prefere_codigo_interno(self):
        self.assertEqual(
            codigo_from_pdv_product({"codigo": 1001, "ean": "7891234567890"}),
            "1001",
        )

    def test_codigo_float_json(self):
        self.assertEqual(codigo_from_pdv_product({"codigo": 12.0}), "12")

    def test_fallback_ean(self):
        self.assertEqual(
            codigo_from_pdv_product({"codigo": None, "ean": "7890000000017"}),
            "7890000000017",
        )

    def test_ignora_uuid_sem_codigo(self):
        self.assertIsNone(
            codigo_from_pdv_product(
                {"id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "codigo": 0}
            )
        )


class MapPdvCatalogTest(unittest.TestCase):
    def test_mapeia_codigo_ean_grupo_e_filtra_sem_chave(self):
        items = map_pdv_catalog_products(CATALOG)
        by_code = {p["codigo"]: p for p in items}
        self.assertEqual(set(by_code), {"1001", "7890000000017", "42"})
        pizza = by_code["1001"]
        self.assertEqual(pizza["nome"], "Pizza Calabresa G")
        self.assertEqual(pizza["preco"], 55.5)
        self.assertEqual(pizza["grupo"], "Pizzas")
        self.assertEqual(pizza["fingerprint"], "Pizza Calabresa G|55.5000|Pizzas")
        self.assertEqual(by_code["42"]["grupo"], "Bebidas")
        self.assertEqual(by_code["7890000000017"]["nome"], "Coca Lata")

    def test_filtro_q(self):
        items = map_pdv_catalog_products(CATALOG, q="coca")
        self.assertEqual([p["codigo"] for p in items], ["7890000000017"])


class PollMaisgestaoTest(unittest.TestCase):
    def test_poll_habilita_novos_pula_desligados_e_inalterados(self):
        mapped = map_pdv_catalog_products(CATALOG)
        known = {
            "42": {
                "codigo": "42",
                "enabled": False,
                "fingerprint": mapped[2]["fingerprint"]
                if mapped[2]["codigo"] == "42"
                else "",
                "last_error": "",
            },
            "1001": {
                "codigo": "1001",
                "enabled": True,
                "fingerprint": next(p["fingerprint"] for p in mapped if p["codigo"] == "1001"),
                "last_error": "",
            },
        }

        class FakeDb:
            def list_sync_products(self, enabled_only=False):
                return list(known.values())

            def set_sync_product_enabled(self, codigo, enabled, nome="", preco=0.0):
                known[codigo] = {
                    "codigo": codigo,
                    "enabled": enabled,
                    "fingerprint": "",
                    "last_error": "",
                    "nome": nome,
                    "preco": preco,
                }

        upserts = []

        with patch.object(product_sync, "is_maisgestao_source", return_value=True), patch.object(
            product_sync, "is_product_source_enabled", return_value=True
        ), patch.object(
            product_sync, "list_source_products", return_value=mapped
        ), patch.object(
            product_sync, "upsert_many", side_effect=lambda items: upserts.append(items) or {"synced": len(items), "failed": 0, "total": len(items)}
        ), patch.object(product_sync, "db", FakeDb()):
            sent = product_sync._poll_maisgestao_catalog()

        self.assertEqual(len(upserts), 1)
        enviados = {p["codigo"] for p in upserts[0]}
        self.assertIn("7890000000017", enviados)
        self.assertNotIn("42", enviados)
        self.assertNotIn("1001", enviados)
        self.assertEqual(sent, 1)
        self.assertTrue(known["7890000000017"]["enabled"])


if __name__ == "__main__":
    unittest.main()
