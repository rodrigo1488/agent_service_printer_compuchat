"""Sistema de auto-correção e recuperação de erros para o Print Agent."""
import time
import threading
import logging
import socket
from typing import Callable, Optional, Dict, Any
from functools import wraps
from datetime import datetime, timedelta

logger = logging.getLogger("error_recovery")


class RetryConfig:
    """Configuração de retry com backoff exponencial."""
    def __init__(
        self,
        max_retries: int = 3,
        initial_delay: float = 1.0,
        max_delay: float = 60.0,
        exponential_base: float = 2.0,
        retryable_exceptions: tuple = (Exception,)
    ):
        self.max_retries = max_retries
        self.initial_delay = initial_delay
        self.max_delay = max_delay
        self.exponential_base = exponential_base
        self.retryable_exceptions = retryable_exceptions


def retry_with_backoff(config: RetryConfig = None):
    """Decorator para retry automático com backoff exponencial."""
    if config is None:
        config = RetryConfig()
    
    def decorator(func: Callable):
        @wraps(func)
        def wrapper(*args, **kwargs):
            delay = config.initial_delay
            last_exception = None
            
            for attempt in range(config.max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except config.retryable_exceptions as e:
                    last_exception = e
                    if attempt < config.max_retries:
                        logger.warning(
                            f"Tentativa {attempt + 1}/{config.max_retries + 1} falhou para {func.__name__}: {str(e)}. "
                            f"Tentando novamente em {delay:.2f}s..."
                        )
                        time.sleep(delay)
                        delay = min(delay * config.exponential_base, config.max_delay)
                    else:
                        logger.error(
                            f"Todas as tentativas falharam para {func.__name__}: {str(e)}"
                        )
            
            raise last_exception
        
        return wrapper
    return decorator


class ConnectionHealthChecker:
    """Verifica saúde de conexões de rede."""
    
    @staticmethod
    def check_printer_connection(printer_ip: str, printer_port: int, timeout: float = 3.0) -> bool:
        """Verifica se a impressora está acessível."""
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(timeout)
            result = sock.connect_ex((printer_ip, printer_port))
            sock.close()
            return result == 0
        except Exception as e:
            logger.debug(f"Erro ao verificar conexão com {printer_ip}:{printer_port}: {e}")
            return False
    
    @staticmethod
    def check_websocket_url(url: str, timeout: float = 5.0) -> bool:
        """Verifica se a URL do WebSocket está acessível (básico)."""
        try:
            from urllib.parse import urlparse
            parsed = urlparse(url)
            host = parsed.hostname
            port = parsed.port or (443 if parsed.scheme == 'wss' else 80)
            
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(timeout)
            result = sock.connect_ex((host, port))
            sock.close()
            return result == 0
        except Exception as e:
            logger.debug(f"Erro ao verificar URL WebSocket {url}: {e}")
            return False


class ThreadMonitor:
    """Monitora threads e reinicia se necessário."""
    
    def __init__(self, check_interval: float = 30.0):
        self.check_interval = check_interval
        self.monitored_threads: Dict[str, Dict[str, Any]] = {}
        self.monitor_thread: Optional[threading.Thread] = None
        self._should_stop = False
        self._lock = threading.Lock()
    
    def register_thread(
        self,
        thread_id: str,
        thread: threading.Thread,
        restart_callback: Callable,
        max_restarts: int = 5,
        restart_delay: float = 5.0
    ):
        """Registra uma thread para monitoramento."""
        with self._lock:
            self.monitored_threads[thread_id] = {
                "thread": thread,
                "restart_callback": restart_callback,
                "max_restarts": max_restarts,
                "restart_delay": restart_delay,
                "restart_count": 0,
                "last_restart": None
            }
    
    def unregister_thread(self, thread_id: str):
        """Remove uma thread do monitoramento."""
        with self._lock:
            self.monitored_threads.pop(thread_id, None)
    
    def _monitor_loop(self):
        """Loop de monitoramento de threads."""
        while not self._should_stop:
            time.sleep(self.check_interval)
            
            with self._lock:
                threads_to_restart = []
                
                for thread_id, info in self.monitored_threads.items():
                    thread = info["thread"]
                    if not thread.is_alive():
                        restart_count = info["restart_count"]
                        max_restarts = info["max_restarts"]
                        
                        if restart_count < max_restarts:
                            threads_to_restart.append((thread_id, info))
                        else:
                            logger.error(
                                f"Thread {thread_id} morreu e excedeu máximo de reinícios ({max_restarts}). "
                                f"Parando monitoramento."
                            )
                            self.monitored_threads.pop(thread_id, None)
                
                # Reiniciar threads mortas
                for thread_id, info in threads_to_restart:
                    restart_count = info["restart_count"]
                    restart_delay = info["restart_delay"]
                    
                    logger.warning(
                        f"Thread {thread_id} morreu. Reiniciando em {restart_delay}s "
                        f"(tentativa {restart_count + 1}/{info['max_restarts']})..."
                    )
                    
                    time.sleep(restart_delay)
                    
                    try:
                        # Criar nova thread usando o callback
                        new_thread = info["restart_callback"]()
                        info["thread"] = new_thread
                        info["restart_count"] += 1
                        info["last_restart"] = datetime.now()
                        logger.info(f"Thread {thread_id} reiniciada com sucesso")
                    except Exception as e:
                        logger.error(f"Erro ao reiniciar thread {thread_id}: {e}")
                        info["restart_count"] += 1
    
    def start(self):
        """Inicia o monitoramento."""
        if self.monitor_thread is None or not self.monitor_thread.is_alive():
            self._should_stop = False
            self.monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True)
            self.monitor_thread.start()
            logger.info("ThreadMonitor iniciado")
    
    def stop(self):
        """Para o monitoramento."""
        self._should_stop = True
        if self.monitor_thread:
            self.monitor_thread.join(timeout=5.0)
        logger.info("ThreadMonitor parado")


class DatabaseRecovery:
    """Recuperação e validação de banco de dados."""
    
    @staticmethod
    def validate_db_connection(db_file: str) -> bool:
        """Valida conexão com o banco de dados."""
        try:
            import sqlite3
            conn = sqlite3.connect(db_file, timeout=5.0)
            conn.execute("SELECT 1")
            conn.close()
            return True
        except Exception as e:
            logger.error(f"Erro ao validar banco de dados {db_file}: {e}")
            return False
    
    @staticmethod
    @retry_with_backoff(RetryConfig(max_retries=3, initial_delay=0.5))
    def execute_with_retry(operation: Callable, *args, **kwargs):
        """Executa operação de banco com retry."""
        return operation(*args, **kwargs)
    
    @staticmethod
    def backup_db(db_file: str, backup_dir: str = "backup") -> Optional[str]:
        """Cria backup do banco de dados."""
        try:
            import os
            import shutil
            from datetime import datetime
            
            if not os.path.exists(backup_dir):
                os.makedirs(backup_dir)
            
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_file = os.path.join(backup_dir, f"agent_{timestamp}.db")
            
            shutil.copy2(db_file, backup_file)
            logger.info(f"Backup criado: {backup_file}")
            return backup_file
        except Exception as e:
            logger.error(f"Erro ao criar backup: {e}")
            return None


class DataValidator:
    """Validação e sanitização de dados."""
    
    @staticmethod
    def validate_print_job(data: dict) -> tuple[bool, Optional[str]]:
        """Valida dados de um job de impressão."""
        if not isinstance(data, dict):
            return False, "Dados não são um dicionário"
        
        required_fields = ["job_id", "conteudo"]
        for field in required_fields:
            if field not in data:
                return False, f"Campo obrigatório ausente: {field}"
        
        job_id = data.get("job_id")
        if not isinstance(job_id, int) and not (isinstance(job_id, str) and job_id.isdigit()):
            return False, "job_id deve ser um número"
        
        conteudo = data.get("conteudo")
        if not isinstance(conteudo, dict):
            return False, "conteudo deve ser um dicionário"
        
        return True, None
    
    @staticmethod
    def sanitize_printer_config(config: dict) -> dict:
        """Sanitiza configuração de impressora."""
        sanitized = {}
        
        # Campos obrigatórios
        sanitized["device_id"] = str(config.get("device_id", "")).strip()
        sanitized["token"] = str(config.get("token", "")).strip()
        
        # Campos de conexão
        sanitized["connection_type"] = str(config.get("connection_type", "network")).strip().lower()
        if sanitized["connection_type"] not in ["network", "local"]:
            sanitized["connection_type"] = "network"
        
        # Configurações de rede
        if sanitized["connection_type"] == "network":
            sanitized["printer_ip"] = str(config.get("printer_ip", "192.168.1.100")).strip()
            try:
                sanitized["printer_port"] = int(config.get("printer_port") or 9100)
                if not (1 <= sanitized["printer_port"] <= 65535):
                    sanitized["printer_port"] = 9100
            except (ValueError, TypeError):
                sanitized["printer_port"] = 9100
        else:
            sanitized["printer_name_local"] = str(config.get("printer_name_local", "")).strip()
        
        # Configurações de impressão
        sanitized["printer_type"] = str(config.get("printer_type", "raw")).strip().lower()
        if sanitized["printer_type"] not in ["raw", "ipp"]:
            sanitized["printer_type"] = "raw"
        
        try:
            sanitized["paper_width"] = str(config.get("paper_width", "32")).strip()
            width = int(sanitized["paper_width"])
            if not (24 <= width <= 48):
                sanitized["paper_width"] = "32"
        except (ValueError, TypeError):
            sanitized["paper_width"] = "32"
        
        sanitized["printer_encoding"] = str(config.get("printer_encoding", "cp850")).strip().lower()
        valid_encodings = ["cp850", "cp860", "cp1252", "utf-8"]
        if sanitized["printer_encoding"] not in valid_encodings:
            sanitized["printer_encoding"] = "cp850"
        
        sanitized["name"] = str(config.get("name", "")).strip()
        
        return sanitized


class EncodingFallback:
    """Sistema de fallback para encoding."""
    
    ENCODING_ORDER = ["cp850", "cp860", "cp1252", "utf-8", "latin1"]
    
    @staticmethod
    def encode_with_fallback(text: str, preferred_encoding: str = "cp850") -> tuple[bytes, str]:
        """Codifica texto com fallback automático."""
        encodings_to_try = [preferred_encoding] + [
            enc for enc in EncodingFallback.ENCODING_ORDER if enc != preferred_encoding
        ]
        
        for encoding in encodings_to_try:
            try:
                return text.encode(encoding, errors="replace"), encoding
            except (UnicodeEncodeError, LookupError):
                continue
        
        # Último recurso: ASCII
        return text.encode("ascii", errors="replace"), "ascii"


# Instância global do monitor de threads
thread_monitor = ThreadMonitor()
