"""
Script para testar o webhook de impressão
"""
import requests
import json
from datetime import datetime

# URL do webhook
WEBHOOK_URL = "http://localhost:5000/webhook"

# Exemplo de payload de pedido
test_payload = {
    "event": "form.submitted",
    "formId": 1,
    "formName": "Cardápio",
    "responseId": 123,
    "submittedAt": datetime.now().isoformat(),
    "responder": {
        "name": "João Silva",
        "phone": "5511999999999",
        "email": "joao@email.com"
    },
    "answers": [
        {
            "fieldId": 1,
            "label": "Observações",
            "answer": "Sem cebola, sem tomate"
        },
        {
            "fieldId": 2,
            "label": "Forma de Pagamento",
            "answer": "Cartão de Crédito"
        }
    ],
    "menuItems": [
        {
            "productId": 1,
            "productName": "Hambúrguer Artesanal",
            "quantity": 2,
            "productValue": 25.00,
            "grupo": "Lanches"
        },
        {
            "productId": 2,
            "productName": "Batata Frita",
            "quantity": 1,
            "productValue": 15.00,
            "grupo": "Acompanhamentos"
        },
        {
            "productId": 3,
            "productName": "Coca-Cola 350ml",
            "quantity": 2,
            "productValue": 8.00,
            "grupo": "Bebidas"
        },
        {
            "productId": 4,
            "productName": "Sobremesa do Dia",
            "quantity": 1,
            "productValue": 12.00,
            "grupo": "Sobremesas"
        }
    ]
}

def test_webhook():
    """Testa o webhook enviando um pedido de exemplo"""
    print("=" * 50)
    print("Teste do Webhook de Impressão")
    print("=" * 50)
    print(f"Enviando pedido para: {WEBHOOK_URL}")
    print()
    
    try:
        response = requests.post(
            WEBHOOK_URL,
            json=test_payload,
            headers={"Content-Type": "application/json"},
            timeout=10
        )
        
        print(f"Status Code: {response.status_code}")
        print(f"Response: {json.dumps(response.json(), indent=2, ensure_ascii=False)}")
        
        if response.status_code == 200:
            print("\n✅ Pedido enviado com sucesso!")
            print("Verifique se a impressora imprimiu o recibo.")
        else:
            print(f"\n❌ Erro ao enviar pedido: {response.status_code}")
            
    except requests.exceptions.ConnectionError:
        print("❌ Erro: Não foi possível conectar ao servidor.")
        print("Certifique-se de que o servidor Flask está rodando.")
    except Exception as e:
        print(f"❌ Erro: {str(e)}")

def test_config():
    """Testa a configuração da impressora"""
    print("=" * 50)
    print("Teste de Configuração")
    print("=" * 50)
    
    # Ler configuração atual
    try:
        response = requests.get("http://localhost:5000/config", timeout=5)
        if response.status_code == 200:
            config = response.json()
            print("Configuração atual:")
            print(json.dumps(config, indent=2, ensure_ascii=False))
        else:
            print(f"Erro ao ler configuração: {response.status_code}")
    except Exception as e:
        print(f"Erro: {str(e)}")

if __name__ == "__main__":
    import sys
    
    if len(sys.argv) > 1 and sys.argv[1] == "config":
        test_config()
    else:
        test_webhook()
