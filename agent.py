"""Cliente WebSocket para o Print Agent - conecta ao SaaS e processa jobs de impressão."""
import json
import threading
import time
import logging
from datetime import datetime

import db
from printer_service import PrinterService
from receipt_formatter import format_order_receipt
from error_recovery import (
    retry_with_backoff,
    RetryConfig,
    ConnectionHealthChecker,
    DataValidator,
    thread_monitor,
)

try:
    import websocket
except ImportError:
    websocket = None

# Configuração de logs
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("agent")

_agent_threads = []
_should_stop = False


def _log(level: str, msg: str):
    """Log formatado para stdout."""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"{ts} [{level}] {msg}")


def _handle_print_job(ws, job_id: int, conteudo: dict, printer_config: dict):
    """Processa um job de impressão usando a impressora indicada em printer_config."""
    device_id = printer_config.get("device_id", "unknown")
    printer_ip = printer_config.get("printer_ip", "192.168.1.100")
    printer_port = int(printer_config.get("printer_port") or 9100)
    
    connection_type = printer_config.get("connection_type") or "network"
    if connection_type == "local":
        printer_name_local = printer_config.get("printer_name_local", "")
        _log("INFO", f"Job {job_id}: Processando na impressora local device_id={device_id}, nome={printer_name_local}")
    else:
        _log("INFO", f"Job {job_id}: Processando na impressora device_id={device_id}, ip={printer_ip}:{printer_port}")
    
    # Verificar saúde da conexão antes de imprimir (apenas para rede)
    if connection_type == "network":
        if not ConnectionHealthChecker.check_printer_connection(printer_ip, printer_port):
            error_msg = f"Impressora {printer_ip}:{printer_port} não está acessível"
            _log("ERROR", f"Job {job_id}: {error_msg}")
            db.add_print_log(job_id, "error", error_msg)
            try:
                ws.send(json.dumps({"event": "ack", "job_id": job_id, "status": "error", "message": error_msg}))
            except Exception:
                pass
            return
    
    printer = PrinterService(
        printer_ip=printer_ip,
        printer_port=printer_port,
        printer_type=printer_config.get("printer_type") or "raw",
        paper_width=printer_config.get("paper_width") or "32",
        printer_encoding=printer_config.get("printer_encoding") or "cp850",
        connection_type=connection_type,
        printer_name_local=printer_config.get("printer_name_local") or None,
    )

    try:
        receipt = format_order_receipt(conteudo)
        
        # Retry automático para impressão com backoff exponencial
        @retry_with_backoff(RetryConfig(
            max_retries=3,
            initial_delay=1.0,
            max_delay=10.0,
            retryable_exceptions=(Exception,)
        ))
        def _print_with_retry():
            return printer.print_receipt(receipt)
        
        success = _print_with_retry()

        status = "done" if success else "error"
        message = "" if success else "Falha ao imprimir após múltiplas tentativas"

        db.add_print_log(job_id, status, message)

        ack = {"event": "ack", "job_id": job_id, "status": status}
        if message:
            ack["message"] = message
        
        try:
            ws.send(json.dumps(ack))
        except Exception as e:
            _log("ERROR", f"Erro ao enviar ACK para job {job_id}: {e}")

        if success:
            _log("INFO", f"Job {job_id} impresso com sucesso na impressora device_id={device_id}")
        else:
            _log("ERROR", f"Job {job_id} falhou na impressora device_id={device_id}: {message}")
    except Exception as e:
        _log("ERROR", f"Job {job_id} erro na impressora device_id={device_id}: {str(e)}")
        db.add_print_log(job_id, "error", str(e))
        try:
            ws.send(json.dumps({"event": "ack", "job_id": job_id, "status": "error", "message": str(e)}))
        except Exception:
            pass


def _make_on_message(printer_config: dict):
    """Retorna handler on_message que usa printer_config."""
    def _on_message(ws, message):
        try:
            data = json.loads(message)
            event = data.get("event")
            if event == "print_job":
                # Validar dados antes de processar
                is_valid, error_msg = DataValidator.validate_print_job(data)
                if not is_valid:
                    _log("ERROR", f"Job inválido recebido: {error_msg}")
                    try:
                        job_id = data.get("job_id", 0)
                        ws.send(json.dumps({
                            "event": "ack",
                            "job_id": job_id,
                            "status": "error",
                            "message": f"Dados inválidos: {error_msg}"
                        }))
                    except Exception:
                        pass
                    return
                
                job_id = data.get("job_id")
                conteudo = data.get("conteudo", {})
                if job_id is not None and conteudo:
                    _handle_print_job(ws, job_id, conteudo, printer_config)
                else:
                    _log("WARN", "print_job recebido sem job_id ou conteudo")
            elif event == "ready":
                _log("INFO", f"Conectado ao SaaS (device_id={printer_config.get('device_id', '')}) - pronto para receber jobs")
        except json.JSONDecodeError as e:
            _log("ERROR", f"Mensagem inválida (JSON): {e}")
        except Exception as e:
            _log("ERROR", f"Erro ao processar mensagem: {e}")
    return _on_message


def _on_error(ws, error):
    """Handler de erros WebSocket."""
    if error:
        _log("ERROR", f"WebSocket error: {error}")
        # Se for erro 401, dar dica sobre token
        if "401" in str(error) or "Unauthorized" in str(error):
            _log("ERROR", "ERRO DE AUTENTICAÇÃO (401): O token ou deviceId estão incorretos.")
            _log("ERROR", "SOLUÇÃO: 1) Acesse Configurações > Dispositivos de Impressão no sistema")
            _log("ERROR", "         2) Copie o TOKEN correto do dispositivo (não use o deviceId como token)")
            _log("ERROR", "         3) Cole o token no campo 'Token (Bearer)' na configuração do agente")
            _log("ERROR", "         4) Certifique-se de que o deviceId no agente corresponde ao deviceId no sistema")


def _on_close(ws, close_status_code, close_msg):
    """Handler de fechamento WebSocket."""
    _log("INFO", f"Conexão fechada (code={close_status_code}, msg={close_msg})")


def _on_open(ws):
    """Handler de abertura WebSocket."""
    _log("INFO", "Conexão WebSocket estabelecida")


def _run_websocket(printer_config: dict):
    """Loop principal do cliente WebSocket para uma impressora (reconexão exponencial)."""
    global _should_stop

    if not websocket:
        return

    ws_url = db.get_config("ws_url")
    token = printer_config.get("token", "").strip()
    device_id = printer_config.get("device_id", "").strip()

    if not ws_url or not token or not device_id:
        _log("WARN", f"Impressora sem ws_url/token/device_id (device_id={device_id or 'vazio'}). Configure em http://localhost:5000/")
        return
    
    # Validar URL WebSocket antes de conectar
    if not ConnectionHealthChecker.check_websocket_url(ws_url):
        _log("WARN", f"URL WebSocket {ws_url} não está acessível. Tentando conectar mesmo assim...")
    
    # Log das credenciais (sem expor o token completo por segurança)
    _log("INFO", f"Credenciais para device_id={device_id}: token_length={len(token)}, token_preview={token[:20] if len(token) > 20 else token}...")

    retry_delay = 1
    max_retry_delay = 60
    consecutive_failures = 0
    max_consecutive_failures = 10
    on_message = _make_on_message(printer_config)

    while not _should_stop:
        extra_headers = {
            "Authorization": f"Bearer {token}",
            "X-Device-Id": device_id,
        }

        _log("INFO", f"Conectando a {ws_url} (device_id={device_id})...")

        try:
            ws = websocket.WebSocketApp(
                ws_url,
                header=extra_headers,
                on_message=on_message,
                on_error=_on_error,
                on_close=_on_close,
                on_open=_on_open,
            )

            ws.run_forever(
                ping_interval=30,
                ping_timeout=10,
            )
            consecutive_failures = 0  # Reset contador em caso de sucesso
        except Exception as e:
            consecutive_failures += 1
            _log("ERROR", f"Erro de conexão (device_id={device_id}): {e} (falhas consecutivas: {consecutive_failures})")
            
            # Se muitas falhas consecutivas, aumentar delay
            if consecutive_failures >= max_consecutive_failures:
                _log("WARN", f"Muitas falhas consecutivas ({consecutive_failures}). Aumentando delay de reconexão...")
                retry_delay = min(retry_delay * 2, max_retry_delay)

        if _should_stop:
            break

        _log("INFO", f"Reconectando em {retry_delay}s (device_id={device_id})...")
        time.sleep(retry_delay)
        retry_delay = min(retry_delay * 2, max_retry_delay)


def start_agent_thread():
    """Inicia uma thread por impressora configurada (cada uma conecta ao SaaS com seu device_id/token)."""
    global _agent_threads, _should_stop

    _should_stop = False
    for t in _agent_threads:
        if t.is_alive():
            return
    _agent_threads.clear()

    printers = db.get_printers()
    if not printers:
        _log("WARN", "Nenhuma impressora configurada. Adicione em http://localhost:5000/")
        return

    ws_url = db.get_config("ws_url")
    if not ws_url or not ws_url.strip():
        _log("WARN", "Configure a URL WebSocket (Conexão SaaS) em http://localhost:5000/")
        return

    # Iniciar monitor de threads se ainda não estiver rodando
    if not thread_monitor.monitor_thread or not thread_monitor.monitor_thread.is_alive():
        thread_monitor.start()

    for p in printers:
        if not p.get("device_id") or not p.get("token"):
            continue
        
        device_id = p.get("device_id")
        
        def create_thread_for_printer(printer_config):
            """Cria uma nova thread para a impressora."""
            t = threading.Thread(target=_run_websocket, args=(printer_config,), daemon=True)
            t.start()
            return t
        
        t = create_thread_for_printer(p)
        _agent_threads.append(t)
        
        # Registrar thread no monitor para auto-restart
        def restart_callback():
            return create_thread_for_printer(p)
        
        thread_monitor.register_thread(
            f"websocket_{device_id}",
            t,
            restart_callback,
            max_restarts=5,
            restart_delay=5.0
        )
        
        _log("INFO", f"Thread iniciada para device_id={device_id} (monitorada)")


def stop_agent():
    """Sinaliza o agent para parar (na próxima desconexão)."""
    global _should_stop
    _should_stop = True
    thread_monitor.stop()