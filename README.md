# Webhook de Impressão de Pedidos

Serviço Flask para receber webhooks de pedidos do formulário de cardápio e imprimir automaticamente em impressora de rede.

## Funcionalidades

- Recebe webhooks de pedidos do formulário de cardápio
- Formata e imprime recibo com informações do pedido
- Suporta impressoras de rede via RAW (porta 9100) ou IPP
- Configuração do IP da impressora via arquivo ou API
- Agrupa produtos por grupo no recibo
- Calcula total automaticamente

## Instalação

1. Instale as dependências:
```bash
pip install -r requirements.txt
```

2. Configure o IP da impressora editando `config.json`:
```json
{
  "printer_ip": "192.168.1.100",
  "printer_port": 9100,
  "printer_type": "raw"
}
```

Ou configure via API após iniciar o servidor.

## Uso

### Iniciar o servidor

```bash
python app.py
```

O servidor iniciará em `http://localhost:5000`

### Endpoints

#### POST /webhook
Recebe webhooks de pedidos do formulário.

**Payload esperado:**
```json
{
  "event": "form.submitted",
  "formName": "Cardápio",
  "menuItems": [
    {
      "productId": 1,
      "productName": "Hambúrguer",
      "quantity": 2,
      "productValue": 25.00,
      "grupo": "Lanches"
    }
  ],
  "responder": {
    "name": "João Silva",
    "phone": "5511999999999",
    "email": "joao@email.com"
  },
  "answers": [
    {
      "label": "Observações",
      "answer": "Sem cebola"
    }
  ]
}
```

#### GET /config
Retorna a configuração atual da impressora.

#### POST /config
Atualiza a configuração da impressora.

**Exemplo:**
```bash
curl -X POST http://localhost:5000/config \
  -H "Content-Type: application/json" \
  -d '{
    "printer_ip": "192.168.1.200",
    "printer_port": 9100,
    "printer_type": "raw"
  }'
```

#### GET /health
Verifica se o serviço está rodando.

## Configuração no Formulário

No formulário de cardápio, configure o webhook:

1. Acesse a configuração do formulário
2. Ative "Enviar Webhook"
3. Configure a URL: `http://SEU_IP:5000/webhook`
   - Exemplo: `http://192.168.1.50:5000/webhook`

## Tipos de Impressora

### RAW (Porta 9100)
Padrão para a maioria das impressoras térmicas e de rede.
- Porta padrão: 9100
- Tipo: `"raw"`

### IPP (Internet Printing Protocol)
Para impressoras que suportam IPP.
- Porta padrão: 631
- Tipo: `"ipp"`

## Formato do Recibo

O recibo impresso contém:

```
================================================
  CARDÁPIO
================================================
Data: 15/02/2024 14:30:00

CLIENTE:
  Nome: João Silva
  Telefone: 5511999999999
------------------------------------------------

* LANCHES *

Hambúrguer              2x R$ 25,00    R$ 50,00
Batata Frita           1x R$ 15,00    R$ 15,00

* BEBIDAS *

Coca-Cola               2x R$ 8,00     R$ 16,00

------------------------------------------------

INFORMAÇÕES ADICIONAIS:
  Observações: Sem cebola

TOTAL DO PEDIDO:
                                          R$ 81,00
================================================

Obrigado pela preferência!
```

## Solução de Problemas

### Impressora não imprime
1. Verifique se o IP está correto
2. Teste a conectividade: `ping IP_DA_IMPRESSORA`
3. Verifique se a porta está aberta
4. Tente mudar o tipo de impressora (raw/ipp)

### Erro de conexão
- Verifique se a impressora está na mesma rede
- Confirme que o firewall permite conexões na porta configurada
- Teste com `telnet IP_DA_IMPRESSORA PORTA`

### Caracteres especiais não aparecem
- A impressora pode não suportar UTF-8
- Tente usar apenas caracteres ASCII

## Desenvolvimento

Para desenvolvimento com auto-reload:

```bash
export FLASK_ENV=development
python app.py
```

## Licença

Este projeto é parte do sistema CompuChat.
