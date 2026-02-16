# Gerar executável do Print Agent (bandeja do sistema)

Instruções para gerar um `.exe` do Print Agent que roda na **bandeja do sistema** (system tray) no Windows, com opções **Ver logs** e **Sair**.

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

As dependências `pystray` e `Pillow` (já em `requirements.txt`) são usadas para o ícone na bandeja.

## Comando para gerar o executável (sem console)

No **PowerShell** ou **CMD**, dentro de `agent_service_printer_compuchat`:

```bash
pyinstaller --noconfirm --onefile --noconsole --name "PrintAgent" --add-data "templates;templates" --hidden-import win32print --hidden-import win32api --hidden-import websocket --hidden-import pystray --hidden-import PIL --hidden-import PIL.Image --hidden-import PIL.ImageDraw app.py
```

**Parâmetros:**
- **`--noconsole`** — aplicação roda sem janela de terminal (sem console).
- **`--onefile`** — um único arquivo `.exe`.
- **`--add-data "templates;templates"`** — inclui a pasta `templates` dentro do executável (no Windows use `;` entre origem e destino).
- **`--hidden-import win32print`** — inclui módulo win32print (impressoras locais Windows).
- **`--hidden-import win32api`** — inclui módulo win32api (impressoras locais Windows).
- **`--hidden-import websocket`** — inclui módulo websocket-client.
- **`app.py`** — ponto de entrada da aplicação.

### No Linux / macOS (referência)

Se for gerar em outro SO, use `:` no `--add-data` e remova os imports do win32:

```bash
pyinstaller --noconfirm --onefile --noconsole --name "PrintAgent" --add-data "templates:templates" --hidden-import websocket app.py
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

O agente sobe na **bandeja do sistema** (ícone ao lado do relógio). Clique com o botão direito no ícone para:

- **Abrir configuração** — abre http://localhost:5000/ no navegador
- **Ver logs** — abre o arquivo `agent_console.log` (mesmo diretório do .exe) no Bloco de Notas para acompanhar o que rodaria no terminal
- **Sair** — encerra o agente

O arquivo `agent_console.log` é criado/atualizado automaticamente e contém toda a saída do agente (conexão WebSocket, jobs de impressão, erros). Use **Ver logs** para depurar se as impressões pararem de chegar após o build.

## Resumo do comando (copiar e colar)

```bash
cd agent_service_printer_compuchat
pip install -r requirements.txt
pip install pyinstaller
pyinstaller --noconfirm --onefile --noconsole --name "PrintAgent" --add-data "templates;templates" --hidden-import win32print --hidden-import win32api --hidden-import websocket --hidden-import pystray --hidden-import PIL --hidden-import PIL.Image --hidden-import PIL.ImageDraw app.py
```

Executável gerado: `dist\PrintAgent.exe`

**Testar em modo console (logs no terminal):** `python app.py`  
**Testar em modo bandeja (como o .exe):** `python app.py --tray`

**Nota:** O PyInstaller detecta automaticamente os módulos Python locais (`agent.py`, `db.py`, `printer_service.py`, `receipt_formatter.py`, `tray.py`), mas os módulos opcionais como `win32print`, `websocket`, `pystray` e `PIL` precisam ser explicitamente incluídos com `--hidden-import`.
