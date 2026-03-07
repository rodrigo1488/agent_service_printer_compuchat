import socket
import http.client
import json
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


def _escpos_qr_bytes(url: str) -> bytes:
    """Gera bytes ESC/POS para imprimir QR code (URL para entregador), tamanho legível."""
    if not url or len(url) > 400:
        return b""
    try:
        # GS ( k - Function 167: definir tamanho do módulo (n = 1-16; 10 = bem legível)
        cmd = bytes([0x1D, 0x28, 0x6B, 0x03, 0x00, 0x31, 0x43, min(16, max(1, QR_MODULE_SIZE))])
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
    
    def __init__(self, printer_ip=None, printer_port=9100, printer_type='raw', paper_width=32, printer_encoding='cp850', connection_type='network', printer_name_local=None):
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
    
    def print_receipt(self, receipt_data):
        """
        Imprime o recibo do pedido.
        Para pedidos delivery, inclui QR/URL para o entregador adicionar à rota.
        """
        try:
            receipt_text = self._generate_receipt_text(receipt_data)
            qr_bytes = b""
            if receipt_data.get("delivery_scan_url"):
                qr_bytes = _escpos_qr_bytes(receipt_data["delivery_scan_url"])

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
                # Adicionais com valor
                for addon in addons:
                    addon_label = (addon.get('label') or 'Adicional').strip()
                    addon_val = float(addon.get('value', 0) or 0)
                    addon_str = f" R$ {addon_val:.2f}".replace(".", ",")
                    addon_one = ("  + " + addon_label)[:W - len(addon_str)].ljust(W - len(addon_str)) + addon_str
                    lines.append(addon_one[:W])

            lines.append("")

        lines.append("-" * W)
        lines.append("")

        # Informações adicionais
        if receipt['custom_info']:
            lines.append("OBS:")
            for key, value in receipt['custom_info'].items():
                lines.append(f" {key}: {str(value)[:W-4]}")
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

    def _print_via_raw(self, text, qr_bytes=b""):
        """Imprime via socket RAW (porta 9100). qr_bytes: opcional, QR ESC/POS."""
        @retry_with_backoff(RetryConfig(
            max_retries=2,
            initial_delay=0.5,
            max_delay=5.0,
            retryable_exceptions=(socket.timeout, socket.error, ConnectionError)
        ))
        def _send_to_printer():
            esc_encoding, encoding = self._get_esc_pos_encoding()
            # Usar fallback de encoding
            text_bytes = self._encode_text_with_fallback(text)
            esc_pos_reset = b"\x1B\x40"
            esc_pos_cut = b"\x1D\x56\x00"
            full_command = esc_pos_reset + esc_encoding + text_bytes + qr_bytes + esc_pos_cut
            
            # Conectar e enviar com timeout maior
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(10)  # Aumentado de 5 para 10 segundos
            try:
                sock.connect((self.printer_ip, self.printer_port))
                sock.sendall(full_command)
            finally:
                sock.close()
            
            print(f"Pedido impresso com sucesso na impressora {self.printer_ip}:{self.printer_port}")
            return True
        
        try:
            return _send_to_printer()
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