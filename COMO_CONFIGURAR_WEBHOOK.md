# Como Configurar Webhook no Formulário de Cardápio

Este guia explica como configurar o webhook para receber pedidos do formulário de cardápio e imprimir automaticamente em uma impressora de rede.

## 📋 Índice

1. [O que é um Webhook?](#o-que-é-um-webhook)
2. [Como Funciona](#como-funciona)
3. [Configuração no Formulário](#configuração-no-formulário)
4. [Formato dos Dados Enviados](#formato-dos-dados-enviados)
5. [Exemplos Práticos](#exemplos-práticos)
6. [Solução de Problemas](#solução-de-problemas)

---

## 🔗 O que é um Webhook?

Um **webhook** é uma forma de comunicação entre sistemas onde um serviço envia automaticamente dados para outro quando um evento acontece. No caso do formulário de cardápio, quando um cliente finaliza um pedido, o sistema envia automaticamente todas as informações do pedido para a URL do webhook configurada.

### Vantagens do Webhook:

- ✅ **Automático**: Não precisa consultar o sistema para ver novos pedidos
- ✅ **Tempo Real**: Recebe os dados imediatamente após o pedido
- ✅ **Integração**: Permite conectar com outros sistemas (impressoras, ERPs, etc.)
- ✅ **Confiável**: O sistema tenta enviar até confirmar o recebimento

---

## ⚙️ Como Funciona

### Fluxo Completo:

```
1. Cliente preenche o formulário de cardápio
   ↓
2. Cliente seleciona produtos e quantidades
   ↓
3. Cliente preenche dados (nome, telefone, etc.)
   ↓
4. Cliente finaliza o pedido
   ↓
5. Sistema salva o pedido no banco de dados
   ↓
6. Sistema envia mensagem WhatsApp (se configurado)
   ↓
7. Sistema envia dados para o Webhook (se configurado)
   ↓
8. Servidor webhook recebe os dados
   ↓
9. Servidor formata e imprime o pedido na impressora
```

### O que acontece quando o webhook é chamado:

1. O sistema CompuChat faz uma requisição HTTP POST para a URL do webhook
2. Envia um JSON com todas as informações do pedido
3. Aguarda resposta de confirmação (até 5 segundos)
4. Se o webhook responder com sucesso (status 200), considera enviado
5. Se houver erro, registra no log mas não impede o salvamento do pedido

---

## 🛠️ Configuração no Formulário

### Passo a Passo:

#### 1. Acesse o Formulário

1. No menu lateral, clique em **"Formulários"**
2. Selecione o formulário de cardápio que deseja configurar
3. Ou crie um novo formulário do tipo **"Cardápio"**

#### 2. Aba "Integrações"

1. No editor do formulário, clique na aba **"Integrações"** (última aba)
2. Você verá as opções de integração disponíveis

#### 3. Ativar Webhook

1. Localize a opção **"Enviar Webhook"**
2. Marque a checkbox para ativar o envio de webhook
3. Um campo de texto aparecerá abaixo para você inserir a URL

#### 4. Configurar URL do Webhook

1. No campo **"URL do Webhook"**, insira a URL completa do seu servidor webhook
2. Exemplo: `http://192.168.1.50:5000/webhook`
3. Ou: `https://seu-dominio.com/webhook`

**⚠️ Importante:**
- Use `http://` para servidores locais
- Use `https://` para servidores externos
- Inclua a porta se necessário (ex: `:5000`)
- Não adicione barra no final (use `/webhook` não `/webhook/`)

#### 5. Salvar Configuração

1. Clique no botão **"Salvar"** no canto superior direito
2. Aguarde a confirmação de salvamento
3. Pronto! O webhook está configurado

### Visual da Configuração:

```
┌─────────────────────────────────────────┐
│  Formulário: Cardápio                   │
├─────────────────────────────────────────┤
│  [Configurações Gerais] [Campos]        │
│  [Aparência] [Integrações] ← Clique aqui│
├─────────────────────────────────────────┤
│                                          │
│  ☑ Enviar Webhook                       │
│                                          │
│  URL do Webhook                          │
│  ┌───────────────────────────────────┐ │
│  │ http://192.168.1.50:5000/webhook  │ │
│  └───────────────────────────────────┘ │
│                                          │
│  [Salvar]                                │
└─────────────────────────────────────────┘
```

---

## 📦 Formato dos Dados Enviados

Quando um pedido é finalizado, o sistema envia um JSON com a seguinte estrutura:

### Estrutura Completa:

```json
{
  "event": "form.submitted",
  "formId": 1,
  "formName": "Cardápio",
  "responseId": 123,
  "submittedAt": "2024-02-15T14:30:00.000Z",
  "responder": {
    "name": "João Silva",
    "phone": "5511999999999",
    "email": "joao@email.com"
  },
  "answers": [
    {
      "fieldId": 5,
      "label": "Observações",
      "answer": "Sem cebola"
    },
    {
      "fieldId": 6,
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
    }
  ]
}
```

### Descrição dos Campos:

| Campo | Tipo | Descrição |
|-------|------|-----------|
| `event` | string | Sempre `"form.submitted"` para pedidos |
| `formId` | number | ID do formulário no sistema |
| `formName` | string | Nome do formulário |
| `responseId` | number | ID único da resposta/pedido |
| `submittedAt` | string | Data e hora do pedido (ISO 8601) |
| `responder.name` | string | Nome do cliente |
| `responder.phone` | string | Telefone do cliente |
| `responder.email` | string | Email do cliente (se preenchido) |
| `answers` | array | Campos customizados preenchidos |
| `menuItems` | array | Lista de produtos do pedido |

### Estrutura de `menuItems`:

Cada item do pedido contém:

```json
{
  "productId": 1,              // ID do produto
  "productName": "Hambúrguer", // Nome do produto
  "quantity": 2,               // Quantidade
  "productValue": 25.00,       // Valor unitário
  "grupo": "Lanches"           // Grupo do produto
}
```

---

## 💡 Exemplos Práticos

### Exemplo 1: Webhook Local (Rede Interna)

Se você está rodando o servidor webhook na mesma rede:

```
URL: http://192.168.1.50:5000/webhook
```

**Quando usar:**
- Servidor webhook na mesma rede local
- Impressora na mesma rede
- Não precisa de acesso externo

### Exemplo 2: Webhook em Servidor Externo

Se você tem um servidor na internet:

```
URL: https://api.seudominio.com/webhook/pedidos
```

**Quando usar:**
- Servidor webhook na nuvem
- Precisa de acesso de qualquer lugar
- Requer certificado SSL (https)

### Exemplo 3: Webhook com Autenticação

Alguns servidores requerem autenticação:

```
URL: https://api.seudominio.com/webhook?token=SEU_TOKEN
```

**Quando usar:**
- Servidor webhook com segurança
- Precisa validar requisições
- Protege contra acesso não autorizado

---

## 🔍 Solução de Problemas

### Webhook não está recebendo dados

**Verificações:**

1. ✅ **URL está correta?**
   - Verifique se não há espaços ou caracteres especiais
   - Confirme se a porta está correta
   - Teste a URL no navegador (deve retornar erro, mas confirma que está acessível)

2. ✅ **Servidor webhook está rodando?**
   - Verifique se o servidor Flask está ativo
   - Teste o endpoint `/health` no navegador
   - Deve retornar: `{"status": "ok", ...}`

3. ✅ **Firewall está bloqueando?**
   - Verifique se a porta está aberta no firewall
   - Teste com `telnet IP PORTA` (Windows) ou `nc -zv IP PORTA` (Linux)

4. ✅ **Rede está acessível?**
   - Teste ping: `ping IP_DO_SERVIDOR`
   - Verifique se estão na mesma rede (se for local)

### Erro "Connection refused"

**Causa:** Servidor webhook não está rodando ou porta incorreta

**Solução:**
1. Inicie o servidor webhook
2. Verifique se está escutando na porta correta
3. Confirme o IP do servidor

### Erro "Timeout"

**Causa:** Servidor demora mais de 5 segundos para responder

**Solução:**
1. Otimize o código do webhook
2. Processe a impressão de forma assíncrona
3. Retorne resposta rápida e processe depois

### Dados não estão completos

**Verificações:**

1. ✅ **Formulário está configurado corretamente?**
   - Verifique se é do tipo "Cardápio"
   - Confirme se tem produtos marcados como "Produto de Cardápio"

2. ✅ **Cliente preencheu todos os campos?**
   - Alguns campos podem estar vazios se não foram preenchidos

3. ✅ **Webhook está processando corretamente?**
   - Adicione logs no servidor webhook
   - Verifique o que está sendo recebido

### Impressora não imprime

**Verificações:**

1. ✅ **IP da impressora está correto?**
   - Edite `config.json` no servidor webhook
   - Ou use o endpoint `/config` para atualizar

2. ✅ **Impressora está na rede?**
   - Teste ping: `ping IP_IMPRESSORA`
   - Verifique se a impressora está ligada

3. ✅ **Porta está correta?**
   - Porta 9100 para RAW (padrão)
   - Porta 631 para IPP

---

## 📝 Checklist de Configuração

Use este checklist para garantir que tudo está configurado:

- [ ] Servidor webhook instalado e rodando
- [ ] IP da impressora configurado no `config.json`
- [ ] Teste de conectividade com impressora (ping)
- [ ] Formulário criado e configurado como "Cardápio"
- [ ] Produtos cadastrados e marcados como "Produto de Cardápio"
- [ ] Webhook ativado no formulário
- [ ] URL do webhook configurada corretamente
- [ ] Formulário salvo
- [ ] Teste realizado com pedido de exemplo

---

## 🧪 Como Testar

### 1. Teste do Servidor Webhook

```bash
# Teste se o servidor está respondendo
curl http://localhost:5000/health

# Deve retornar: {"status": "ok", ...}
```

### 2. Teste com Script Python

```bash
cd webhook_lanch
python test_webhook.py
```

### 3. Teste com Pedido Real

1. Acesse o formulário público
2. Selecione alguns produtos
3. Preencha os dados
4. Finalize o pedido
5. Verifique se o webhook recebeu os dados
6. Confirme se a impressora imprimiu

---

## 📞 Suporte

Se você encontrar problemas:

1. Verifique os logs do servidor webhook
2. Verifique os logs do CompuChat (backend)
3. Teste a conectividade de rede
4. Confirme todas as configurações

---

## 📚 Recursos Adicionais

- [README do Webhook](./README.md) - Documentação completa do servidor
- [Documentação Flask](https://flask.palletsprojects.com/) - Framework usado
- [Protocolo IPP](https://www.pwg.org/ipp/) - Para impressoras IPP

---

**Última atualização:** 2024-02-15
