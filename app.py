"""Print Agent - Interface web e servidor HTTP."""
import os
import sys
import threading
from datetime import datetime
from typing import List, Optional
from flask import Flask, request, redirect, url_for, render_template, jsonify, send_file
from flask_cors import CORS

import db
from agent import start_agent_thread, stop_agent
from product_sync import refresh_product_sync_thread, start_product_sync_thread
from error_recovery import DataValidator, DatabaseRecovery
from pos_api import pos_bp

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
app.register_blueprint(pos_bp)

# Inicializar banco na importação
db.init_db()

# Validar banco de dados na inicialização
if not DatabaseRecovery.validate_db_connection(db.DB_FILE):
    print("[WARN] Problema detectado no banco de dados. Criando backup...")
    DatabaseRecovery.backup_db(db.DB_FILE)


def _config_context():
    """Dados para o template de configuração: ws_url, lista de impressoras e opções."""
    ws_url = db.get_config("ws_url")
    printers = db.get_printers()
    restart_on_save = (db.get_config("restart_service_on_save") or "true").lower() == "true"
    uniplus_enabled = (db.get_config("uniplus_enabled") or "false").lower() in ("true", "1", "yes", "on")
    uniplus_connection_string = db.get_config("uniplus_connection_string") or ""
    uniplus_produto_table = db.get_config("uniplus_produto_table") or "produto"
    uniplus_produto_codigo_column = db.get_config("uniplus_produto_codigo_column") or "codigo"
    uniplus_produto_id_column = db.get_config("uniplus_produto_id_column") or "id"
    uniplus_produto_preco_column = db.get_config("uniplus_produto_preco_column") or "preco"
    uniplus_produto_nome_column = db.get_config("uniplus_produto_nome_column") or "nome"
    uniplus_contamesa_table = db.get_config("uniplus_contamesa_table") or "contamesa"
    uniplus_contamesaitem_table = db.get_config("uniplus_contamesaitem_table") or "contamesaitem"
    uniplus_product_sync_poll = (db.get_config("uniplus_product_sync_poll") or "false").lower() in (
        "true", "1", "yes", "on"
    )
    pos_api_token = db.get_config("pos_api_token") or ""
    uniplus_mesa_tipopedido = db.get_config("uniplus_mesa_tipopedido") or "1"
    pos_images = _pos_images_for_ui()
    return {
        "active_nav": "config",
        "ws_url": ws_url,
        "printers": printers,
        "restart_service_on_save": restart_on_save,
        "uniplus_enabled": uniplus_enabled,
        "uniplus_connection_string": uniplus_connection_string,
        "uniplus_produto_table": uniplus_produto_table,
        "uniplus_produto_codigo_column": uniplus_produto_codigo_column,
        "uniplus_produto_id_column": uniplus_produto_id_column,
        "uniplus_produto_preco_column": uniplus_produto_preco_column,
        "uniplus_produto_nome_column": uniplus_produto_nome_column,
        "uniplus_contamesa_table": uniplus_contamesa_table,
        "uniplus_contamesaitem_table": uniplus_contamesaitem_table,
        "uniplus_product_sync_poll": uniplus_product_sync_poll,
        "pos_api_token": pos_api_token,
        "uniplus_mesa_tipopedido": uniplus_mesa_tipopedido,
        "pos_catalog_version": db.get_config("pos_catalog_version") or "0",
        "pos_last_sync_error": db.get_config("pos_last_sync_error") or "",
        "pos_images": pos_images,
        "pos_images_ok": sum(1 for i in pos_images if i.get("exists")),
        "pos_images_missing": sum(1 for i in pos_images if not i.get("exists")),
    }


def _pos_images_for_ui():
    """Imagens do catálogo POS com nomes dos produtos e se o arquivo existe."""
    names_by_id = {}
    for product in db.list_pos_products():
        image_id = str(product.get("imageId") or "").strip()
        if not image_id:
            continue
        name = str(product.get("name") or "").strip() or f"Produto {product.get('id')}"
        names_by_id.setdefault(image_id, []).append(name)
    out = []
    for img in db.list_pos_images():
        path = img.get("path") or ""
        exists = bool(path and os.path.isfile(path))
        out.append(
            {
                "id": img.get("id"),
                "exists": exists,
                "products": names_by_id.get(str(img.get("id") or ""), []),
            }
        )
    out.sort(key=lambda i: (not i["exists"], (i["products"][0] if i["products"] else i["id"] or "").lower()))
    return out


def _build_health_status():
    """Monta o payload de saúde usado por /health e /status."""
    from error_recovery import ConnectionHealthChecker, thread_monitor
    from agent import _agent_threads

    health_status = {
        "status": "ok",
        "message": "Print Agent is running",
        "timestamp": datetime.now().isoformat(),
        "database": {
            "status": "ok" if DatabaseRecovery.validate_db_connection(db.DB_FILE) else "error"
        },
        "threads": {
            "total": len(_agent_threads),
            "alive": sum(1 for t in _agent_threads if t.is_alive()),
            "monitored": len(thread_monitor.monitored_threads) if hasattr(thread_monitor, "monitored_threads") else 0,
        },
        "printers": {
            "configured": len(db.get_printers()),
            "active": sum(1 for p in db.get_printers() if p.get("device_id") and p.get("token")),
        },
    }

    printers = db.get_printers()
    printer_health = []
    for p in printers:
        if p.get("connection_type") == "network":
            printer_ip = p.get("printer_ip", "")
            printer_port = p.get("printer_port", 9100)
            is_accessible = (
                ConnectionHealthChecker.check_printer_connection(printer_ip, printer_port)
                if printer_ip
                else False
            )
            printer_health.append(
                {
                    "device_id": p.get("device_id", ""),
                    "connection_type": "network",
                    "printer_ip": printer_ip,
                    "printer_port": printer_port,
                    "accessible": is_accessible,
                }
            )
        else:
            printer_health.append(
                {
                    "device_id": p.get("device_id", ""),
                    "connection_type": "local",
                    "accessible": True,
                }
            )

    health_status["printers"]["health"] = printer_health

    # UniPlus: NÃO abrir conexão no health (Unico interpreta sessão concorrente).
    from uniplus_handler import is_uniplus_enabled
    from product_sync import is_product_sync_poll_enabled

    uniplus_on = is_uniplus_enabled(db)
    uniplus_info = {
        "enabled": uniplus_on,
        "db_ok": None,
        "product_sync_poll": is_product_sync_poll_enabled(),
        "last_error": db.get_config("uniplus_last_error") or "",
        "note": "conexão sob demanda (jobs/produtos); health não testa o Postgres",
    }
    health_status["uniplus"] = uniplus_info

    if health_status["database"]["status"] != "ok":
        health_status["status"] = "degraded"
    if health_status["threads"]["alive"] < health_status["threads"]["total"]:
        health_status["status"] = "degraded"

    return health_status


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
            from uniplus_handler import validate_uniplus_connection, _IDENT_RE

            # UniPlus Gourmet (Postgres local) — validar antes de persistir
            uniplus_enabled = request.form.get("uniplus_enabled", "").lower() in ("true", "1", "on", "yes")
            uniplus_dsn = request.form.get("uniplus_connection_string", "").strip()
            uniplus_tables = {
                "uniplus_produto_table": request.form.get("uniplus_produto_table", "produto").strip() or "produto",
                "uniplus_produto_codigo_column": request.form.get("uniplus_produto_codigo_column", "codigo").strip() or "codigo",
                "uniplus_produto_id_column": request.form.get("uniplus_produto_id_column", "id").strip() or "id",
                "uniplus_produto_preco_column": request.form.get("uniplus_produto_preco_column", "preco").strip() or "preco",
                "uniplus_produto_nome_column": request.form.get("uniplus_produto_nome_column", "nome").strip() or "nome",
                "uniplus_contamesa_table": request.form.get("uniplus_contamesa_table", "contamesa").strip() or "contamesa",
                "uniplus_contamesaitem_table": request.form.get("uniplus_contamesaitem_table", "contamesaitem").strip()
                or "contamesaitem",
            }
            for key, value in uniplus_tables.items():
                if not _IDENT_RE.match(value):
                    raise ValueError(f"Identificador UniPlus inválido em {key}: {value}")

            if uniplus_enabled:
                ok_dsn, dsn_msg = validate_uniplus_connection(
                    uniplus_dsn,
                    contamesa_table=uniplus_tables["uniplus_contamesa_table"],
                    contamesaitem_table=uniplus_tables["uniplus_contamesaitem_table"],
                )
                if not ok_dsn:
                    raise ValueError(f"UniPlus Postgres inválido: {dsn_msg}")
                db.set_config("uniplus_last_error", "")
            else:
                db.set_config("uniplus_last_error", "")

            ws_url = request.form.get("ws_url", "").strip()
            if ws_url:
                db.set_config("ws_url", ws_url)

            uniplus_product_sync_poll = request.form.get(
                "uniplus_product_sync_poll", ""
            ).lower() in ("true", "1", "on", "yes")

            db.set_config("uniplus_enabled", "true" if uniplus_enabled else "false")
            db.set_config("uniplus_connection_string", uniplus_dsn)
            db.set_config("pos_api_token", request.form.get("pos_api_token", "").strip())
            db.set_config(
                "uniplus_mesa_tipopedido",
                (request.form.get("uniplus_mesa_tipopedido") or "1").strip() or "1",
            )
            db.set_config(
                "uniplus_product_sync_poll",
                "true" if uniplus_product_sync_poll else "false",
            )
            try:
                refresh_product_sync_thread()
            except Exception as sync_exc:
                print(f"[WARN] refresh product_sync: {sync_exc}")
            for key, value in uniplus_tables.items():
                db.set_config(key, value)

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
                
                # Sanitizar dados antes de adicionar
                printer_data = DataValidator.sanitize_printer_config(printer_data)
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
            return redirect(
                url_for(
                    "index",
                    message="Configuração salva com sucesso!"
                    + (" Serviço reiniciado." if restart_on_save else ""),
                    message_type="success",
                )
                + "#conexao"
            )
        except Exception as e:
            import traceback
            error_msg = f"Erro: {str(e)}"
            print(f"[ERROR] Erro ao salvar configuração: {error_msg}")
            print(f"[ERROR] Traceback: {traceback.format_exc()}")
            try:
                db.set_config("uniplus_last_error", error_msg[:500])
            except Exception:
                pass
            ctx = _config_context()
            return render_template(
                "config.html",
                **ctx,
                message=error_msg,
                message_type="error",
            )
    return render_template("config.html", **_config_context())


@app.route("/pos/catalog-sync", methods=["POST"])
def pos_catalog_sync_ui():
    """Sync manual do catálogo POS a partir da UI do Agent."""
    try:
        from pos_catalog import sync_catalog_from_cloud

        result = sync_catalog_from_cloud()
        return redirect(
            url_for(
                "index",
                message=f"Catálogo POS sincronizado (v{result.get('catalogVersion')}).",
                message_type="success",
            )
            + "#pos"
        )
    except Exception as e:
        return redirect(
            url_for("index", message=f"Falha no sync POS: {e}", message_type="error")
            + "#pos"
        )


@app.route("/pos/media-preview/<image_id>", methods=["GET"])
def pos_media_preview(image_id: str):
    """Preview das imagens POS na UI do Agent (sem token do tablet)."""
    safe = "".join(ch for ch in (image_id or "") if ch.isalnum() or ch in "-_.")
    if not safe or safe != image_id:
        return "not_found", 404
    row = db.get_pos_image(safe)
    path = (row or {}).get("path") or ""
    if not path or not os.path.isfile(path):
        return "not_found", 404
    return send_file(path)


@app.route("/pos/images-sync", methods=["POST"])
def pos_images_sync_ui():
    """Força o download das imagens do catálogo cloud."""
    try:
        from pos_catalog import sync_images_from_cloud

        result = sync_images_from_cloud(force=True)
        stats = result.get("images") or {}
        return redirect(
            url_for(
                "index",
                message=(
                    f"Imagens POS: {stats.get('downloaded', 0)} baixadas, "
                    f"{stats.get('failed', 0)} falhas."
                ),
                message_type="success",
            )
            + "#pos"
        )
    except Exception as e:
        return redirect(
            url_for("index", message=f"Falha ao baixar imagens: {e}", message_type="error")
            + "#pos"
        )


@app.route("/health", methods=["GET"])
def health():
    """Health check JSON para monitoramento."""
    health_status = _build_health_status()
    status_code = 200 if health_status["status"] == "ok" else 503
    return jsonify(health_status), status_code


@app.route("/status")
def status_page():
    """Painel visual de saúde do agente."""
    health_status = _build_health_status()
    return render_template(
        "status.html",
        active_nav="status",
        health=health_status,
        ws_url=db.get_config("ws_url") or "",
        uniplus_enabled=(db.get_config("uniplus_enabled") or "false").lower()
        in ("true", "1", "yes", "on"),
    )


@app.route("/logs")
def logs():
    """Histórico de impressões com filtros."""
    status_filter = (request.args.get("status") or "all").strip().lower()
    kind_filter = (request.args.get("kind") or "all").strip().lower()
    q = (request.args.get("q") or "").strip()
    try:
        limit = int(request.args.get("limit") or 50)
    except (TypeError, ValueError):
        limit = 50
    limit = max(1, min(limit, 500))
    logs_list = db.get_print_logs(
        limit=limit, status=status_filter, q=q, kind=kind_filter
    )
    stats = db.get_print_log_stats()
    return render_template(
        "logs.html",
        active_nav="logs",
        logs=logs_list,
        stats=stats,
        status_filter=status_filter,
        kind_filter=kind_filter,
        q=q,
        limit=limit,
    )


def _find_parent_name(parents, product_id):
    try:
        pid = int(product_id)
    except (TypeError, ValueError):
        return None
    for p in parents or []:
        if p.get("id") == pid:
            return p.get("name")
    return None


def _friendly_error(exc: Exception, parents) -> str:
    """Traduz erros crus do backend (ex.: ERR_UNIPLUS_CODIGO_IN_OTHER_OPTION:12) em mensagens legíveis."""
    msg = str(exc)
    if "ERR_UNIPLUS_CODIGO_IN_OTHER_OPTION" in msg:
        try:
            other_id = msg.split("ERR_UNIPLUS_CODIGO_IN_OTHER_OPTION:")[1].split('"')[0].strip()
            other_id = "".join(ch for ch in other_id if ch.isdigit())
        except Exception:
            other_id = ""
        other_name = _find_parent_name(parents, other_id) if other_id else None
        alvo = f"“{other_name}” (#{other_id})" if other_name else f"produto #{other_id}"
        return f"Este código já está vinculado a {alvo}. Desvincule antes de anexar aqui."
    if "ERR_UNIPLUS_ATTACH_NOT_LEAF" in msg:
        return "O produto avulso com esse código já tem variações próprias — remova manualmente antes de vincular."
    if "ERR_PRODUCT_NOT_FOUND" in msg:
        return "Produto Compuchat não encontrado (pode ter sido removido). Atualize a página e tente de novo."
    if "ERR_UNIPLUS_ADDON_NOT_FOUND" in msg:
        return "Adicional Compuchat não encontrado (pode ter sido removido). Atualize a página e tente de novo."
    if "ERR_UNIPLUS_ADDON_ID_REQUIRED" in msg:
        return "Selecione um adicional válido na lista."
    return msg


@app.route("/products")
def products_page():
    """Lista produtos UniPlus com status de vínculo e agrupamento por produto pai."""
    import product_sync

    q = (request.args.get("q") or "").strip()
    message = request.args.get("message")
    message_type = request.args.get("message_type", "success")
    uniplus_on = (db.get_config("uniplus_enabled") or "false").lower() in (
        "true",
        "1",
        "yes",
        "on",
    )
    products = []
    parents = []
    parents_error = None
    addons_catalog = []
    addons_groups = []
    addons_error = None
    pending_singles = []
    pending_count = 0
    clusters = []
    linked_cards = []
    linked_codes_count = 0
    grupos = []
    enabled_map = {p["codigo"]: p for p in db.list_sync_products()}
    enabled_count = sum(1 for p in enabled_map.values() if p.get("enabled"))
    list_error = None
    if uniplus_on:
        try:
            products = product_sync.list_uniplus_products(q=q, limit=2000)
            for p in products:
                local = enabled_map.get(p["codigo"]) or {}
                p["sync_enabled"] = bool(local.get("enabled"))
                p["last_synced_at"] = local.get("last_synced_at")
                p["last_error"] = local.get("last_error") or ""
                p["suggested_label"] = product_sync.suggest_option_label(
                    p.get("nome") or "", p.get("codigo") or ""
                )
        except Exception as e:
            list_error = str(e)
            message = f"Erro ao listar produtos UniPlus: {e}"
            message_type = "error"
        try:
            catalog = product_sync.list_compuchat_catalog(limit=1000)
            parents = catalog.get("products") or []
            addons_catalog = catalog.get("addOns") or []
            addons_groups = catalog.get("addOnGroups") or []
        except Exception as e:
            parents_error = str(e)
            addons_error = str(e)
        if products:
            product_sync.annotate_link_status(products, parents, addons_catalog)
            pending, linked = product_sync.split_pending_and_linked(products)
            pending_count = len(pending)
            clusters = product_sync.suggest_flavor_clusters(pending)
            clustered_codes = {
                item.get("codigo") for cluster in clusters for item in cluster["codes"]
            }
            pending_singles = [
                p for p in pending if p.get("codigo") not in clustered_codes
            ]
            linked_cards = product_sync.build_linked_cards(linked)
            linked_codes_count = len(linked)
        grupos = product_sync.distinct_grupos(parents)
    return render_template(
        "products.html",
        active_nav="products",
        products=products,
        parents=parents,
        parents_error=parents_error,
        addons_catalog=addons_catalog,
        addons_groups=addons_groups,
        addons_error=addons_error,
        pending_singles=pending_singles,
        pending_count=pending_count,
        clusters=clusters,
        linked_cards=linked_cards,
        linked_codes_count=linked_codes_count,
        grupos=grupos,
        enabled_count=enabled_count,
        q=q,
        uniplus_enabled=uniplus_on,
        message=message,
        message_type=message_type,
        list_error=list_error,
    )


@app.route("/products/link-standalone", methods=["POST"])
def products_link_standalone():
    """Vincula codigo UniPlus a um produto avulso (novo ou existente) no Compuchat."""
    import product_sync  # noqa: F401 (usado abaixo)

    codigo = (request.form.get("codigo") or "").strip()
    q = (request.form.get("q") or "").strip()
    nome = (request.form.get("nome") or "").strip()
    grupo = (request.form.get("grupo") or "").strip()
    preco_raw = (request.form.get("preco") or "").strip()
    product_id_raw = (request.form.get("productId") or "").strip()
    try:
        preco = float(preco_raw) if preco_raw else 0.0
    except ValueError:
        preco = 0.0
    product_id = None
    if product_id_raw:
        try:
            product_id = int(product_id_raw)
        except ValueError:
            product_id = None

    parents_for_errors = []
    try:
        parents_for_errors = product_sync.list_compuchat_products(limit=1000)
    except Exception:
        pass

    try:
        if not codigo or not nome:
            raise RuntimeError("Informe o codigo e o nome do produto")
        result = product_sync.link_standalone(
            codigo=codigo,
            nome=nome,
            preco=preco,
            grupo=grupo,
            product_id=product_id,
        )
        action = result.get("action") or "ok"
        msg = f"Produto avulso {action}: {codigo} → produto #{result.get('productId')}"
        if result.get("removedProductId"):
            msg += f" · desvinculado #{result.get('removedProductId')}"
        try:
            product_sync.enable_product(codigo, enabled=True)
        except Exception:
            pass
        mtype = "success"
    except Exception as e:
        msg = f"Falha ao vincular {codigo}: {_friendly_error(e, parents_for_errors)}"
        mtype = "error"
    return redirect(
        url_for("products_page", q=q, message=msg, message_type=mtype)
    )


@app.route("/products/link-addon", methods=["POST"])
def products_link_addon():
    """Vincula codigo UniPlus a um adicional existente ou cria item em grupo."""
    import product_sync

    codigo = (request.form.get("codigo") or "").strip()
    q = (request.form.get("q") or "").strip()
    add_on_item_id_raw = (request.form.get("addOnItemId") or "").strip()
    add_on_group_id_raw = (request.form.get("addOnGroupId") or "").strip()
    label = (request.form.get("label") or "").strip()
    preco_raw = (request.form.get("preco") or "").strip()
    create_new = (request.form.get("createNewItem") or "").strip() in (
        "1",
        "true",
        "on",
        "yes",
    )

    try:
        preco = float(preco_raw) if preco_raw else 0.0
    except ValueError:
        preco = 0.0

    try:
        if not codigo:
            raise RuntimeError("Informe o codigo UniPlus")

        add_on_item_id = None
        add_on_group_id = None
        if add_on_item_id_raw and not create_new:
            try:
                add_on_item_id = int(add_on_item_id_raw)
            except ValueError:
                add_on_item_id = None
        if add_on_group_id_raw:
            try:
                add_on_group_id = int(add_on_group_id_raw)
            except ValueError:
                add_on_group_id = None

        if create_new:
            if not add_on_group_id:
                raise RuntimeError("Selecione o grupo de adicionais")
            if not label:
                raise RuntimeError("Informe o nome do novo adicional")
            result = product_sync.link_addon(
                codigo=codigo,
                add_on_group_id=add_on_group_id,
                label=label,
                value=preco,
            )
            msg = (
                f"Adicional criado e vinculado: {codigo} → "
                f"\"{result.get('label')}\" (#{result.get('addOnItemId')})"
            )
        else:
            if not add_on_item_id:
                raise RuntimeError("Selecione o adicional para vincular")
            result = product_sync.link_addon(
                codigo=codigo, add_on_item_id=add_on_item_id
            )
            msg = (
                f"Adicional vinculado: {codigo} → "
                f"\"{result.get('label')}\" (#{result.get('addOnItemId')})"
            )

        if result.get("removedProductId"):
            msg += f" · desvinculado produto #{result.get('removedProductId')}"
        if result.get("clearedOptionIds"):
            msg += f" · desvinculado(s) option(s) {result.get('clearedOptionIds')}"
        mtype = "success"
    except Exception as e:
        msg = f"Falha ao vincular adicional {codigo}: {_friendly_error(e, [])}"
        mtype = "error"
    return redirect(
        url_for("products_page", q=q, message=msg, message_type=mtype)
    )


@app.route("/products/unlink", methods=["POST"])
def products_unlink():
    """Remove o vinculo (avulso ou variação) de um codigo UniPlus no Compuchat."""
    import product_sync

    codigo = (request.form.get("codigo") or "").strip()
    q = (request.form.get("q") or "").strip()
    try:
        if not codigo:
            raise RuntimeError("Informe o codigo")
        result = product_sync.unlink_codigo(codigo=codigo)
        # Desliga sync automático local, senão o próximo poll/"Sync agora"
        # recria o vínculo avulso automaticamente (grupo "Outros").
        try:
            db.set_sync_product_enabled(codigo, False)
        except Exception:
            pass
        msg = f"Código {codigo} desvinculado."
        if result.get("removedProductId"):
            msg += f" · produto avulso #{result.get('removedProductId')} removido"
        if result.get("clearedOptionIds"):
            ids = ", ".join(str(i) for i in result.get("clearedOptionIds") or [])
            msg += f" · opção(ões) desvinculada(s): {ids}"
        mtype = "success"
    except Exception as e:
        msg = f"Falha ao desvincular {codigo}: {_friendly_error(e, [])}"
        mtype = "error"
    return redirect(
        url_for("products_page", q=q, message=msg, message_type=mtype)
    )


@app.route("/products/attach-variation", methods=["POST"])
def products_attach_variation():
    """Anexa codigo UniPlus como variação de um produto pai no Compuchat."""
    import product_sync

    codigo = (request.form.get("codigo") or "").strip()
    q = (request.form.get("q") or "").strip()
    try:
        parent_id = int(request.form.get("parentProductId") or 0)
    except (TypeError, ValueError):
        parent_id = 0
    variation_name = (request.form.get("variationName") or "Tamanho").strip()
    option_label = (request.form.get("optionLabel") or "").strip()
    parent_grupo = (request.form.get("parentGrupo") or "").strip()
    preco_raw = (request.form.get("preco") or "").strip()
    preco = None
    if preco_raw:
        try:
            preco = float(preco_raw)
        except ValueError:
            preco = None

    parents_for_errors = []
    try:
        parents_for_errors = product_sync.list_compuchat_products(limit=1000)
    except Exception:
        pass

    try:
        if not codigo or parent_id <= 0:
            raise RuntimeError("Informe o codigo UniPlus e o produto pai Compuchat")
        result = product_sync.attach_variation_to_parent(
            codigo=codigo,
            parent_product_id=parent_id,
            variation_name=variation_name,
            option_label=option_label,
            preco=preco,
            parent_grupo=parent_grupo,
        )
        removed = result.get("removedProductId")
        msg = (
            f"Variação anexada: {codigo} → produto #{result.get('parentProductId')} "
            f"(option #{result.get('optionId')})"
        )
        if removed:
            msg += f" · removido standalone #{removed}"
        # Mantém sync ON para o codigo atualizar preço na option
        try:
            product_sync.enable_product(codigo, enabled=True)
        except Exception:
            pass
        mtype = "success"
    except Exception as e:
        msg = f"Falha ao anexar variação {codigo}: {_friendly_error(e, parents_for_errors)}"
        mtype = "error"
    return redirect(
        url_for("products_page", q=q, message=msg, message_type=mtype)
    )


@app.route("/products/transform-cluster", methods=["POST"])
def products_transform_cluster():
    """Cria um produto pai novo e anexa cada codigo do cluster sugerido como
    variação (tamanho) dele — usado pelo card "Parecem ser tamanhos do mesmo
    produto" da seção Pendentes."""
    import product_sync

    q = (request.form.get("q") or "").strip()
    nome = (request.form.get("nome") or "").strip()
    grupo = (request.form.get("grupo") or "").strip()
    codigos = [c.strip() for c in request.form.getlist("codigos") if c.strip()]
    labels = request.form.getlist("labels")
    precos_raw = request.form.getlist("precos")

    try:
        if not nome:
            raise RuntimeError("Informe o nome do produto")
        if len(codigos) < 2:
            raise RuntimeError("Selecione ao menos 2 códigos para formar as variações")

        def preco_at(i: int) -> Optional[float]:
            if i >= len(precos_raw) or not str(precos_raw[i]).strip():
                return None
            try:
                return float(precos_raw[i])
            except ValueError:
                return None

        base_preco = preco_at(0) or 0.0
        created = product_sync.create_parent_product(
            nome=nome, grupo=grupo, preco=base_preco
        )
        parent_id = created.get("productId")

        ok = 0
        fail = 0
        errors: List[str] = []
        for i, codigo in enumerate(codigos):
            label = labels[i].strip() if i < len(labels) and labels[i] else ""
            try:
                product_sync.attach_variation_to_parent(
                    codigo=codigo,
                    parent_product_id=parent_id,
                    variation_name="Tamanho",
                    option_label=label,
                    preco=preco_at(i),
                    parent_grupo=grupo,
                )
                try:
                    product_sync.enable_product(codigo, enabled=True)
                except Exception:
                    pass
                ok += 1
            except Exception as e:
                fail += 1
                if len(errors) < 5:
                    errors.append(f"{codigo}: {_friendly_error(e, [])}")

        msg = f"Produto “{nome}” criado (#{parent_id}) com {ok} variação(ões)"
        if fail:
            msg += f", {fail} falha(s)"
            if errors:
                msg += " — " + "; ".join(errors)
            mtype = "error"
        else:
            mtype = "success"
    except Exception as e:
        msg = f"Falha ao transformar cluster “{nome}”: {_friendly_error(e, [])}"
        mtype = "error"
    return redirect(
        url_for("products_page", q=q, message=msg, message_type=mtype)
    )


@app.route("/products/toggle", methods=["POST"])
def products_toggle():
    import product_sync

    codigo = (request.form.get("codigo") or "").strip()
    enabled = request.form.get("enabled") == "1"
    q = (request.form.get("q") or "").strip()
    try:
        result = product_sync.enable_product(codigo, enabled=enabled)
        if enabled and not result.get("ok"):
            msg = f"Marcado, mas sync falhou: {result.get('error')}"
            mtype = "error"
        elif enabled:
            action = (result.get("result") or {}).get("action") or "ok"
            msg = f"Sync automático ON ({codigo}) — {action}"
            mtype = "success"
        else:
            msg = f"Sync automático OFF ({codigo})"
            mtype = "success"
    except Exception as e:
        msg = str(e)
        mtype = "error"
    return redirect(
        url_for("products_page", q=q, message=msg, message_type=mtype)
    )


@app.route("/products/sync-now", methods=["POST"])
def products_sync_now():
    import product_sync

    codigo = (request.form.get("codigo") or "").strip()
    q = (request.form.get("q") or "").strip()
    try:
        result = product_sync.sync_one(codigo, force=True)
        if result.get("ok"):
            action = (result.get("result") or {}).get("action") or "ok"
            msg = f"Sincronizado {codigo}: {action}"
            mtype = "success"
        else:
            msg = f"Falha {codigo}: {result.get('error')}"
            mtype = "error"
    except Exception as e:
        msg = str(e)
        mtype = "error"
    return redirect(
        url_for("products_page", q=q, message=msg, message_type=mtype)
    )


def _bulk_result_message(action_label: str, result: dict) -> tuple:
    synced = int(result.get("synced") or 0)
    failed = int(result.get("failed") or 0)
    total = int(result.get("total") or result.get("enabled") or 0)
    custom = (result.get("message") or "").strip()
    if custom and synced == 0 and failed == 0:
        return custom, "success"
    errors = result.get("errors") or []
    detail = f" — {errors[0]}" if errors else ""
    if failed:
        return (
            f"{action_label}: {synced}/{total} ok, {failed} falha(s){detail}",
            "error",
        )
    return f"{action_label}: {synced}/{total} ok{detail}", "success"


@app.route("/products/enable-all", methods=["POST"])
def products_enable_all():
    """Adiciona (sync ON) + upsert de todos os produtos listados (respeita filtro q)."""
    import product_sync

    q = (request.form.get("q") or "").strip()
    try:
        result = product_sync.enable_all_products(q=q, limit=2000)
        if result.get("message") and not int(result.get("synced") or 0):
            msg, mtype = result["message"], "success"
        else:
            enabled = int(result.get("enabled") or 0)
            synced = int(result.get("synced") or 0)
            failed = int(result.get("failed") or 0)
            msg = f"Adicionar todos: {enabled} marcados, {synced} sincronizados"
            if failed:
                msg += f", {failed} falha(s)"
                errors = result.get("errors") or []
                if errors:
                    msg += f" — {errors[0]}"
                mtype = "error"
            else:
                mtype = "success"
    except Exception as e:
        msg = str(e)
        mtype = "error"
    return redirect(
        url_for("products_page", q=q, message=msg, message_type=mtype)
    )


@app.route("/products/sync-all", methods=["POST"])
def products_sync_all():
    """Força sync de todos os produtos com sync automático ON (respeita filtro q)."""
    import product_sync

    q = (request.form.get("q") or "").strip()
    try:
        result = product_sync.sync_all_products(q=q, limit=2000)
        msg, mtype = _bulk_result_message("Sincronizar todos", result)
    except Exception as e:
        msg = str(e)
        mtype = "error"
    return redirect(
        url_for("products_page", q=q, message=msg, message_type=mtype)
    )


@app.route("/api/logs")
def api_logs():
    """API JSON para auto-refresh da tela de logs."""
    status_filter = (request.args.get("status") or "all").strip().lower()
    kind_filter = (request.args.get("kind") or "all").strip().lower()
    q = (request.args.get("q") or "").strip()
    try:
        limit = int(request.args.get("limit") or 50)
    except (TypeError, ValueError):
        limit = 50
    limit = max(1, min(limit, 500))
    return jsonify(
        {
            "logs": db.get_print_logs(
                limit=limit, status=status_filter, q=q, kind=kind_filter
            ),
            "stats": db.get_print_log_stats(),
        }
    )


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
            start_product_sync_thread()
            run_flask()
    else:
        print("=" * 50)
        print("Print Agent - WebSocket")
        print("=" * 50)
        print("Interface: http://localhost:5000/")
        print("Produtos:  http://localhost:5000/products")
        print("Logs:      http://localhost:5000/logs")
        print("Status:    http://localhost:5000/status")
        print("Health:    http://localhost:5000/health")
        print("=" * 50)
        start_agent_thread()
        start_product_sync_thread()
        run_flask()
