"""Print Agent - Interface web e servidor HTTP."""
import os
import sys
import threading
from flask import Flask, request, redirect, url_for, render_template, jsonify
from flask_cors import CORS

import db
from agent import start_agent_thread, stop_agent

# Tentar importar win32print para listar impressoras locais (Windows)
try:
    import win32print
    HAS_WIN32PRINT = True
except ImportError:
    HAS_WIN32PRINT = False

# Suporte a executável PyInstaller (sem console): templates extraídos em sys._MEIPASS
if getattr(sys, "frozen", False):
    _base = sys._MEIPASS
    _template_folder = os.path.join(_base, "templates")
else:
    _template_folder = "templates"

app = Flask(__name__, template_folder=_template_folder)
CORS(app)
app.config["SECRET_KEY"] = "print-agent-secret"

# Inicializar banco na importação
db.init_db()


def _config_context():
    """Dados para o template de configuração: ws_url, lista de impressoras e opções."""
    ws_url = db.get_config("ws_url")
    printers = db.get_printers()
    restart_on_save = (db.get_config("restart_service_on_save") or "true").lower() == "true"
    return {"ws_url": ws_url, "printers": printers, "restart_service_on_save": restart_on_save}


@app.route("/")
def index():
    """Página de configuração."""
    ctx = _config_context()
    message = request.args.get("message")
    message_type = request.args.get("message_type", "success")
    return render_template("config.html", **ctx, message=message, message_type=message_type)


@app.route("/config", methods=["GET", "POST"])
def config():
    """GET: exibe form. POST: salva configuração (ws_url + lista de impressoras)."""
    if request.method == "POST":
        try:
            ws_url = request.form.get("ws_url", "").strip()
            if ws_url:
                db.set_config("ws_url", ws_url)

            # Montar lista de impressoras: printer_0_device_id, printer_0_token, ...
            indices = []
            print(f"[DEBUG] Form keys recebidos: {list(request.form.keys())}")
            for key in request.form:
                if key.startswith("printer_") and key.endswith("_device_id"):
                    idx = key.replace("printer_", "").replace("_device_id", "")
                    try:
                        indices.append(int(idx))
                    except ValueError:
                        pass
            indices.sort()
            print(f"[DEBUG] Índices de impressoras encontrados: {indices}")
            printers = []
            for idx in indices:
                prefix = f"printer_{idx}_"
                device_id = request.form.get(prefix + "device_id", "").strip()
                token = request.form.get(prefix + "token", "").strip()
                connection_type = request.form.get(prefix + "connection_type", "network").strip() or "network"
                
                print(f"[DEBUG] Processando impressora {idx}: device_id={device_id}, connection_type={connection_type}")
                
                printer_data = {
                    "device_id": device_id,
                    "token": token,
                    "printer_type": request.form.get(prefix + "printer_type", "raw").strip() or "raw",
                    "paper_width": request.form.get(prefix + "paper_width", "32").strip() or "32",
                    "printer_encoding": request.form.get(prefix + "printer_encoding", "cp850").strip() or "cp850",
                    "name": request.form.get(prefix + "name", "").strip(),
                    "connection_type": connection_type,
                }
                
                if connection_type == "local":
                    printer_data["printer_name_local"] = request.form.get(prefix + "printer_name_local", "").strip()
                    # Para impressoras locais, não precisamos de IP/porta, mas mantemos valores padrão para compatibilidade
                    printer_data["printer_ip"] = ""
                    printer_data["printer_port"] = 9100
                else:
                    printer_data["printer_ip"] = request.form.get(prefix + "printer_ip", "192.168.1.100").strip() or "192.168.1.100"
                    printer_data["printer_port"] = int(request.form.get(prefix + "printer_port", "9100").strip() or "9100")
                    printer_data["printer_name_local"] = ""
                
                printers.append(printer_data)
                print(f"[DEBUG] Impressora {idx} adicionada: {printer_data}")
            print(f"[DEBUG] Salvando {len(printers)} impressora(s): {printers}")
            db.set_printers(printers)
            # Opção "Reiniciar serviço ao salvar"
            restart_on_save = request.form.get("restart_service_on_save", "true").lower() in ("true", "1", "on", "yes")
            db.set_config("restart_service_on_save", "true" if restart_on_save else "false")
            print(f"[DEBUG] Impressoras salvas com sucesso")
            if restart_on_save:
                stop_agent()
                start_agent_thread()
                print("[INFO] Serviço reiniciado após salvar configuração.")
            return redirect(url_for("index", message="Configuração salva com sucesso!" + (" Serviço reiniciado." if restart_on_save else ""), message_type="success"))
        except Exception as e:
            import traceback
            error_msg = f"Erro: {str(e)}"
            print(f"[ERROR] Erro ao salvar configuração: {error_msg}")
            print(f"[ERROR] Traceback: {traceback.format_exc()}")
            ctx = _config_context()
            return render_template(
                "config.html",
                **ctx,
                message=error_msg,
                message_type="error",
            )
    return render_template("config.html", **_config_context())


@app.route("/health", methods=["GET"])
def health():
    """Health check para monitoramento."""
    return jsonify({"status": "ok", "message": "Print Agent is running"}), 200


@app.route("/logs")
def logs():
    """Histórico de impressões."""
    logs_list = db.get_print_logs(limit=50)
    return render_template("logs.html", logs=logs_list)


@app.route("/api/test-printer", methods=["POST"])
def test_printer():
    """Testa uma impressora (rede ou local) enviando uma página de teste."""
    try:
        data = request.get_json() or {}
        connection_type = data.get("connection_type", "network")
        
        if connection_type == "local":
            printer_name_local = data.get("printer_name_local", "")
            if not printer_name_local:
                return jsonify({"error": "Nome da impressora local não especificado"}), 400
            
            if not HAS_WIN32PRINT:
                return jsonify({"error": "pywin32 não está instalado. Instale com: pip install pywin32"}), 400
            
            try:
                from printer_service import PrinterService
                printer = PrinterService(
                    connection_type="local",
                    printer_name_local=printer_name_local,
                    paper_width=data.get("paper_width", "32"),
                    printer_encoding=data.get("printer_encoding", "cp850"),
                )
                
                # Criar conteúdo de teste
                test_receipt = {
                    "form_name": "TESTE DE IMPRESSÃO",
                    "protocol": "TEST-0001",
                    "date": "Teste",
                    "customer": {"name": "Teste", "phone": "", "email": ""},
                    "items_by_group": {"Teste": [{"name": "Página de teste", "quantity": 1, "value": 0.0, "total": 0.0}]},
                    "custom_info": {},
                    "total": 0.0,
                }
                
                success = printer.print_receipt(test_receipt)
                if success:
                    return jsonify({"success": True, "message": f"Teste enviado com sucesso para {printer_name_local}"})
                else:
                    return jsonify({"error": "Falha ao imprimir página de teste"}), 500
                    
            except Exception as e:
                return jsonify({"error": f"Erro ao testar impressora: {str(e)}"}), 500
        else:
            # Teste para impressora de rede
            printer_ip = data.get("printer_ip", "")
            printer_port = int(data.get("printer_port", 9100))
            printer_type = data.get("printer_type", "raw")
            
            if not printer_ip:
                return jsonify({"error": "IP da impressora não especificado"}), 400
            
            try:
                from printer_service import PrinterService
                printer = PrinterService(
                    printer_ip=printer_ip,
                    printer_port=printer_port,
                    printer_type=printer_type,
                    paper_width=data.get("paper_width", "32"),
                    printer_encoding=data.get("printer_encoding", "cp850"),
                    connection_type="network",
                )
                
                # Criar conteúdo de teste
                test_receipt = {
                    "form_name": "TESTE DE IMPRESSÃO",
                    "protocol": "TEST-0001",
                    "date": "Teste",
                    "customer": {"name": "Teste", "phone": "", "email": ""},
                    "items_by_group": {"Teste": [{"name": "Página de teste", "quantity": 1, "value": 0.0, "total": 0.0}]},
                    "custom_info": {},
                    "total": 0.0,
                }
                
                success = printer.print_receipt(test_receipt)
                if success:
                    return jsonify({"success": True, "message": f"Teste enviado com sucesso para {printer_ip}:{printer_port}"})
                else:
                    return jsonify({"error": "Falha ao imprimir página de teste"}), 500
                    
            except Exception as e:
                return jsonify({"error": f"Erro ao testar impressora: {str(e)}"}), 500
                
    except Exception as e:
        return jsonify({"error": f"Erro: {str(e)}"}), 500


@app.route("/api/local-printers", methods=["GET"])
def get_local_printers():
    """Lista impressoras instaladas no Windows (apenas Windows)."""
    if not HAS_WIN32PRINT:
        import platform
        system = platform.system()
        if system == "Windows":
            error_msg = "pywin32 não está instalado. Instale com: pip install pywin32"
        else:
            error_msg = f"Impressoras locais só estão disponíveis no Windows. Sistema atual: {system}"
        print(f"[ERROR] {error_msg}")
        return jsonify({"error": error_msg, "printers": []}), 200  # Retornar 200 com lista vazia para não quebrar o frontend
    
    try:
        printers = []
        # EnumPrinters retorna uma tupla: (flags, name, default, description)
        printer_list = win32print.EnumPrinters(win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS)
        for printer_info in printer_list:
            printer_name = printer_info[2] if len(printer_info) > 2 else str(printer_info)
            printers.append({
                "name": printer_name,
                "description": printer_name,
            })
        print(f"[INFO] Listadas {len(printers)} impressoras locais")
        return jsonify({"printers": printers})
    except Exception as e:
        error_msg = f"Erro ao listar impressoras: {str(e)}"
        print(f"[ERROR] {error_msg}")
        return jsonify({"error": error_msg, "printers": []}), 200  # Retornar 200 com lista vazia para não quebrar o frontend


def run_flask():
    """Executa o servidor Flask."""
    app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False, threaded=True)


if __name__ == "__main__":
    # Modo bandeja: com --tray ou quando for executável (PyInstaller sem console)
    use_tray = "--tray" in sys.argv or getattr(sys, "frozen", False)
    if use_tray:
        try:
            from tray import run_tray
            run_tray(run_flask)
        except ImportError as e:
            print("Erro ao iniciar bandeja (instale: pip install pystray Pillow):", e)
            start_agent_thread()
            run_flask()
    else:
        print("=" * 50)
        print("Print Agent - WebSocket")
        print("=" * 50)
        print("Interface: http://localhost:5000/")
        print("Health:    http://localhost:5000/health")
        print("=" * 50)
        start_agent_thread()
        run_flask()
