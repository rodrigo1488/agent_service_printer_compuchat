# Gerar executável do Print Agent (sem console)

Instruções para gerar um `.exe` do Print Agent que roda **sem abrir janela de console** no Windows.

## Arquivos removidos do diretório (limpeza)

Foram removidos arquivos desnecessários para build e uso em produção:

- `test_webhook.py` — script de teste
- `COMO_CONFIGURAR_WEBHOOK.md` — documentação duplicada
- `GUIA_RAPIDO.md` — documentação duplicada

O `.gitignore` foi atualizado para ignorar: `agent.db`, `dist/`, `build/`, `*.spec`.

## Pré-requisitos

- **Python 3.8+** instalado
- Dependências do projeto e do build instaladas

No diretório `agent_service_printer_compuchat`:

```bash
pip install -r requirements.txt
pip install pyinstaller
```

## Comando para gerar o executável (sem console)

No **PowerShell** ou **CMD**, dentro de `agent_service_printer_compuchat`:

```bash
pyinstaller --noconfirm --onefile --noconsole --name "PrintAgent" --add-data "templates;templates" app.py
```

- **`--noconsole`** — aplicação roda sem janela de terminal (sem console).
- **`--onefile`** — um único arquivo `.exe`.
- **`--add-data "templates;templates"`** — inclui a pasta `templates` dentro do executável (no Windows use `;` entre origem e destino).
- **`app.py`** — ponto de entrada da aplicação.

### No Linux / macOS (referência)

Se for gerar em outro SO, use `:` no `--add-data`:

```bash
pyinstaller --noconfirm --onefile --noconsole --name "PrintAgent" --add-data "templates:templates" app.py
```

## Onde fica o executável

Após o build:

- **Executável:** `agent_service_printer_compuchat/dist/PrintAgent.exe`
- **Arquivos temporários do build:** `agent_service_printer_compuchat/build/` e `agent_service_printer_compuchat/PrintAgent.spec`

Você pode copiar apenas `PrintAgent.exe` para outra pasta ou máquina. Na primeira execução, o app cria no **mesmo diretório do .exe** (ou no diretório de trabalho atual):

- `agent.db` — banco SQLite (configuração e logs)
- Configurações salvas pela interface

## Como executar

1. **Duplo clique** em `PrintAgent.exe`, ou
2. Pelo terminal: `.\dist\PrintAgent.exe`

A interface fica em **http://localhost:5000/** (configuração e logs). O agente WebSocket sobe junto; não aparece janela de console.

## Resumo do comando (copiar e colar)

```bash
cd agent_service_printer_compuchat
pip install -r requirements.txt
pip install pyinstaller
pyinstaller --noconfirm --onefile --noconsole --name "PrintAgent" --add-data "templates;templates" app.py
```

Executável gerado: `dist\PrintAgent.exe`
