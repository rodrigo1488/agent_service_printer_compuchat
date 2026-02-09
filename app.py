"""Print Agent - Interface web e servidor HTTP."""
import threading
from flask import Flask, request, redirect, url_for, render_template, jsonify
from flask_cors import CORS

import db
from agent import start_agent_thread, stop_agent

app = Flask(__name__)
CORS(app)
app.config["SECRET_KEY"] = "print-agent-secret"

# Inicializar banco na importação
db.init_db()


def _config_context():
    """Dados para o template de configuração: ws_url e lista de impressoras."""
    ws_url = db.get_config("ws_url")
    printers = db.get_printers()
    return {"ws_url": ws_url, "printers": printers}


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
            for key in request.form:
                if key.startswith("printer_") and key.endswith("_device_id"):
                    idx = key.replace("printer_", "").replace("_device_id", "")
                    try:
                        indices.append(int(idx))
                    except ValueError:
                        pass
            indices.sort()
            printers = []
            for idx in indices:
                prefix = f"printer_{idx}_"
                device_id = request.form.get(prefix + "device_id", "").strip()
                token = request.form.get(prefix + "token", "").strip()
                printers.append({
                    "device_id": device_id,
                    "token": token,
                    "printer_ip": request.form.get(prefix + "printer_ip", "192.168.1.100").strip() or "192.168.1.100",
                    "printer_port": request.form.get(prefix + "printer_port", "9100").strip() or "9100",
                    "printer_type": request.form.get(prefix + "printer_type", "raw").strip() or "raw",
                    "paper_width": request.form.get(prefix + "paper_width", "32").strip() or "32",
                    "printer_encoding": request.form.get(prefix + "printer_encoding", "cp850").strip() or "cp850",
                    "name": request.form.get(prefix + "name", "").strip(),
                })
            db.set_printers(printers)
            stop_agent()
            start_agent_thread()
            return redirect(url_for("index", message="Configuração salva com sucesso!", message_type="success"))
        except Exception as e:
            ctx = _config_context()
            return render_template(
                "config.html",
                **ctx,
                message=f"Erro: {str(e)}",
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


def run_flask():
    """Executa o servidor Flask."""
    app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False, threaded=True)


if __name__ == "__main__":
    print("=" * 50)
    print("Print Agent - WebSocket")
    print("=" * 50)
    print("Interface: http://localhost:5000/")
    print("Health:    http://localhost:5000/health")
    print("=" * 50)

    start_agent_thread()

    run_flask()
