import socket
import http.client
import json
import threading
import time
from datetime import datetime

from error_recovery import (
    retry_with_backoff,
    RetryConfig,
    EncodingFallback,
)

# Tentar importar win32print para impressoras locais (Windows)
try:
    import win32print
    import win32api
    import tempfile
    import os
    import time
    HAS_WIN32PRINT = True
except ImportError:
    HAS_WIN32PRINT = False


# Tamanho do módulo do QR (1-16). 10 = maior, mais fácil de escanear no celular.
QR_MODULE_SIZE = 10

_active_lock = threading.Lock()
_active_socks = []  # (key, socket)
_cancel_gen = {}


class _QueueCancelled(Exception):
    """Impressão abortada porque a fila foi cancelada."""


def _as_int(value, default):
    try:
        if value is None or value == "":
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _printer_key(connection_type, printer_ip, printer_port, printer_name_local):
    if (connection_type or "network") == "local":
        return ("local", str(printer_name_local or "").strip().lower())
    return ("net", str(printer_ip or "").strip().lower(), _as_int(printer_port, 9100))


def _bump_cancel(key):
    with _active_lock:
        _cancel_gen[key] = _cancel_gen.get(key, 0) + 1
        return _cancel_gen[key]


def _generation(key):
    with _active_lock:
        return _cancel_gen.get(key, 0)


def _register_sock(key, sock):
    with _active_lock:
        _active_socks.append((key, sock))


def _unregister_sock(sock):
    with _active_lock:
        _active_socks[:] = [item for item in _active_socks if item[1] is not sock]


def _close_active_socks(key):
    with _active_lock:
        victims = [sock for k, sock in _active_socks if k == key]
    closed = 0
    for sock in victims:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except Exception:
            pass
        try:
            sock.close()
        except Exception:
            pass
        closed += 1
    return closed


_drain_until = {}


def _is_draining_key(key) -> bool:
    with _active_lock:
        until = _drain_until.get(key, 0)
    return time.monotonic() < until


def start_print_drain_for_config(cfg, seconds=120):
    """Para o envio e trata novos cupons como já impressos por alguns segundos."""
    svc = PrinterService.from_config(cfg or {}, timeout=2, max_retries=0)
    key = svc._printer_key()
    _bump_cancel(key)
    closed = _close_active_socks(key)
    with _active_lock:
        _drain_until[key] = time.monotonic() + max(1, int(seconds or 120))
    return key, closed


def is_print_draining_for_config(cfg) -> bool:
    svc = PrinterService.from_config(cfg or {}, timeout=2, max_retries=0)
    return _is_draining_key(svc._printer_key())


def _wrap_text_by_words(text: str, max_width: int) -> list:
    """Quebra texto por palavras para não cortar no meio; retorna lista de linhas."""
    if not text or max_width <= 0:
        return [text] if text else []
    text = text.strip()
    words = text.split()
    if not words:
        return []
    lines = []
    current = []
    current_len = 0
    for w in words:
        need = len(w) + (1 if current else 0)
        if current and current_len + need > max_width:
            lines.append(" ".join(current))
            current = [w]
            current_len = len(w)
        else:
            current.append(w)
            current_len = current_len + need if current_len else len(w)
    if current:
        lines.append(" ".join(current))
    return lines


def _escpos_qr_bytes(url: str, module_size=None) -> bytes:
    """Gera bytes ESC/POS para imprimir QR code (URL para entregador), tamanho legível."""
    if not url or len(url) > 400:
        return b""
    try:
        size = min(16, max(1, int(module_size or QR_MODULE_SIZE)))
        # GS ( k - Function 167: definir tamanho do módulo (n = 1-16; 10 = bem legível)
        cmd = bytes([0x1D, 0x28, 0x6B, 0x03, 0x00, 0x31, 0x43, size])
        # GS ( k - Function 169: nível de correção de erro (30 = L)
        cmd += b"\x1D\x28\x6B\x03\x00\x31\x45\x30"
        # GS ( k - Function 180: armazenar dados do QR
        data_bytes = url.encode("utf-8")
        n = len(data_bytes) + 3
        pL, pH = n % 256, n // 256
        cmd += bytes([0x1D, 0x28, 0x6B, pL, pH, 0x31, 0x50, 0x30]) + data_bytes
        # GS ( k - Function 181: imprimir o QR
        cmd += b"\x1D\x28\x6B\x03\x00\x31\x51\x30"
        return cmd
    except Exception:
        return b""


class PrinterService:
    """Serviço para impressão em impressoras de rede ou locais (Windows)"""
    
    def __init__(self, printer_ip=None, printer_port=9100, printer_type='raw', paper_width=32, printer_encoding='cp850', connection_type='network', printer_name_local=None, timeout=10, max_retries=2):
        """
        Inicializa o serviço de impressão

        Args:
            printer_ip: IP da impressora (obrigatório se connection_type='network')
            printer_port: Porta da impressora (9100 para RAW, 631 para IPP)
            printer_type: Tipo de conexão ('raw' ou 'ipp')
            paper_width: Largura em caracteres (32 para 58mm, 48 para 80mm)
            printer_encoding: Codificação para ç, ã, é (cp850, cp860, cp1252, utf8)
            connection_type: Tipo de conexão ('network' ou 'local')
            printer_name_local: Nome da impressora local (obrigatório se connection_type='local')
        """
        self.connection_type = connection_type or 'network'
        self.printer_ip = printer_ip
        self.printer_port = printer_port
        self.printer_type = printer_type
        self.paper_width = int(paper_width) if paper_width else 32
        self.printer_encoding = (printer_encoding or 'cp850').lower()
        self.printer_name_local = printer_name_local
        self.timeout = max(1, int(timeout or 10))
        self.max_retries = max(0, int(max_retries if max_retries is not None else 2))
    
    def print_receipt(self, receipt_data):
        """
        Imprime o recibo do pedido.
        Para pedidos delivery, inclui QR/URL para o entregador adicionar à rota.
        """
        if _is_draining_key(self._printer_key()):
            print("[INFO] Fila marcada como impressa — não envia cupom")
            return True
        try:
            receipt_text = self._generate_receipt_text(receipt_data)
            qr_bytes = b""
            if receipt_data.get("delivery_scan_url"):
                qr_bytes = _escpos_qr_bytes(
                    receipt_data["delivery_scan_url"],
                    receipt_data.get("qr_module_size"),
                )

            if self.connection_type == "local":
                return self._print_via_local(receipt_text, qr_bytes=qr_bytes)
            elif self.printer_type == "ipp":
                return self._print_via_ipp(receipt_text, qr_bytes=qr_bytes)
            else:
                return self._print_via_raw(receipt_text, qr_bytes=qr_bytes)
        except Exception as e:
            print(f"Erro ao imprimir: {str(e)}")
            return False
    
    def _generate_receipt_text(self, receipt):
        """Gera o texto formatado do recibo"""
        W = min(max(self.paper_width, 24), 48)  # 24-48 caracteres
        lines = []

        # Cabeçalho (pedidos de mesa: não imprimir título do form)
        is_mesa = bool(receipt.get("table_number"))
        if not is_mesa:
            lines.append("=" * W)
            lines.append(f" {receipt['form_name'].upper()[:W-2]}")
            lines.append("=" * W)
        if receipt.get('protocol'):
            lines.append(f"Pedido: {receipt['protocol'][:W-10]}")
        lines.append(f"Data: {receipt['date'][:W-6]}")
        if receipt.get('table_number'):
            lines.append(f"Mesa: {receipt['table_number'][:W-8]}")
        if receipt.get('garcom_name'):
            lines.append(f"Garcom: {receipt['garcom_name'][:W-10]}")
        if receipt.get('table_number') or receipt.get('garcom_name'):
            lines.append("")
        lines.append("")

        # Dados do cliente
        lines.append("CLIENTE:")
        lines.append(f" {receipt['customer']['name'][:W-2]}")
        if receipt['customer']['phone']:
            lines.append(f" Tel: {receipt['customer']['phone'][:W-6]}")
        if receipt['customer']['email']:
            lines.append(f" {receipt['customer']['email'][:W-2]}")
        # Campos configurados no Form Builder (aba Impressão) saem junto ao cliente
        for key, value in (receipt.get('custom_info') or {}).items():
            text = f" {key}: {str(value).strip()}"
            for wrap_line in _wrap_text_by_words(text, W) or [text[:W]]:
                lines.append(wrap_line[:W])
        lines.append("")
        lines.append("-" * W)
        lines.append("")

        # Itens agrupados por grupo (nome em uma linha; só qty e total, sem preço unitário)
        name_width = max(W - 14, 12)  # espaço à direita para "  Nx R$ XX,XX"
        for grupo, items in receipt['items_by_group'].items():
            lines.append(f"* {grupo.upper()[:W-4]} *")
            lines.append("")

            for item in items:
                name = (item.get('name') or 'Item').strip()
                half_lines = item.get('half_lines') or []
                is_half = item.get('type') == 'halfAndHalf' or bool(half_lines)
                # Meio a meio: título curto; sabores em linhas próprias (evita corte "..")
                if is_half:
                    name = "MEIO A MEIO"
                name_one_line = (name[: name_width - 2] + "..") if len(name) > name_width else name
                qty = item.get('quantity', 1) or 1
                addons = item.get('addons') or []
                # Valor unitário "seco" do produto (sem adicionais)
                unit_full = float(item.get('value', 0) or 0)
                addons_sum = sum(float(a.get('value', 0) or 0) for a in addons)
                base_unit = unit_full - addons_sum
                total_seco = round(base_unit * qty, 2)
                total_str = f"R$ {total_seco:.2f}".replace(".", ",")
                right_part = f" {qty}x {total_str}"
                if len(right_part) <= 14:
                    right_part = right_part.rjust(14)
                line = (name_one_line[:name_width].ljust(name_width)) + right_part
                lines.append(line[:W])
                # Metades do meio a meio (ex.: 1/2 PIZZA DE PRESUNTO)
                for half_name in half_lines:
                    half_text = f"  {str(half_name).strip()}"
                    for wrap_line in _wrap_text_by_words(half_text, W) or [half_text[:W]]:
                        lines.append(wrap_line[:W])
                # Integrantes do combo
                for ci in item.get('combo_items') or []:
                    ci_name = (ci.get('name') or 'Item').strip()
                    ci_qty = int(ci.get('quantity') or 1)
                    ci_val = float(ci.get('value') or 0)
                    ci_label = f"  > {ci_qty}x {ci_name}" if ci_qty > 1 else f"  > {ci_name}"
                    ci_str = f" R$ {ci_val:.2f}".replace(".", ",")
                    ci_one = ci_label[:W - len(ci_str)].ljust(W - len(ci_str)) + ci_str
                    lines.append(ci_one[:W])
                # Adicionais com valor
                for addon in addons:
                    addon_label = (addon.get('label') or 'Adicional').strip()
                    addon_val = float(addon.get('value', 0) or 0)
                    addon_str = f" R$ {addon_val:.2f}".replace(".", ",")
                    addon_one = ("  + " + addon_label)[:W - len(addon_str)].ljust(W - len(addon_str)) + addon_str
                    lines.append(addon_one[:W])
                # Observação do cliente (quebra em múltiplas linhas se necessário)
                obs = (item.get('observation') or '').strip()
                if obs:
                    obs_text = "  Obs: " + obs
                    while obs_text:
                        lines.append(obs_text[:W])
                        obs_text = ("    " + obs_text[W:]) if len(obs_text) > W else ""

            lines.append("")

        lines.append("-" * W)
        lines.append("")

        # Taxa de entrega (pedidos delivery): mostrar sempre que for delivery ou valor > 0
        delivery_fee = float(receipt.get("delivery_fee") or 0)
        if delivery_fee <= 0 and receipt.get("delivery_scan_url"):
            # Pedido delivery mas taxa não veio no payload: inferir por total - subtotal
            sub = float(receipt.get("subtotal") or 0)
            tot = float(receipt.get("total") or 0)
            if tot > sub:
                delivery_fee = round(tot - sub, 2)
        if delivery_fee > 0 or receipt.get("delivery_scan_url"):
            subtotal_val = receipt.get("subtotal")
            if subtotal_val is None:
                subtotal_val = (receipt.get("total") or 0) - delivery_fee
            subtotal_str = f"R$ {float(subtotal_val):.2f}".replace(".", ",")
            fee_str = f"R$ {delivery_fee:.2f}".replace(".", ",")
            lines.append("SUBTOTAL:")
            lines.append(f" {subtotal_str:>{W-1}}")
            lines.append("TAXA ENTREGA:")
            lines.append(f" {fee_str:>{W-1}}")
            lines.append("")

        # Total
        total_str = f"R$ {receipt['total']:.2f}".replace(".", ",")
        lines.append("TOTAL:")
        lines.append(f" {total_str:>{W-1}}")
        lines.append("")

        # QR Entregador (pedidos delivery): só título; o QR é impresso em seguida (bytes ESC/POS)
        if receipt.get("delivery_scan_url"):
            lines.append("-" * W)
            lines.append(" QR ENTREGADOR")
            lines.append(" Escaneie o QR abaixo")
            lines.append(" para add a rota")
            lines.append("")
            # Não imprimir a URL em texto (era o que saía no lugar do QR / poluía)
            lines.append("")
            lines.append("-" * W)
            lines.append("")

        lines.append("=" * W)
        lines.append("")
        lines.append("Obrigado pela preferência!")
        lines.append("")
        lines.append("")
        lines.append("")  # Espaços para cortar papel
        
        return "\n".join(lines)
    
    def _get_esc_pos_encoding(self):
        """Retorna (bytes_cmd, encoding) para caracteres portugueses (ç, ã, é)"""
        # ESC t n = Select character code table (ESC/POS)
        # 0=PC437, 1=PC850, 2=PC860(Português), 16=UTF-8/WPC1252
        enc = self.printer_encoding
        if enc == 'cp850':
            return b'\x1B\x74\x01', 'cp850'  # PC850 Multilingual
        if enc == 'cp860':
            return b'\x1B\x74\x02', 'cp860'  # PC860 Português
        if enc == 'cp1252':
            return b'\x1B\x74\x10', 'cp1252'  # Windows Latin-1
        # utf8: alguns modelos aceitam com ESC t 16
        return b'\x1B\x74\x10', 'utf-8'
    
    def _encode_text_with_fallback(self, text: str) -> bytes:
        """Codifica texto com fallback automático de encoding."""
        _, preferred_encoding = self._get_esc_pos_encoding()
        text_bytes, used_encoding = EncodingFallback.encode_with_fallback(text, preferred_encoding)
        if used_encoding != preferred_encoding:
            print(f"[WARN] Encoding {preferred_encoding} falhou, usando {used_encoding} como fallback")
        return text_bytes

    def _printer_key(self):
        return _printer_key(
            self.connection_type,
            self.printer_ip,
            self.printer_port,
            self.printer_name_local,
        )

    def _print_via_raw(self, text, qr_bytes=b""):
        """Imprime via socket RAW (porta 9100). qr_bytes: opcional, QR ESC/POS."""
        epoch = _generation(self._printer_key())

        @retry_with_backoff(RetryConfig(
            max_retries=self.max_retries,
            initial_delay=0.4,
            max_delay=2.0,
            retryable_exceptions=(socket.timeout, socket.error, ConnectionError)
        ))
        def _send_to_printer():
            if _generation(self._printer_key()) != epoch:
                raise _QueueCancelled()
            esc_encoding, encoding = self._get_esc_pos_encoding()
            # Usar fallback de encoding
            text_bytes = self._encode_text_with_fallback(text)
            esc_pos_reset = b"\x1B\x40"
            esc_pos_cut = b"\x1D\x56\x00"
            full_command = esc_pos_reset + esc_encoding + text_bytes + qr_bytes + esc_pos_cut
            
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            sock.settimeout(self.timeout)
            _register_sock(self._printer_key(), sock)
            try:
                sock.connect((self.printer_ip, int(self.printer_port)))
                if _generation(self._printer_key()) != epoch:
                    raise _QueueCancelled()
                sock.sendall(full_command)
            finally:
                _unregister_sock(sock)
                try:
                    sock.close()
                except Exception:
                    pass
            
            print(f"Pedido impresso com sucesso na impressora {self.printer_ip}:{self.printer_port}")
            return True
        
        try:
            return _send_to_printer()
        except _QueueCancelled:
            if _is_draining_key(self._printer_key()):
                print(f"[INFO] Fila marcada como impressa em {self.printer_ip}:{self.printer_port}")
                return True
            print(f"Impressão cancelada em {self.printer_ip}:{self.printer_port}")
            return False
        except socket.timeout:
            print(f"Timeout ao conectar na impressora {self.printer_ip}:{self.printer_port} após múltiplas tentativas")
            return False
        except socket.error as e:
            print(f"Erro de conexão com impressora {self.printer_ip}:{self.printer_port}: {str(e)}")
            return False
        except Exception as e:
            print(f"Erro ao imprimir via RAW: {str(e)}")
            return False
    
    def _print_via_ipp(self, text, qr_bytes=b""):
        """Imprime via IPP. qr_bytes: opcional."""
        try:
            _, encoding = self._get_esc_pos_encoding()
            text_bytes = text.encode(encoding, errors="replace")
            conn = http.client.HTTPConnection(self.printer_ip, self.printer_port, timeout=5)
            headers = {"Content-Type": "application/ipp"}
            ipp_payload = self._create_ipp_request(text_bytes + qr_bytes)
            headers["Content-Length"] = str(len(ipp_payload))
            conn.request("POST", "/ipp/print", ipp_payload, headers)
            response = conn.getresponse()
            conn.close()
            
            if response.status == 200:
                print(f"Pedido impresso com sucesso via IPP na impressora {self.printer_ip}:{self.printer_port}")
                return True
            else:
                print(f"Erro ao imprimir via IPP: Status {response.status}")
                return False
                
        except Exception as e:
            print(f"Erro ao imprimir via IPP: {str(e)}")
            return False
    
    def _create_ipp_request(self, data):
        """Cria requisição IPP básica"""
        # Versão IPP (2.0)
        ipp_request = b'\x02\x00'
        # Operação Print-Job (0x0002)
        ipp_request += b'\x00\x02'
        # Request ID
        ipp_request += b'\x00\x00\x00\x01'
        # Attributes
        ipp_request += b'\x01'  # Operation attributes tag
        ipp_request += b'\x47'  # charset
        ipp_request += b'\x00\x12attributes-charset'
        ipp_request += b'\x00\x05utf-8'
        ipp_request += b'\x48'  # naturalLanguage
        ipp_request += b'\x00\x1battributes-natural-language'
        ipp_request += b'\x00\x02pt'
        ipp_request += b'\x45'  # uri
        ipp_request += b'\x00\x0bprinter-uri'
        ipp_request += b'\x00\x1f'
        ipp_request += f'ipp://{self.printer_ip}/ipp/print'.encode('utf-8')
        ipp_request += b'\x03'  # End of attributes
        # Data
        ipp_request += data
        
        return ipp_request
    
    def _print_via_local(self, text, qr_bytes=b""):
        """Imprime via impressora local do Windows usando win32print com comandos ESC/POS."""
        if not HAS_WIN32PRINT:
            print("Erro: win32print não disponível. Apenas Windows suporta impressoras locais.")
            return False
        
        if not self.printer_name_local:
            print("Erro: Nome da impressora local não especificado.")
            return False
        
        @retry_with_backoff(RetryConfig(
            max_retries=2,
            initial_delay=1.0,
            max_delay=5.0,
            retryable_exceptions=(Exception,)
        ))
        def _send_to_local_printer():
            # Usar fallback de encoding
            text_bytes = self._encode_text_with_fallback(text)
            
            # Comando ESC/POS para cortar papel: GS V 0 (corte total)
            # 0x1D = GS (Group Separator)
            # 0x56 = V (comando de corte)
            # 0x00 = modo de corte (0 = corte total)
            esc_pos_cut = b"\x1D\x56\x00"
            
            # Comando ESC @ para inicializar a impressora
            esc_pos_init = b"\x1B\x40"
            
            # Combinar inicialização, texto, QR bytes e comando de corte
            full_content = esc_pos_init + text_bytes + qr_bytes + esc_pos_cut
            
            # Abrir a impressora local
            printer_handle = win32print.OpenPrinter(self.printer_name_local)
            try:
                # Iniciar documento com tipo RAW para enviar comandos ESC/POS diretamente
                job_info = ("Print Agent", None, "RAW")
                job_id = win32print.StartDocPrinter(printer_handle, 1, job_info)
                try:
                    win32print.StartPagePrinter(printer_handle)
                    # Enviar dados RAW (incluindo comandos ESC/POS)
                    win32print.WritePrinter(printer_handle, full_content)
                    win32print.EndPagePrinter(printer_handle)
                finally:
                    win32print.EndDocPrinter(printer_handle)
            finally:
                win32print.ClosePrinter(printer_handle)
            
            print(f"Pedido impresso com sucesso na impressora local: {self.printer_name_local}")
            return True
        
        try:
            return _send_to_local_printer()
        except Exception as e:
            print(f"Erro ao imprimir na impressora local {self.printer_name_local}: {str(e)}")
            return False

    @classmethod
    def from_config(cls, cfg, timeout=4, max_retries=0):
        cfg = cfg or {}
        return cls(
            printer_ip=str(cfg.get("printer_ip") or "").strip() or None,
            printer_port=_as_int(cfg.get("printer_port"), 9100),
            printer_type=(str(cfg.get("printer_type") or "raw").strip().lower() or "raw"),
            paper_width=_as_int(cfg.get("paper_width"), 32),
            printer_encoding=cfg.get("printer_encoding") or "cp850",
            connection_type=(str(cfg.get("connection_type") or "network").strip().lower() or "network"),
            printer_name_local=str(cfg.get("printer_name_local") or "").strip() or None,
            timeout=timeout,
            max_retries=max_retries,
        )

    def cancel_queue(self):
        """Cancela a fila da impressora (buffer ESC/POS e/ou spooler do Windows)."""
        try:
            if self.connection_type == "local":
                return self._cancel_local_queue()
            if self.printer_type == "ipp":
                return self._cancel_ipp_queue()
            return self._cancel_raw_queue()
        except Exception as e:
            print(f"[WARN] cancelar fila falhou: {e}")
            return False, str(e)

    def _escpos_clear_buffer_bytes(self):
        # DLE DC4 fn=8: limpa buffer de recepção (Epson TM).
        # CAN + ESC @: cancela modo página e reinicia (ESC/POS genérico).
        # Não enviar DLE ENQ: a impressora responde 1 byte e alguns modelos fecham o TCP.
        return (
            bytes([0x10, 0x14, 0x08, 0x01, 0x03, 0x14, 0x01, 0x06, 0x02, 0x08])
            + b"\x18\x1B\x40"
        )

    def _cancel_raw_queue(self):
        ip = str(self.printer_ip or "").strip()
        if not ip:
            return False, "IP da impressora não informado"
        port = _as_int(self.printer_port, 9100)
        key = self._printer_key()
        _bump_cancel(key)
        interrupted = _close_active_socks(key)
        time.sleep(0.2)
        last_err = None
        for _attempt in range(3):
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            sock.settimeout(min(max(self.timeout, 1), 4))
            try:
                sock.connect((ip, port))
                try:
                    sock.sendall(self._escpos_clear_buffer_bytes())
                except Exception as send_exc:
                    print(f"[WARN] clear buffer em {ip}:{port}: {send_exc}")
                print(f"[INFO] Fila cancelada em {ip}:{port}")
                return True, f"Fila cancelada em {ip}:{port}"
            except Exception as e:
                last_err = e
                time.sleep(0.3)
            finally:
                try:
                    sock.close()
                except Exception:
                    pass
        if interrupted:
            return True, f"Envio interrompido para {ip}:{port}"
        return False, f"Não conectou em {ip}:{port}: {last_err}"

    def _cancel_ipp_queue(self):
        ip = str(self.printer_ip or "").strip()
        if not ip:
            return False, "IP da impressora não informado"
        key = self._printer_key()
        _bump_cancel(key)
        _close_active_socks(key)
        try:
            conn = http.client.HTTPConnection(ip, _as_int(self.printer_port, 631), timeout=min(max(self.timeout, 1), 4))
            payload = self._create_ipp_purge_jobs()
            headers = {"Content-Type": "application/ipp", "Content-Length": str(len(payload))}
            conn.request("POST", "/ipp/print", payload, headers)
            response = conn.getresponse()
            conn.close()
            if response.status not in (200, 204):
                raise RuntimeError(f"IPP {response.status}")
            print(f"[INFO] Fila IPP cancelada em {ip}:{self.printer_port}")
            return True, f"Fila cancelada via IPP em {ip}:{self.printer_port}"
        except Exception as ipp_exc:
            # Muitas térmicas "IPP" ainda escutam RAW 9100 — tenta limpar o buffer ESC/POS.
            try:
                raw = PrinterService(
                    printer_ip=ip,
                    printer_port=9100,
                    printer_type="raw",
                    connection_type="network",
                    timeout=min(max(self.timeout, 1), 4),
                    max_retries=0,
                )
                ok, msg = raw._cancel_raw_queue()
                if ok:
                    return True, msg
            except Exception:
                pass
            return False, str(ipp_exc)

    def _create_ipp_purge_jobs(self):
        uri = f"ipp://{self.printer_ip}/ipp/print".encode("utf-8")
        req = b"\x02\x00"  # IPP 2.0
        req += b"\x00\x12"  # Purge-Jobs
        req += b"\x00\x00\x00\x01"
        req += b"\x01"  # operation attributes
        req += b"\x47\x00\x12attributes-charset\x00\x05utf-8"
        req += b"\x48\x00\x1battributes-natural-language\x00\x02pt"
        req += b"\x45\x00\x0bprinter-uri" + len(uri).to_bytes(2, "big") + uri
        req += b"\x03"
        return req

    def _cancel_local_queue(self):
        if not HAS_WIN32PRINT:
            return False, "Impressora local só está disponível no Windows"
        if not self.printer_name_local:
            return False, "Nome da impressora local não informado"
        _bump_cancel(self._printer_key())
        handle = win32print.OpenPrinter(self.printer_name_local)
        deleted = 0
        try:
            purge = getattr(win32print, "PRINTER_CONTROL_PURGE", 3)
            try:
                win32print.SetPrinter(handle, 0, None, purge)
            except Exception:
                pass
            try:
                jobs = win32print.EnumJobs(handle, 0, 999, 1) or []
            except Exception:
                jobs = []
            for job in jobs:
                job_id = None
                if isinstance(job, dict):
                    job_id = job.get("JobId")
                elif isinstance(job, (tuple, list)) and job:
                    job_id = job[0]
                if not job_id:
                    continue
                try:
                    win32print.SetJob(handle, job_id, 0, None, win32print.JOB_CONTROL_DELETE)
                    deleted += 1
                except Exception:
                    try:
                        win32print.SetJob(handle, job_id, 0, None, win32print.JOB_CONTROL_CANCEL)
                        deleted += 1
                    except Exception:
                        pass
            try:
                job_info = ("Print Agent cancel queue", None, "RAW")
                win32print.StartDocPrinter(handle, 1, job_info)
                try:
                    win32print.StartPagePrinter(handle)
                    win32print.WritePrinter(handle, self._escpos_clear_buffer_bytes())
                    win32print.EndPagePrinter(handle)
                finally:
                    win32print.EndDocPrinter(handle)
            except Exception as esc_exc:
                print(f"[WARN] ESC/POS clear na local falhou: {esc_exc}")
        finally:
            win32print.ClosePrinter(handle)
        print(f"[INFO] Fila local cancelada em {self.printer_name_local} ({deleted} job(s))")
        return True, f"Fila cancelada em {self.printer_name_local}"