"""Cliente WebSocket para o Print Agent - conecta ao SaaS e processa jobs de impressão."""
import json
import logging
import random
import ssl
import threading
import time
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
from uniplus_handler import (
    handle_uniplus_job,
    is_uniplus_enabled,
    format_uniplus_log_message,
    UniplusPermanentError,
)
from uniplus_handler import OperationalError as UniplusOperationalError
from uniplus_handler import InterfaceError as UniplusInterfaceError
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
_active_websockets = {}
_active_websockets_lock = threading.Lock()

# Janela de tolerÃ¢ncia para quedas momentÃ¢neas da impressora/rede
PRINTER_RECOVERY_WAIT_SECONDS = 90
PRINTER_RECOVERY_CHECK_INTERVAL = 5

# WebSocket: não verificar certificado SSL (evita CERTIFICATE_VERIFY_FAILED com servidor com cert autoassinado).
SSLOPT_WS = {"cert_reqs": ssl.CERT_NONE}

# Reconexão WS: backoff por device_id (cada impressora/agente tem o próprio delay).
WS_RETRY_MIN_SECONDS = 1.0
WS_RETRY_MAX_SECONDS = 300.0  # 5 min — evita martelar servidor offline
WS_RETRY_MULTIPLIER = 2.0
WS_AUTH_ERROR_DELAY_SECONDS = 60.0  # 401: não insistir a cada segundo


def _log(level: str, msg: str):
    """Log formatado para stdout."""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"{ts} [{level}] {msg}")


def _interruptible_sleep(seconds: float) -> None:
    """Sleep que respeita _should_stop (para parar o agente sem esperar o backoff inteiro)."""
    end = time.monotonic() + max(0.0, seconds)
    while not _should_stop:
        remaining = end - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(0.5, remaining))


def _reconnect_delay_with_jitter(base_delay: float, device_id: str) -> float:
    """
    Delay de reconexão com jitter ±20% + offset estável por device_id.
    Assim vários agentes/impressoras não reconectam em lockstep nem se bloqueiam mutuamente.
    """
    base = max(WS_RETRY_MIN_SECONDS, float(base_delay))
    jitter = base * 0.2 * (random.random() * 2 - 1)
    # Offset 0–2s derivado do device_id (estável entre tentativas do mesmo device)
    device_offset = (abs(hash(device_id or "")) % 2000) / 1000.0
    return max(WS_RETRY_MIN_SECONDS, base + jitter + device_offset)


def _register_websocket(device_id: str, ws):
    """Registra conexÃ£o WebSocket ativa por device_id."""
    with _active_websockets_lock:
        _active_websockets[device_id] = ws


def _unregister_websocket(device_id: str, ws):
    """Remove conexÃ£o WebSocket ativa somente se for a mesma instÃ¢ncia."""
    with _active_websockets_lock:
        current = _active_websockets.get(device_id)
        if current is ws:
            _active_websockets.pop(device_id, None)


def _close_all_websockets():
    """Fecha todas as conexÃµes WebSocket ativas para forÃ§ar reconexÃ£o/parada imediata."""
    with _active_websockets_lock:
        to_close = list(_active_websockets.values())
        _active_websockets.clear()

    for ws in to_close:
        try:
            ws.close()
        except Exception:
            pass


def _get_latest_printer_config(device_id: str, fallback_config: dict) -> dict:
    """Busca configuraÃ§Ã£o mais recente da impressora no banco, com fallback para config inicial."""
    if not device_id:
        return fallback_config

    for printer in db.get_printers():
        if (printer.get("device_id") or "").strip() == device_id:
            return printer
    return fallback_config


def _send_uniplus_ack(ws, payload: dict):
    try:
        ws.send(json.dumps(payload))
    except Exception as e:
        _log("ERROR", f"Erro ao enviar ACK UniPlus job={payload.get('job_id')}: {e}")


def _handle_uniplus_job(ws, job_id: int, conteudo: dict):
    """Processa job UniPlus (INSERT CONTAMESA) e envia ACK."""
    _log("INFO", f"Job {job_id}: processando uniplus_job...")
    try:
        if not is_uniplus_enabled(db):
            raise UniplusPermanentError(
                "ERR_UNIPLUS_CONFIG: UniPlus desabilitado ou sem connection string"
            )

        @retry_with_backoff(
            RetryConfig(
                max_retries=2,
                initial_delay=0.8,
                max_delay=6.0,
                retryable_exceptions=(UniplusOperationalError, UniplusInterfaceError),
            )
        )
        def _run():
            return handle_uniplus_job(db, conteudo or {})

        result = _run()
        conta_id = result.get("conta_id")
        message = result.get("message") or result.get("action") or "ok"
        log_msg = format_uniplus_log_message(result)
        db.add_print_log(job_id, "done", log_msg, kind="uniplus", detail=result)
        ack = {
            "event": "ack",
            "job_id": job_id,
            "status": "done",
            "message": message,
            "uniplusContaId": conta_id,
            "uniplusAction": result.get("action"),
            "uniplusNumeromesa": result.get("numeromesa"),
            "protocol": result.get("protocol"),
        }
        _send_uniplus_ack(ws, ack)
        _log(
            "INFO",
            f"Job {job_id}: UniPlus {result.get('action')} contaId={conta_id} "
            f"mesa={result.get('numeromesa')} protocol={result.get('protocol')} "
            f"cliente={result.get('cliente')} itens={result.get('itens_count')} "
            f"total={result.get('valortotal')}",
        )
    except Exception as e:
        permanent = isinstance(e, UniplusPermanentError)
        msg = str(e)
        _log("ERROR", f"Job {job_id}: UniPlus erro{' permanente' if permanent else ''}: {msg}")
        err_detail = {
            "action": "error",
            "error": msg,
            "permanent": permanent,
            "protocol": (conteudo or {}).get("protocol"),
            "formResponseId": (conteudo or {}).get("formResponseId"),
            "cliente": ((conteudo or {}).get("contamesa") or {}).get("nomecliente"),
        }
        db.add_print_log(job_id, "error", msg, kind="uniplus", detail=err_detail)
        _send_uniplus_ack(
            ws,
            {
                "event": "ack",
                "job_id": job_id,
                "status": "error",
                "message": msg,
                "permanent": permanent,
            },
        )


def _handle_print_job(ws, job_id: int, conteudo: dict, printer_config: dict):
    """Processa um job de impressão usando a impressora indicada em printer_config."""
    initial_device_id = (printer_config.get("device_id") or "").strip()
    latest_config = _get_latest_printer_config(initial_device_id, printer_config)

    device_id = latest_config.get("device_id", "unknown")
    printer_ip = latest_config.get("printer_ip", "192.168.1.100")
    printer_port = int(latest_config.get("printer_port") or 9100)

    connection_type = latest_config.get("connection_type") or "network"
    if connection_type == "local":
        printer_name_local = latest_config.get("printer_name_local", "")
        _log("INFO", f"Job {job_id}: Processando na impressora local device_id={device_id}, nome={printer_name_local}")
    else:
        _log("INFO", f"Job {job_id}: Processando na impressora device_id={device_id}, ip={printer_ip}:{printer_port}")

    # Para impressora de rede, aguardar reconexÃ£o por um tempo antes de falhar o job.
    if connection_type == "network":
        start_wait = time.time()
        while not _should_stop and not ConnectionHealthChecker.check_printer_connection(printer_ip, printer_port):
            elapsed = int(time.time() - start_wait)
            if elapsed >= PRINTER_RECOVERY_WAIT_SECONDS:
                error_msg = (
                    f"Impressora {printer_ip}:{printer_port} nÃ£o voltou em "
                    f"{PRINTER_RECOVERY_WAIT_SECONDS}s"
                )
                _log("ERROR", f"Job {job_id}: {error_msg}")
                db.add_print_log(job_id, "error", error_msg)
                try:
                    ws.send(json.dumps({"event": "ack", "job_id": job_id, "status": "error", "message": error_msg}))
                except Exception:
                    pass
                return

            _log(
                "WARN",
                f"Job {job_id}: impressora indisponÃ­vel ({printer_ip}:{printer_port}), aguardando retorno..."
            )
            time.sleep(PRINTER_RECOVERY_CHECK_INTERVAL)
            latest_config = _get_latest_printer_config(device_id, latest_config)
            printer_ip = latest_config.get("printer_ip", printer_ip)
            printer_port = int(latest_config.get("printer_port") or printer_port)

    printer = PrinterService(
        printer_ip=printer_ip,
        printer_port=printer_port,
        printer_type=latest_config.get("printer_type") or "raw",
        paper_width=latest_config.get("paper_width") or "32",
        printer_encoding=latest_config.get("printer_encoding") or "cp850",
        connection_type=connection_type,
        printer_name_local=latest_config.get("printer_name_local") or None,
    )

    try:
        receipt = format_order_receipt(conteudo)

        # Retry automÃ¡tico para impressÃ£o com backoff exponencial
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
        protocol = (conteudo or {}).get("protocol") or ""
        message = "" if success else "Falha ao imprimir após múltiplas tentativas"
        if success and protocol:
            message = f"Impresso protocol={protocol}"
        elif not success and protocol:
            message = f"{message} protocol={protocol}"

        # Detalhe específico de PRINT (não misturar com layout UniPlus na UI de logs)
        customer = (receipt or {}).get("customer") or {}
        items_flat = []
        for grupo, items in ((receipt or {}).get("items_by_group") or {}).items():
            for it in items or []:
                items_flat.append(
                    {
                        "grupo": grupo,
                        "nome": it.get("name"),
                        "qtd": it.get("quantity"),
                        "total": it.get("total"),
                    }
                )
        print_detail = {
            "kind": "print",
            "protocol": protocol or None,
            "formResponseId": (conteudo or {}).get("formResponseId")
            or (conteudo or {}).get("form_response_id"),
            "formName": (receipt or {}).get("form_name"),
            "cliente": customer.get("name") or None,
            "telefone": customer.get("phone") or None,
            "email": customer.get("email") or None,
            "tableNumber": (receipt or {}).get("table_number") or None,
            "garcomName": (receipt or {}).get("garcom_name") or None,
            "valortotal": (receipt or {}).get("total"),
            "valorentrega": (receipt or {}).get("delivery_fee"),
            "subtotal": (receipt or {}).get("subtotal"),
            "itens_count": len(items_flat),
            "itens": items_flat[:40],
            "device_id": device_id,
            "connection_type": connection_type,
            "printer_ip": printer_ip if connection_type == "network" else None,
            "printer_name_local": latest_config.get("printer_name_local")
            if connection_type == "local"
            else None,
        }

        db.add_print_log(
            job_id,
            status,
            message,
            kind="print",
            detail=print_detail,
        )
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
                    # Processar em thread separada para não bloquear loop WebSocket.
                    threading.Thread(
                        target=_handle_print_job,
                        args=(ws, job_id, conteudo, printer_config),
                        daemon=True,
                        name=f"print_job_{job_id}",
                    ).start()
                else:
                    _log("WARN", "print_job recebido sem job_id ou conteudo")
            elif event == "uniplus_job":
                is_valid, error_msg = DataValidator.validate_uniplus_job(data)
                job_id = data.get("job_id")
                if not is_valid:
                    _log("ERROR", f"uniplus_job inválido: {error_msg}")
                    if job_id is not None:
                        _send_uniplus_ack(
                            ws,
                            {
                                "event": "ack",
                                "job_id": job_id,
                                "status": "error",
                                "message": error_msg,
                                "permanent": True,
                            },
                        )
                    return
                conteudo = data.get("conteudo", {})
                threading.Thread(
                    target=_handle_uniplus_job,
                    args=(ws, job_id, conteudo),
                    daemon=True,
                    name=f"uniplus_job_{job_id}",
                ).start()
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


def _run_websocket(printer_config: dict):
    """Loop WebSocket por impressora com backoff exponencial + jitter (rate limit local por device_id)."""
    global _should_stop

    if not websocket:
        return

    base_device_id = (printer_config.get("device_id") or "").strip()
    if not base_device_id:
        _log("WARN", "Impressora sem device_id. Configure em http://localhost:5000/")
        return

    # Estado de backoff É POR THREAD/device — outros agents/devices não são afetados.
    retry_delay = WS_RETRY_MIN_SECONDS
    consecutive_failures = 0
    logged_credentials_once = False

    while not _should_stop:
        latest_config = _get_latest_printer_config(base_device_id, printer_config)
        ws_url = db.get_config("ws_url")
        token = (latest_config.get("token") or "").strip()
        device_id = (latest_config.get("device_id") or "").strip() or base_device_id

        if not ws_url or not token or not device_id:
            _log(
                "WARN",
                f"Impressora sem ws_url/token/device_id (device_id={device_id or 'vazio'}). "
                f"Aguardando configuração... (próxima checagem em 15s)",
            )
            _interruptible_sleep(15)
            continue

        # Preflight TCP: se a porta está recusada, não martelar o handshake WS.
        if not ConnectionHealthChecker.check_websocket_url(ws_url, timeout=2.0):
            wait_s = _reconnect_delay_with_jitter(retry_delay, device_id)
            consecutive_failures += 1
            _log(
                "WARN",
                f"URL WebSocket {ws_url} inacessível (device_id={device_id}, "
                f"falha #{consecutive_failures}). Nova tentativa em {wait_s:.0f}s.",
            )
            _interruptible_sleep(wait_s)
            retry_delay = min(retry_delay * WS_RETRY_MULTIPLIER, WS_RETRY_MAX_SECONDS)
            continue

        if not logged_credentials_once:
            _log(
                "INFO",
                f"Credenciais para device_id={device_id}: token_length={len(token)}, "
                f"token_preview={token[:20] if len(token) > 20 else token}...",
            )
            logged_credentials_once = True

        on_message = _make_on_message(latest_config)
        extra_headers = {
            "Authorization": f"Bearer {token}",
            "X-Device-Id": device_id,
        }

        connected = threading.Event()
        auth_failed = {"value": False}

        def on_open(ws):
            connected.set()
            consecutive_local = consecutive_failures  # só para log
            _log(
                "INFO",
                f"Conexão WebSocket estabelecida (device_id={device_id})"
                + (f" após {consecutive_local} falha(s)" if consecutive_local else ""),
            )

        def on_error(ws, error):
            _on_error(ws, error)
            if error and ("401" in str(error) or "Unauthorized" in str(error)):
                auth_failed["value"] = True

        _log("INFO", f"Conectando a {ws_url} (device_id={device_id})...")

        session_opened = False
        had_exception = False
        try:
            ws = websocket.WebSocketApp(
                ws_url,
                header=extra_headers,
                on_message=on_message,
                on_error=on_error,
                on_close=_on_close,
                on_open=on_open,
            )
            _register_websocket(device_id, ws)

            try:
                ws.run_forever(
                    ping_interval=30,
                    ping_timeout=10,
                    sslopt=SSLOPT_WS,
                )
            finally:
                _unregister_websocket(device_id, ws)

            session_opened = connected.is_set()
        except Exception as e:
            had_exception = True
            consecutive_failures += 1
            _log(
                "ERROR",
                f"Erro de conexão (device_id={device_id}): {e} "
                f"(falhas consecutivas: {consecutive_failures})",
            )
            session_opened = False

        if _should_stop:
            break

        if session_opened:
            # Sessão chegou a abrir: reset do backoff deste device.
            consecutive_failures = 0
            retry_delay = WS_RETRY_MIN_SECONDS
            wait_s = _reconnect_delay_with_jitter(retry_delay, device_id)
            _log("INFO", f"Reconectando em {wait_s:.0f}s (device_id={device_id})...")
            _interruptible_sleep(wait_s)
            continue

        # Não abriu (connection refused, timeout, etc.) — backoff só deste device_id.
        if not had_exception:
            consecutive_failures += 1
        if auth_failed["value"]:
            wait_s = _reconnect_delay_with_jitter(WS_AUTH_ERROR_DELAY_SECONDS, device_id)
            _log(
                "WARN",
                f"Auth falhou (401). Aguardando {wait_s:.0f}s antes de nova tentativa "
                f"(device_id={device_id}) — outros devices não são afetados.",
            )
            retry_delay = min(
                max(retry_delay, WS_AUTH_ERROR_DELAY_SECONDS),
                WS_RETRY_MAX_SECONDS,
            )
        else:
            wait_s = _reconnect_delay_with_jitter(retry_delay, device_id)
            _log(
                "WARN",
                f"Falha ao conectar WS (device_id={device_id}, falha #{consecutive_failures}). "
                f"Nova tentativa em {wait_s:.0f}s (backoff local; outros agents seguem independentes).",
            )
            retry_delay = min(retry_delay * WS_RETRY_MULTIPLIER, WS_RETRY_MAX_SECONDS)

        _interruptible_sleep(wait_s)


def start_agent_thread():
    """Inicia uma thread por impressora configurada (cada uma conecta ao SaaS com seu device_id/token)."""
    global _agent_threads, _should_stop

    _should_stop = False
    alive_threads = [t for t in _agent_threads if t.is_alive()]
    if alive_threads:
        _agent_threads = alive_threads
        _log("INFO", "Agent jÃ¡ estÃ¡ em execuÃ§Ã£o.")
        return
    _agent_threads = []

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
        def restart_callback(printer_snapshot=p):
            return create_thread_for_printer(printer_snapshot)
        
        thread_monitor.register_thread(
            f"websocket_{device_id}",
            t,
            restart_callback,
            max_restarts=5,
            restart_delay=5.0
        )
        
        _log("INFO", f"Thread iniciada para device_id={device_id} (monitorada)")


def stop_agent():
    """Sinaliza o agent para parar (na prÃ³xima desconexÃ£o)."""
    global _should_stop, _agent_threads
    _should_stop = True
    _close_all_websockets()

    # Aguardar encerramento das threads de websocket para permitir restart limpo.
    for t in _agent_threads:
        try:
            t.join(timeout=3.0)
        except Exception:
            pass
    _agent_threads = [t for t in _agent_threads if t.is_alive()]

    thread_monitor.stop()
