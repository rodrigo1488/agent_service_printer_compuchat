# 🚀 Guia Rápido - Configuração de Webhook

## 📍 Onde Configurar

### No Formulário de Cardápio:

1. **Acesse:** Formulários → Seu Formulário de Cardápio
2. **Clique na aba:** "Integrações" (última aba)
3. **Ative:** Marque ☑ "Enviar Webhook"
4. **Cole a URL:** `http://SEU_IP:5000/webhook`
5. **Salve:** Clique em "Salvar"

---

## 🎯 Exemplo de URL

### Servidor Local (mesma rede):
```
http://192.168.1.50:5000/webhook
```

### Servidor Externo:
```
https://api.seudominio.com/webhook
```

---

## ⚡ Início Rápido

### 1. Inicie o Servidor Webhook:
```bash
cd webhook_lanch
python app.py
```

### 2. Configure o IP da Impressora:
Edite `config.json`:
```json
{
  "printer_ip": "192.168.1.100",
  "printer_port": 9100,
  "printer_type": "raw"
}
```

### 3. Configure no Formulário:
- Aba "Integrações"
- Ative "Enviar Webhook"
- URL: `http://SEU_IP:5000/webhook`

### 4. Teste:
```bash
python test_webhook.py
```

---

## ✅ Checklist

- [ ] Servidor webhook rodando
- [ ] IP da impressora configurado
- [ ] Webhook ativado no formulário
- [ ] URL configurada corretamente
- [ ] Teste realizado

---

**📖 Documentação completa:** [COMO_CONFIGURAR_WEBHOOK.md](./COMO_CONFIGURAR_WEBHOOK.md)
