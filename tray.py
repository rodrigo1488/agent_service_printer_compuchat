"""
Bandeja do sistema (system tray) para o Print Agent.
Permite rodar o agente em segundo plano com opções: Abrir configuração, Ver logs, Sair.
"""
import os
import sys
import threading
import time
import webbrowser
from datetime import datetime

# Caminho do arquivo de log (terminal redirecionado)
def _log_file_path():
    if getattr(sys, "frozen", False):
        base = os.path.dirname(sys.executable)
    else:
        base = os.getcwd()
    return os.path.join(base, "agent_console.log")


class Tee:
    """Redireciona stdout/stderr para um arquivo e mantém o stream original (se existir)."""
    def __init__(self, stream, path):
        self._stream = stream
        self._path = path
        self._file = open(path, "a", encoding="utf-8", errors="replace")

    def write(self, data):
        try:
            if self._stream:
                self._stream.write(data)
            self._file.write(data)
            self._file.flush()
        except Exception:
            pass

    def flush(self):
        try:
            if self._stream:
                self._stream.flush()
            self._file.flush()
        except Exception:
            pass

    def close(self):
        try:
            self._file.close()
        except Exception:
            pass


def _create_icon_image():
    """Cria ícone simples 64x64 para a bandeja (impressora/documento)."""
    from PIL import Image, ImageDraw
    w, h = 64, 64
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    # Retângulo (corpo da impressora) + triângulo (saída)
    draw.rectangle([8, 16, 56, 48], fill=(0, 180, 255), outline=(0, 120, 200))
    draw.rectangle([12, 20, 52, 36], fill=(255, 255, 255))
    draw.polygon([(20, 48), (44, 48), (38, 56), (26, 56)], fill=(0, 180, 255))
    return img


def run_tray(run_flask_callable):
    """
    Coloca stdout/stderr em arquivo, inicia Flask e agente em thread, exibe bandeja com menu.
    run_flask_callable: função sem argumentos que inicia o servidor Flask (ex.: run_flask do app).
    """
    log_path = _log_file_path()
    # Redirecionar saída para o arquivo de log (e manter console se existir)
    _stdout = sys.stdout
    _stderr = sys.stderr
    sys.stdout = Tee(_stdout, log_path)
    sys.stderr = Tee(sys.stderr, log_path)

    print(f"\n{'='*60}\nPrint Agent - Sessão iniciada em {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n{'='*60}\n")

    # Importar agente após redirecionar para que prints vão para o log
    from agent import start_agent_thread, stop_agent

    # Iniciar Flask em thread (callable passado para evitar re-importar app)
    flask_thread = threading.Thread(target=run_flask_callable, daemon=True)
    flask_thread.start()
    time.sleep(1.2)

    start_agent_thread()

    try:
        import pystray
    except ImportError:
        print("[ERRO] pystray não instalado. Execute: pip install pystray Pillow")
        sys.stdout = _stdout
        sys.stderr = _stderr
        return

    def on_abrir_config(icon, item):
        webbrowser.open("http://localhost:5000/")

    def on_ver_logs(icon, item):
        path = _log_file_path()
        if os.path.isfile(path):
            if sys.platform == "win32":
                os.startfile(path)
            elif sys.platform == "darwin":
                os.system(f'open "{path}"')
            else:
                os.system(f'xdg-open "{path}"')
        else:
            webbrowser.open("http://localhost:5000/logs")

    def on_sair(icon, item):
        stop_agent()
        icon.stop()
        if hasattr(sys.stdout, "close"):
            sys.stdout.close()
        if hasattr(sys.stderr, "close"):
            sys.stderr.close()
        os._exit(0)

    icon_image = _create_icon_image()
    menu = pystray.Menu(
        pystray.MenuItem("Abrir configuração", on_abrir_config, default=True),
        pystray.MenuItem("Ver logs", on_ver_logs),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Sair", on_sair),
    )
    icon = pystray.Icon("print_agent", icon_image, "Print Agent", menu)
    print(f"[INFO] Print Agent na bandeja. Log: {log_path}")
    print("[INFO] Interface: http://localhost:5000/")
    icon.run()
