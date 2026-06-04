"""
Luminatec · Odoo — Funciones helper para la API de Odoo (XML-RPC)
Todas las funciones @st.cache_data y helpers de creación/búsqueda van aquí.
"""
import xmlrpc.client
import base64
import re
import time
import logging
import streamlit as st
from io import BytesIO
from datetime import datetime as _dt_now
from zoneinfo import ZoneInfo
import pandas as pd
import config as _cfg

_logger = logging.getLogger("lumidoo.odoo_client")

_AR_TZ = ZoneInfo("America/Argentina/Buenos_Aires")


def clean_str(s: object) -> str:
    """Strip whitespace de un valor, devuelve str vacío para None/False."""
    if s is None or s is False:
        return ""
    return str(s).strip()


# ═══════════════════════════════════════════════════════════════════════════
# ERROR HANDLING CENTRALIZADO
# ═══════════════════════════════════════════════════════════════════════════

class OdooError(Exception):
    """Error controlado de Odoo XML-RPC."""
    pass


def odoo_call(models, uid, api_key, model, method, args, kw=None,
              retries: int = 2, retry_delay: float = 1.5):
    """
    Wrapper seguro para models.execute_kw con:
      - retry automático ante errores de red/timeout (no ante errores de negocio)
      - logging estructurado
      - excepción tipada OdooError para manejo uniforme en la UI

    Uso:
        result = odoo_call(models, uid, api_key, "account.move", "create", [vals])

    Para errores de negocio (campo requerido, acceso denegado, etc.)
    Odoo devuelve Fault — se propagan como OdooError con mensaje legible.
    """
    kw = kw or {}
    last_exc = None

    for attempt in range(retries + 1):
        try:
            return models.execute_kw(_cfg.ODOO_DB, uid, api_key, model, method, args, kw)
        except xmlrpc.client.Fault as e:
            # Error de negocio de Odoo — no reintentar, el mensaje ya es legible
            msg = _clean_odoo_fault(e.faultString)
            _logger.error("OdooFault %s.%s: %s", model, method, msg)
            raise OdooError(msg) from e
        except (ConnectionError, TimeoutError, OSError) as e:
            last_exc = e
            _logger.warning("OdooNetwork attempt %d/%d %s.%s: %s",
                            attempt + 1, retries + 1, model, method, e)
            if attempt < retries:
                time.sleep(retry_delay * (2 ** attempt))
        except Exception as e:
            last_exc = e
            _logger.error("OdooUnexpected %s.%s: %s", model, method, e)
            if attempt < retries:
                time.sleep(retry_delay * (2 ** attempt))

    raise OdooError(f"No se pudo conectar a Odoo tras {retries + 1} intentos: {last_exc}") from last_exc


def _clean_odoo_fault(fault_str: str) -> str:
    """Extrae el mensaje legible de un Fault string de Odoo (quita traceback Python)."""
    # Los Fault strings de Odoo tienen la forma:
    # "Traceback (most recent call last):\n  ...\nUserError: El mensaje real"
    for prefix in ("UserError: ", "ValidationError: ", "AccessError: ",
                   "MissingError: ", "AccessDenied: "):
        if prefix in fault_str:
            return fault_str.split(prefix, 1)[-1].strip().split("\n")[0]
    # Si no matchea ningún prefijo conocido, tomar la última línea no vacía
    lines = [l.strip() for l in fault_str.splitlines() if l.strip()]
    return lines[-1] if lines else fault_str


def _append_session_log(nivel: str, context: str, message: str) -> None:
    """Agrega una entrada al log de sesión visible en el tab Historial."""
    if "error_log" not in st.session_state:
        st.session_state["error_log"] = []
    st.session_state["error_log"].append({
        "ts":      _dt_now.now(_AR_TZ).isoformat(timespec="seconds"),
        "nivel":   nivel,
        "context": context,
        "error":   message,
    })


def show_odoo_error(e: Exception, context: str = "") -> None:
    """
    Muestra un error de Odoo en la UI de Streamlit de forma consistente.
    Loguea el error completo; muestra al usuario solo el mensaje limpio.
    También escribe en el log de sesión visible en el tab Historial.

    Uso en un tab:
        try:
            result = odoo_call(models, uid, api_key, ...)
        except OdooError as e:
            show_odoo_error(e, "crear factura")
    """
    prefix = f"Error al {context}: " if context else "Error: "
    if isinstance(e, OdooError):
        st.error(f"❌ {prefix}{e}")
        _append_session_log("ERROR", context, str(e))
    else:
        _logger.exception("Unexpected error: %s", e)
        st.error(f"❌ {prefix}Error inesperado — revisá el log de sesión.")
        _append_session_log("ERROR", context, f"{type(e).__name__}: {e}")


def show_odoo_warning(message: str, context: str = "") -> None:
    """Muestra un warning en UI y lo registra en el log de sesión."""
    prefix = f"Advertencia al {context}: " if context else ""
    st.warning(f"⚠️ {prefix}{message}")
    _logger.warning("%s%s", prefix, message)
    _append_session_log("WARNING", context, message)


def get_odoo_error_log() -> list:
    """Devuelve el log de errores/warnings de la sesión actual (para mostrar en Historial)."""
    return st.session_state.get("error_log", [])


# ── Detección de documentos duplicados ───────────────────────────────────────

def _file_hash(file_bytes: bytes) -> str:
    """SHA-256 de los bytes del archivo, truncado a 16 chars para usar como key."""
    import hashlib
    return hashlib.sha256(file_bytes).hexdigest()[:16]



def check_duplicate_vendor_bill(models_url, uid, api_key, partner_id, document_number,
                                move_type='in_invoice'):
    """Verifica si ya existe una factura/NC de proveedor con ese número + proveedor en Odoo.
    Retorna (True, move_name, move_id) si existe, (False, None, None) si no."""
    if not partner_id or not document_number:
        return False, None, None
    try:
        m = xmlrpc.client.ServerProxy(models_url, allow_none=True)
        _doc_search = str(document_number).strip()
        rows = m.execute_kw(
            _cfg.ODOO_DB, uid, api_key, "account.move", "search_read",
            [[("move_type", "=", move_type),
              ("l10n_latam_document_number", "=", _doc_search),
              ("partner_id",                "=", partner_id),
              ("state",                     "=", "posted")]],
            {"fields": ["id", "name", "l10n_latam_document_number"], "limit": 5})
        for row in rows:
            # Verificar del lado Python: Odoo puede ignorar el filtro
            # sobre campos computados en algunas versiones
            found_doc = (row.get("l10n_latam_document_number") or "").strip()
            if found_doc == _doc_search:
                return True, row["name"], row["id"]
        return False, None, None
    except Exception:
        return False, None, None


def check_duplicate_sale_order(models_url, uid, api_key, partner_id, client_order_ref):
    """Verifica si ya existe un pedido con ese N° OC + cliente en Odoo.
    Retorna (True, order_name, order_id) si existe, (False, None, None) si no."""
    if not partner_id or not client_order_ref:
        return False, None, None
    try:
        m = xmlrpc.client.ServerProxy(models_url, allow_none=True)
        rows = m.execute_kw(
            _cfg.ODOO_DB, uid, api_key, "sale.order", "search_read",
            [[("client_order_ref", "=", str(client_order_ref).strip()),
              ("partner_id",       "=", partner_id),
              ("state",            "not in", ["cancel"])]],
            {"fields": ["id", "name"], "limit": 1})
        if rows:
            return True, rows[0]["name"], rows[0]["id"]
        return False, None, None
    except Exception:
        return False, None, None


def check_duplicate_cheque(models_url, uid, api_key, nro, issuer_vat):
    """Verifica si ya existe un cheque con ese número + CUIT emisor en pagos confirmados.
    Retorna (True, payment_name, group_id) si existe, (False, None, None) si no."""
    if not nro or not issuer_vat:
        return False, None, None
    try:
        m = xmlrpc.client.ServerProxy(models_url, allow_none=True)
        # Buscar en l10n_latam_new_check (cheques de terceros en Odoo AR)
        rows = m.execute_kw(
            _cfg.ODOO_DB, uid, api_key, "l10n_latam.check", "search_read",
            [[("name",       "=",  str(nro).strip()),
              ("issuer_vat", "=",  str(issuer_vat).replace("-","").strip()),
              ("state",      "!=", "cancelled")]],
            {"fields": ["id", "name", "payment_id"], "limit": 1})
        if rows:
            pname = ""
            try:
                pay = m.execute_kw(_cfg.ODOO_DB, uid, api_key,
                    "account.payment", "read",
                    [[rows[0]["payment_id"][0]]],
                    {"fields": ["name", "payment_group_id"]})
                pname = pay[0].get("name","") if pay else ""
            except Exception:
                pass
            return True, pname or f"Cheque #{nro}", None
        return False, None, None
    except Exception:
        return False, None, None

def check_duplicate_file(file_bytes: bytes, filename: str) -> tuple[bool, dict]:
    """Verifica si el archivo ya fue procesado en esta sesion.

    Retorna (is_dup, entry) donde entry es el dict del procesamiento original,
    o (False, {}) si es nuevo.
    """
    h = _file_hash(file_bytes)
    processed = st.session_state.get("processed_files", {})
    if h in processed:
        return True, processed[h]
    return False, {}


def register_processed_file(file_bytes: bytes, filename: str,
                             tipo: str, resultado: str = "") -> None:
    """Registra un archivo como procesado en la sesion actual.

    Llamar despues de procesar exitosamente un documento para que
    futuros uploads del mismo archivo sean detectados como duplicados.
    """
    if "processed_files" not in st.session_state:
        st.session_state["processed_files"] = {}
    h = _file_hash(file_bytes)
    st.session_state["processed_files"][h] = {
        "filename":  filename,
        "tipo":      tipo,
        "resultado": resultado,
        "hora":      _dt_now.now(_AR_TZ).strftime("%H:%M"),
    }


def get_models_proxy():
    """ServerProxy para account.move, etc. Es stateless — uid y password van por llamada."""
    return xmlrpc.client.ServerProxy(f"{_cfg.ODOO_URL}/xmlrpc/2/object", allow_none=True)

def odoo_authenticate(email: str, password: str):
    """
    Autentica al usuario contra Odoo XML-RPC.
    Devuelve (uid, "") si OK, (None, mensaje_error) si falla.
    """
    try:
        common = xmlrpc.client.ServerProxy(f"{_cfg.ODOO_URL}/xmlrpc/2/common", allow_none=True)
        uid = common.authenticate(_cfg.ODOO_DB, email.strip().lower(), password, {})
        if uid:
            return uid, ""
        return None, "Email o contraseña incorrectos."
    except Exception as e:
        return None, f"No se pudo conectar a Odoo: {e}"

def verify_user(email, password):
    """
    Verifica email + contraseña.
    Prioridad:
      1. Odoo JSON-RPC /web/session/authenticate (contraseña web normal, sin API key)
      2. APP_USERS  = "email1:clave1,email2:clave2"  (fallback manual en secrets)
      3. APP_PASSWORD = "clave_compartida"            (fallback global)
    """
    import hashlib, urllib.request, json as _json
    email = email.strip().lower()

    # ── 1. Autenticación via Odoo JSON-RPC (misma clave que el navegador) ────
    try:
        _payload = _json.dumps({
            "jsonrpc": "2.0", "method": "call", "id": 1,
            "params": {"db": _cfg.ODOO_DB, "login": email, "password": password}
        }).encode()
        _req = urllib.request.Request(
            f"{_cfg.ODOO_URL}/web/session/authenticate",
            data=_payload,
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        with urllib.request.urlopen(_req, timeout=8) as _resp:
            _data = _json.loads(_resp.read())
        _uid = (_data.get("result") or {}).get("uid")
        if _uid:
            return True, ""
    except Exception:
        pass  # si Odoo no responde, cae al fallback

    # ── 2. APP_USERS en secrets (fallback) ───────────────────────────────────
    pw_hash = hashlib.sha256(password.encode()).hexdigest()
    app_users_raw = st.secrets.get("APP_USERS", "")
    if app_users_raw:
        user_map = {}
        for entry in app_users_raw.split(","):
            parts = entry.strip().split(":", 1)
            if len(parts) == 2:
                user_map[parts[0].strip().lower()] = parts[1].strip()
        if email in user_map:
            expected = user_map[email]
            if password == expected or pw_hash == expected:
                return True, ""
            return False, "Contraseña incorrecta."
        return False, "Email no autorizado o contraseña incorrecta."

    # ── 3. APP_PASSWORD (clave global compartida) ────────────────────────────
    app_password = st.secrets.get("APP_PASSWORD", "")
    if app_password:
        if password == app_password or pw_hash == app_password:
            return True, ""
        return False, "Contraseña incorrecta."

    return False, "Contraseña incorrecta."

def call(models, uid, api_key, model, method, args, kw=None):
    """
    Helper de bajo nivel. Delega a odoo_call() para retry automático y
    manejo uniforme de errores. Lanza OdooError ante fallos de Odoo.
    """
    return odoo_call(models, uid, api_key, model, method, args, kw)

@st.cache_data(ttl=300, show_spinner=False)
def search_partners(models_url, uid, api_key, name, limit=8):
    m = xmlrpc.client.ServerProxy(models_url, allow_none=True)
    rows = m.execute_kw(_cfg.ODOO_DB, uid, api_key, "res.partner", "search_read",
        [[("name", "ilike", name), ("active", "=", True)]],
        {"fields": ["id", "name"], "limit": limit, "order": "name asc"})
    return [(r["id"], r["name"]) for r in rows]

@st.cache_data(ttl=600, show_spinner=False)
def get_all_accounts(models_url, uid, api_key):
    """Carga todas las cuentas contables activas de Odoo (cacheado 10 min)."""
    try:
        m = xmlrpc.client.ServerProxy(models_url, allow_none=True)
        rows = m.execute_kw(_cfg.ODOO_DB, uid, api_key, "account.account", "search_read",
            [[("deprecated", "=", False)]],
            {"fields": ["id", "code", "name"], "order": "code asc"})
        return [(r["id"], f"{r['code']}  {r['name']}") for r in rows]
    except Exception:
        return []

@st.cache_data(ttl=120, show_spinner=False)
def get_partner_default_account(models_url, uid, api_key, partner_id):
    """Devuelve (account_id, 'CODE  Name') de la cuenta de gasto por defecto del proveedor.
    Estrategia:
      1. product.supplierinfo donde partner_id -> product_tmpl_id
         -> property_account_expense_id del product.template (pestaña Contabilidad en Odoo)
      2. Fallback: account de la categoria del producto (product.category)
    Solo usa cuentas configuradas en el producto — NO recurre a facturas históricas.
    Retorna None si no se puede determinar."""
    try:
        m = xmlrpc.client.ServerProxy(models_url, allow_none=True)

        # --- Estrategia 1: producto del proveedor → cuenta de gasto del producto ---
        sinfo = m.execute_kw(_cfg.ODOO_DB, uid, api_key, "product.supplierinfo", "search_read",
            [[("partner_id", "=", partner_id)]],
            {"fields": ["product_tmpl_id"], "limit": 5, "order": "id asc"})

        for si in (sinfo or []):
            tmpl_id = (si.get("product_tmpl_id") or [None])[0]
            if not tmpl_id:
                continue
            # Leer cuenta de gasto Y categoría (para fallback)
            tmpls = m.execute_kw(_cfg.ODOO_DB, uid, api_key, "product.template", "read",
                [[tmpl_id]],
                {"fields": ["property_account_expense_id", "categ_id"]})
            if not tmpls:
                continue
            tmpl = tmpls[0]

            # property_account_expense_id (pestaña Contabilidad del producto)
            acct = tmpl.get("property_account_expense_id")
            if acct and isinstance(acct, (list, tuple)) and acct[0]:
                accts = m.execute_kw(_cfg.ODOO_DB, uid, api_key, "account.account", "read",
                    [[acct[0]]], {"fields": ["code", "name", "deprecated"]})
                if accts and not accts[0].get("deprecated"):
                    return (accts[0]["id"], f"{accts[0]['code']}  {accts[0]['name']}")

            # Fallback: cuenta de gasto de la categoria del producto
            categ = tmpl.get("categ_id")
            categ_id = (categ[0] if isinstance(categ, (list, tuple)) else categ) if categ else None
            if categ_id:
                cats = m.execute_kw(_cfg.ODOO_DB, uid, api_key, "product.category", "read",
                    [[categ_id]],
                    {"fields": ["property_account_expense_categ_id"]})
                if cats:
                    categ_acct = cats[0].get("property_account_expense_categ_id")
                    if categ_acct and isinstance(categ_acct, (list, tuple)) and categ_acct[0]:
                        accts = m.execute_kw(_cfg.ODOO_DB, uid, api_key, "account.account", "read",
                            [[categ_acct[0]]], {"fields": ["code", "name", "deprecated"]})
                        if accts and not accts[0].get("deprecated"):
                            return (accts[0]["id"], f"{accts[0]['code']}  {accts[0]['name']}")
    except Exception:
        pass
    return None



@st.cache_data(ttl=300, show_spinner=False)
def get_expense_products(models_url, uid, api_key):
    """Carga variantes de productos activos y aptos para compra (product.product → IDs válidos para lineas de factura)."""
    try:
        m = xmlrpc.client.ServerProxy(models_url, allow_none=True)
        rows = m.execute_kw(_cfg.ODOO_DB, uid, api_key, "product.product", "search_read",
            [[("active", "=", True), ("purchase_ok", "=", True), ("type", "=", "service")]],
            {"fields": ["id", "name", "default_code"], "order": "name asc", "limit": 500})
        result = []
        for r in rows:
            code = r.get("default_code") or ""
            label = f"[{code}] {r['name']}" if code else r["name"]
            result.append((r["id"], label))
        return result
    except Exception:
        return []

@st.cache_data(ttl=120, show_spinner=False)
def get_partner_default_product(models_url, uid, api_key, partner_id):
    """Devuelve (product_product_id, label) del primer producto configurado para el proveedor."""
    try:
        m = xmlrpc.client.ServerProxy(models_url, allow_none=True)
        # Intentar campo product_id (variante específica) primero
        sinfo = m.execute_kw(_cfg.ODOO_DB, uid, api_key, "product.supplierinfo", "search_read",
            [[("partner_id", "=", partner_id)]],
            {"fields": ["product_tmpl_id", "product_id"], "limit": 1, "order": "id asc"})
        if sinfo:
            row = sinfo[0]
            # product_id en supplierinfo es Many2one a product.product (puede ser False)
            prod_var = row.get("product_id")
            if prod_var and isinstance(prod_var, (list, tuple)) and prod_var[0]:
                return (prod_var[0], prod_var[1])
            # Sino buscar primera variante activa del template
            tmpl = row.get("product_tmpl_id")
            if tmpl and isinstance(tmpl, (list, tuple)) and tmpl[0]:
                variants = m.execute_kw(_cfg.ODOO_DB, uid, api_key, "product.product", "search_read",
                    [[("product_tmpl_id", "=", tmpl[0]), ("active", "=", True)]],
                    {"fields": ["id", "name"], "limit": 1})
                if variants:
                    return (variants[0]["id"], variants[0]["name"])
    except Exception:
        pass
    return None

@st.cache_data(ttl=300, show_spinner=False)
def get_analytic_accounts(models_url, uid, api_key):
    """Carga cuentas analíticas activas (Centros de Costo) de Odoo."""
    try:
        m = xmlrpc.client.ServerProxy(models_url, allow_none=True)
        rows = m.execute_kw(_cfg.ODOO_DB, uid, api_key, "account.analytic.account", "search_read",
            [[("active", "=", True)]],
            {"fields": ["id", "name", "code"], "order": "name asc"})
        result = []
        for r in rows:
            label = f"{r['code']}  {r['name']}" if r.get("code") else r["name"]
            result.append((r["id"], label))
        return result
    except Exception:
        return []

@st.cache_data(ttl=3600, show_spinner=False)
def get_currency_id(models_url, uid, api_key, name):
    """Retorna el ID de la moneda por nombre (ej: 'USD', 'ARS')."""
    try:
        m = xmlrpc.client.ServerProxy(models_url, allow_none=True)
        rows = m.execute_kw(_cfg.ODOO_DB, uid, api_key, "res.currency", "search_read",
            [[("name", "=", name)]],
            {"fields": ["id", "name"], "limit": 1})
        return rows[0]["id"] if rows else None
    except Exception:
        return None

@st.cache_data(ttl=120, show_spinner=False)
def search_purchase_orders(models_url, uid, api_key, query):
    m = xmlrpc.client.ServerProxy(models_url, allow_none=True)
    rows = m.execute_kw(_cfg.ODOO_DB, uid, api_key, "purchase.order", "search_read",
        [[("name", "ilike", query), ("state", "in", ["purchase", "done"])]],
        {"fields": ["id", "name", "partner_id", "date_order", "amount_total"], "limit": 10})
    return rows

@st.cache_data(ttl=60, show_spinner=False)
def get_pickings_for_po(models_url, uid, api_key, po_id):
    m = xmlrpc.client.ServerProxy(models_url, allow_none=True)
    rows = m.execute_kw(_cfg.ODOO_DB, uid, api_key, "stock.picking", "search_read",
        [[("purchase_id", "=", po_id), ("state", "!=", "cancel")]],
        {"fields": ["id", "name", "state", "location_dest_id"], "limit": 10})
    return rows

@st.cache_data(ttl=60, show_spinner=False)
def get_bills_for_carpeta(models_url, uid, api_key, carpeta_ref):
    m = xmlrpc.client.ServerProxy(models_url, allow_none=True)
    rows = m.execute_kw(_cfg.ODOO_DB, uid, api_key, "account.move", "search_read",
        [[("move_type", "=", "in_invoice"), ("ref", "ilike", carpeta_ref), ("state", "!=", "cancel")]],
        {"fields": ["id", "name", "partner_id", "invoice_date", "amount_total", "state", "journal_id"], "limit": 50})
    return rows

def attach_file(models, uid, api_key, res_model, res_id, filename, file_bytes, mimetype):
    call(models, uid, api_key, "ir.attachment", "create", [{
        "name": filename, "res_model": res_model, "res_id": res_id,
        "datas": base64.b64encode(file_bytes).decode(), "mimetype": mimetype,
    }])


def create_purchase_order_petdur(models, uid, api_key, carpeta_id,
                                  currency_id=None, tc_usd=None,
                                  filename=None, file_bytes=None, mimetype=None,
                                  lineas=None):
    """
    Crea OC PETDUR en draft (purchase.order).
    currency_id: pasar SOLO si se tiene el ID real de USD (no un fallback arbitrario).
    tc_usd: cotización ARS/USD del día (x_studio_cotizacion_dolar) — requerido por Odoo.
    lineas: lista de {"descripcion", "cantidad", "precio_unit"} del documento PETDUR.
    Retorna el ID del purchase.order.
    """
    import datetime as _dt_po
    po_vals = {
        "partner_id":  49328,       # PETDUR CORPORATION S.A.
        "partner_ref": carpeta_id,  # clave de búsqueda en load_carpeta_full
    }
    if currency_id:
        po_vals["currency_id"] = currency_id
    if tc_usd and float(tc_usd) > 0:
        po_vals["x_studio_cotizacion_dolar"] = float(tc_usd)
    po_id = call(models, uid, api_key, "purchase.order", "create", [po_vals])

    # Agregar líneas al pedido si vienen del documento PETDUR
    if lineas:
        _today_po = _dt_po.date.today().isoformat()
        for ln in lineas:
            try:
                desc  = str(ln.get("descripcion") or "").strip() or carpeta_id
                qty   = float(ln.get("cantidad")   or 1)
                price = float(ln.get("precio_unit") or 0)
                call(models, uid, api_key, "purchase.order.line", "create", [{
                    "order_id":     po_id,
                    "name":         desc,
                    "product_qty":  qty,
                    "price_unit":   price,
                    "date_planned": _today_po,
                    "product_uom":  1,          # UdM = Unidad (ID 1 en Odoo estándar)
                }])
            except Exception:
                pass  # si falla una línea, seguir con las demás

    if filename and file_bytes:
        try:
            attach_file(models, uid, api_key, "purchase.order", po_id, filename, file_bytes, mimetype)
        except Exception:
            pass
    return po_id


# ── Mapeo partner_id → tipo para facturas de importación ────────────────────
PARTNER_TO_TIPO = {
    49328: {"tipo": "petdur", "etapa": "1",  "label": "PETDUR"},
    9:     {"tipo": "di_afip","etapa": "2",  "label": "AFIP"},
    48825: {"tipo": "nac",    "etapa": "2a", "label": "TRICE"},
    48828: {"tipo": "nac",    "etapa": "2a", "label": "Terminal 4"},
    48826: {"tipo": "nac",    "etapa": "2a", "label": "Mundo Comex"},
    48827: {"tipo": "nac",    "etapa": "2a", "label": "SENASA"},
}

# ── CUIT → partner (sin guiones, 11 dígitos) ────────────────────────────────
CUIT_TO_PARTNER = {
    "30711100314": {"tipo":"nac",    "label":"Bill TRICE Transport (Etapa 2a)", "partner_id":48825,"journal_id":10,"doc_type":None},
    "30717845419": {"tipo":"nac",    "label":"Bill Mundo Comex (Etapa 2a)",     "partner_id":48826,"journal_id":10,"doc_type":None},
    "30678196165": {"tipo":"nac",    "label":"Bill Terminal 4 SA (Etapa 2a)",   "partner_id":48828,"journal_id":10,"doc_type":None},
    "33546700939": {"tipo":"nac",    "label":"Bill SENASA (Etapa 2a)",          "partner_id":48827,"journal_id":10,"doc_type":None},
}

def _parse_odoo_rate(r):
    """
    Extrae el TC ARS/USD de un registro res.currency.rate.
    Odoo almacena la tasa de distintas formas según versión y localización:
      - inverse_company_rate: ARS por 1 USD (ej: 1417.0)  ← Odoo 16+
      - company_rate:         ARS por 1 USD (ej: 1417.0)  ← alternativo
      - rate:                 USD por 1 ARS (ej: 0.000706) ← inverso
    Siempre validamos que el resultado sea > 100 para descartar defaults de 1.0.
    """
    for field in ("inverse_company_rate", "company_rate"):
        v = r.get(field)
        if v and v is not False:
            fv = float(v)
            if fv > 100:
                return fv
            if 0 < fv < 0.01:          # almacenado como su propio inverso
                return 1.0 / fv
    # rate suele ser 1/TC (USD por ARS)
    rate = r.get("rate")
    if rate and rate is not False:
        fv = float(rate)
        if fv > 100:                    # ya está en ARS/USD directamente
            return fv
        if 0 < fv < 0.01:              # correcto: 1/TC ≈ 0.000706
            return 1.0 / fv
    return None

@st.cache_data(ttl=300, show_spinner=False)
def get_usd_rate_odoo(models_url, uid, api_key, date_str):
    """TC ARS/USD del día o el más reciente anterior en Odoo."""
    try:
        m = xmlrpc.client.ServerProxy(models_url, allow_none=True)
        rows = m.execute_kw(_cfg.ODOO_DB, uid, api_key, "res.currency.rate", "search_read",
            [[("currency_id.name", "=", "USD"), ("name", "<=", date_str)]],
            {"fields": ["name", "rate", "inverse_company_rate", "company_rate"],
             "limit": 1, "order": "name desc"})
        if rows:
            tc = _parse_odoo_rate(rows[0])
            if tc:
                return tc, rows[0]["name"]
    except Exception:
        pass
    return None, None

@st.cache_data(ttl=120, show_spinner=False)
def get_po_lines(models_url, uid, api_key, po_id):
    """Líneas de una OC con productos, cantidades y precios."""
    try:
        m = xmlrpc.client.ServerProxy(models_url, allow_none=True)
        return m.execute_kw(_cfg.ODOO_DB, uid, api_key, "purchase.order.line", "search_read",
            [[("order_id", "=", po_id), ("state", "!=", "cancel")]],
            {"fields": ["product_id", "product_qty", "price_unit", "price_subtotal", "name"]})
    except Exception:
        return []

@st.cache_data(ttl=120, show_spinner=False)
def load_carpeta_full(models_url, uid, api_key, carpeta_id):
    """
    Carga bills, OC, pickings y detecta etapas de una carpeta desde Odoo.
    Busca la OC por el campo partner_ref (Referencia de proveedor).
    """
    result = {"bills": [], "po": None, "pickings": [], "lc_ids": [],
              "stages": {}, "tc_oc": None, "error": None}
    try:
        m = xmlrpc.client.ServerProxy(models_url, allow_none=True)

        # 1. OC por partner_ref (campo "Referencia de proveedor")
        po_fields_ext = ["id", "name", "partner_id", "amount_total", "currency_id",
                         "state", "partner_ref", "x_studio_cotizacion_dolar"]
        po_fields_safe = ["id", "name", "partner_id", "amount_total", "currency_id",
                          "state", "partner_ref"]
        # Incluir draft para OCs auto-creadas que no pudieron confirmarse
        _po_states = ["draft", "sent", "purchase", "done"]
        try:
            pos = m.execute_kw(_cfg.ODOO_DB, uid, api_key, "purchase.order", "search_read",
                [[("partner_ref", "ilike", carpeta_id), ("state", "in", _po_states)]],
                {"fields": po_fields_ext, "limit": 5})
        except Exception:
            pos = m.execute_kw(_cfg.ODOO_DB, uid, api_key, "purchase.order", "search_read",
                [[("partner_ref", "ilike", carpeta_id), ("state", "in", _po_states)]],
                {"fields": po_fields_safe, "limit": 5})
        po = pos[0] if pos else None
        result["po"] = po

        # TC desde campo custom de la OC ("Cotización dólar")
        if po:
            tc_raw = po.get("x_studio_cotizacion_dolar")
            if tc_raw and float(tc_raw or 0) > 100:
                result["tc_oc"] = float(tc_raw)

        # 2. Bills por ref (carpeta_id como substring — incluye LUMI_291A etc.)
        bill_fields = ["id", "name", "ref", "partner_id", "invoice_date", "amount_total",
                       "amount_total_signed", "currency_id", "state", "invoice_currency_rate"]
        bills = m.execute_kw(_cfg.ODOO_DB, uid, api_key, "account.move", "search_read",
            [[("move_type", "=", "in_invoice"), ("ref", "ilike", carpeta_id),
              ("state", "!=", "cancel")]],
            {"fields": bill_fields, "limit": 50})
        result["bills"] = bills

        # 3. Pickings vinculados a la OC
        pickings = []
        if po:
            pickings = m.execute_kw(_cfg.ODOO_DB, uid, api_key, "stock.picking", "search_read",
                [[("purchase_id", "=", po["id"]), ("picking_type_code", "=", "incoming")]],
                {"fields": ["id", "name", "state", "date_done", "location_dest_id"]})
        result["pickings"] = pickings

        # 4. Landed Costs
        lc_ids = []
        if pickings:
            lcs = m.execute_kw(_cfg.ODOO_DB, uid, api_key, "stock.landed.cost", "search_read",
                [[("picking_ids", "in", [p["id"] for p in pickings])]],
                {"fields": ["id", "name", "state"], "limit": 5})
            lc_ids = [lc["id"] for lc in lcs]
        result["lc_ids"] = lc_ids

        # 5. Detectar etapas automáticamente
        partner_ids = {b["partner_id"][0] for b in bills if b.get("partner_id")}
        stages = {k: False for k, *_ in _cfg.ETAPAS_DEF}
        stages["0"]  = bool(po)
        stages["1"]  = 49328 in partner_ids
        stages["2"]  = 9 in partner_ids
        stages["2a"] = bool({48825, 48826, 48827, 48828} & partner_ids)
        stages["3"]  = any(p.get("state") == "done" for p in pickings)
        stages["4"]  = bool(lc_ids)
        result["stages"] = stages

    except Exception as e:
        result["error"] = str(e)
    return result


def parse_petdur_invoice_lines(text):
    """
    Parsea líneas de producto de una factura PETDUR (e-Ticket Uruguay).
    Formato por línea: Nro COD Descripcion Cantidad u Precio Monto
    Retorna lista de dicts: descripcion, cantidad, precio_unit, monto.
    """
    def _num(s):
        if "," in s:
            parts = s.rsplit(",", 1)
            return float(parts[0].replace(".", "").replace(",", "") + "." + parts[1])
        return float(s.replace(".", ""))

    lines = []
    pattern = re.compile(
        r"^\s*\d+\s+(.+?)\s+([\d.,]+)\s+u\s+([\d.,]+)\s+([\d.,]+)\s*$",
        re.MULTILINE)
    for m in pattern.finditer(text):
        desc_raw, cant_s, precio_s, monto_s = m.groups()
        desc_raw = desc_raw.strip()
        # Eliminar prefijo duplicado: "A10 PRO A10 PRO" → "A10 PRO"
        words = desc_raw.split()
        mid = len(words) // 2
        if mid >= 1 and words[:mid] == words[mid:mid + mid]:
            desc_raw = " ".join(words[mid:])
        try:
            lines.append({
                "descripcion": desc_raw,
                "cantidad":    _num(cant_s),
                "precio_unit": _num(precio_s),
                "monto":       _num(monto_s),
            })
        except Exception:
            pass
    return lines


def get_bill_lines(models_url, uid, api_key, bill_ids):
    """Trae líneas de producto de una lista de facturas. Retorna dict {bill_id: [lineas]}."""
    if not bill_ids:
        return {}
    try:
        m = xmlrpc.client.ServerProxy(models_url, allow_none=True)
        lines = m.execute_kw(
            _cfg.ODOO_DB, uid, api_key, "account.move.line", "search_read",
            [[("move_id", "in", bill_ids),
              ("display_type", "=", False)]],
            {"fields": ["id", "move_id", "product_id", "name",
                        "quantity", "price_unit", "price_subtotal",
                        "price_total", "tax_ids"],
             "limit": 500})
        result = {}
        for ln in lines:
            mid = ln["move_id"][0] if isinstance(ln["move_id"], (list, tuple)) else ln["move_id"]
            result.setdefault(mid, []).append(ln)
        return result
    except Exception:
        return {}


def _bill_currency(b):
    """Devuelve el nombre de moneda del bill ('ARS', 'USD', etc.)"""
    c = b.get("currency_id")
    if c and isinstance(c, (list, tuple)) and len(c) > 1:
        return str(c[1])
    return "ARS"

def _bill_ars_amount(b):
    """Monto del bill en ARS usando amount_total_signed (siempre ARS en Odoo)."""
    return abs(float(b.get("amount_total_signed") or b.get("amount_total") or 0))

def _calc_cost_breakdown(po_lines, bills, tc_usd):
    """
    Calcula costo estimado por producto en USD y ARS.
    Fuentes de costo:
      - FOB: líneas de la OC (precio del proveedor extranjero, en USD)
      - Nac: bills de TRICE/T4/MundoComex/SENASA (gastos locales, en ARS)
      - AFIP: DI AFIP (derechos + IVA aduanero, en ARS)
    El landeo = (total_nac_ARS + total_afip_ARS) / TC → expresado en USD por unidad.
    Coef. de landeo = landeo_unit_USD / FOB_unit_USD × 100%
    """
    if not po_lines or not tc_usd:
        return [], {}

    # ── Identificar bills por rol ──────────────────────────────────────────
    # PETDUR: proveedor extranjero (USD). Se usa solo para refinar el TC.
    petdur   = next((b for b in bills
                     if b.get("partner_id") and b["partner_id"][0] == 49328), None)

    # Nac: gastos locales en ARS. Filtramos SOLO bills en ARS para no
    # contaminar el total con el bill USD de PETDUR si el partner_id difiere.
    nac_bils = [b for b in bills
                if b.get("partner_id")
                and b["partner_id"][0] in {48825, 48826, 48827, 48828}
                and _bill_currency(b) != "USD"]

    # AFIP: derechos de importación (ARS)
    afip     = next((b for b in bills
                     if b.get("partner_id")
                     and b["partner_id"][0] == 9
                     and _bill_currency(b) != "USD"), None)

    # Refinar TC desde invoice_currency_rate del bill PETDUR
    if petdur:
        icr = petdur.get("invoice_currency_rate")
        if icr and icr is not False:
            tc_ref = _parse_odoo_rate({"rate": icr, "inverse_company_rate": None})
            if tc_ref:
                tc_usd = tc_ref

    # ── Totales de costos de nacionalización (todos en ARS) ───────────────
    total_nac_ars  = sum(_bill_ars_amount(b) for b in nac_bils)
    total_afip_ars = _bill_ars_amount(afip) if afip else 0
    # Landeo total en USD = costos ARS convertidos al TC
    total_landeo_usd = (total_nac_ars + total_afip_ars) / tc_usd if tc_usd > 0 else 0

    # ── FOB total (en USD, desde líneas OC) ───────────────────────────────
    total_fob_usd = sum(float(l.get("price_subtotal") or 0) for l in po_lines)
    if total_fob_usd == 0:
        return [], {}

    rows = []
    for ln in po_lines:
        prod   = ln["product_id"][1] if ln.get("product_id") else ln.get("name", "?")
        qty    = float(ln.get("product_qty") or 1)
        fob_total_usd = float(ln.get("price_subtotal") or 0)   # FOB total línea en USD
        fob_pu        = fob_total_usd / qty if qty > 0 else 0  # FOB por unidad en USD

        # Proporción de esta línea sobre el total FOB → distribuir landeo
        prop            = fob_total_usd / total_fob_usd
        landeo_line_usd = total_landeo_usd * prop               # landeo asignado a esta línea
        landeo_u_usd    = landeo_line_usd / qty if qty > 0 else 0  # landeo por unidad en USD

        # Total y coeficiente
        total_u_usd = fob_pu + landeo_u_usd                    # costo total por unidad en USD
        coef_pct    = (landeo_u_usd / fob_pu * 100) if fob_pu > 0 else 0
        cost_u_ars  = total_u_usd * tc_usd                     # costo por unidad en ARS
        rows.append({
            "Producto":           prod,
            "Cant.":              int(qty) if qty == int(qty) else qty,
            "FOB unit (u$s)":     fmt_usd(fob_pu),
            "Landeo unit (u$s)":  fmt_usd(landeo_u_usd),
            "Total unit (u$s)":   fmt_usd(total_u_usd),
            "Coef. landeo":       f"+{coef_pct:.1f}%",
            "Costo unit. (ARS)":  fmt_ars(cost_u_ars),
        })

    # Desglose de gastos de nac para el expander
    nac_detail = {}
    for b in nac_bils:
        pid   = b["partner_id"][0] if b.get("partner_id") else 0
        lbl   = PARTNER_TO_TIPO.get(pid, {}).get("label", "Otro")
        amt   = _bill_ars_amount(b)
        nac_detail[lbl] = nac_detail.get(lbl, 0) + amt
    if afip:
        nac_detail["AFIP"] = total_afip_ars

    total_landeo_ars = total_nac_ars + total_afip_ars
    summary = {
        "tc_usd":              tc_usd,
        "total_fob_usd":       total_fob_usd,
        "total_fob_ars":       total_fob_usd * tc_usd,
        "total_landeo_usd":    total_landeo_usd,
        "total_landeo_ars":    total_landeo_ars,
        "grand_total_usd":     total_fob_usd + total_landeo_usd,
        "grand_total_ars":     total_fob_usd * tc_usd + total_landeo_ars,
        "coef_landeo_total":   (total_landeo_usd / total_fob_usd * 100) if total_fob_usd > 0 else 0,
        "nac_detail":          nac_detail,
        # debug
        "_nac_bils_count":     len(nac_bils),
        "_afip_found":         afip is not None,
        "_petdur_found":       petdur is not None,
    }
    return rows, summary


@st.cache_data(ttl=300, show_spinner=False)
def get_purchase_journals(models_url, uid, api_key):
    """Retorna lista de (id, name) de diarios de tipo 'purchase'."""
    try:
        common_url = models_url.replace("/object", "/common").replace("xmlrpc/2/object", "xmlrpc/2/common")
        _pj_models = xmlrpc.client.ServerProxy(models_url, allow_none=True)
        rows = _pj_models.execute_kw(
            _cfg.ODOO_DB, uid, api_key, "account.journal", "search_read",
            [[("type", "=", "purchase")]],
            {"fields": ["id", "name", "code", "company_id"], "order": "name asc", "limit": 50})
        result = []
        for r in rows:
            label = r["name"]
            code  = (r.get("code") or "").strip()
            comp  = ((r.get("company_id") or [0, ""])[1] or "").strip()
            if code:
                label = f"{r['name']} [{code}]"
                if comp:
                    label += f" — {comp}"
            elif comp:
                label = f"{r['name']} — {comp}"
            result.append((r["id"], label))
        return result
    except Exception:
        return []

def get_journal_purchase_account(models_url, uid, api_key, journal_id):
    """
    Devuelve un account_id de compras/gastos para líneas de factura de proveedor.
    Estrategias (en orden):
    1. Línea reciente de in_invoice en ese journal
    2. Línea reciente de in_invoice de cualquier journal con ese mismo partner (PETDUR=49328)
    3. default_account_id del journal
    4. Cualquier cuenta con tipo 'expense' o 'other' activa
    """
    try:
        m = xmlrpc.client.ServerProxy(models_url, allow_none=True)
        from collections import Counter

        def _most_common(rows):
            counts = Counter(l["account_id"][0] for l in rows if l.get("account_id"))
            return counts.most_common(1)[0][0] if counts else None

        # 1. Líneas recientes de facturas en el journal dado
        lines = m.execute_kw(
            _cfg.ODOO_DB, uid, api_key, "account.move.line", "search_read",
            [[("journal_id", "=", journal_id),
              ("display_type", "=", False),
              ("account_id", "!=", False)]],
            {"fields": ["account_id"], "limit": 30, "order": "id desc"})
        if lines:
            result = _most_common(lines)
            if result:
                return result

        # 2. Líneas de facturas del partner PETDUR (cualquier journal)
        lines2 = m.execute_kw(
            _cfg.ODOO_DB, uid, api_key, "account.move.line", "search_read",
            [[("partner_id", "=", 49328),
              ("display_type", "=", False),
              ("account_id", "!=", False)]],
            {"fields": ["account_id"], "limit": 30, "order": "id desc"})
        if lines2:
            result = _most_common(lines2)
            if result:
                return result

        # 3. default_account_id del journal
        jrows = m.execute_kw(
            _cfg.ODOO_DB, uid, api_key, "account.journal", "read",
            [[journal_id]], {"fields": ["default_account_id"]})
        if jrows and jrows[0].get("default_account_id"):
            return jrows[0]["default_account_id"][0]

        # 4. Cualquier cuenta de gastos activa
        acc = m.execute_kw(
            _cfg.ODOO_DB, uid, api_key, "account.account", "search_read",
            [[("account_type", "in", ["expense", "expense_direct_cost"]),
              ("deprecated", "=", False)]],
            {"fields": ["id"], "limit": 1, "order": "code asc"})
        if acc:
            return acc[0]["id"]
    except Exception:
        pass
    return None


def create_vendor_bill(models, uid, api_key, partner_id, ref, invoice_date,
                       filename, file_bytes, mimetype, journal_id=None, doc_type_id=None,
                       invoice_date_due=None, account_id=None, amount_neto=None,
                       currency_id=None, analytic_account_id=None, product_id=None,
                       l10n_latam_document_number=None, invoice_origin=None,
                       extra_lines=None, clear_taxes=False, line_name=None,
                       move_type='in_invoice',
                       percepcion_lines=None):
    """
    extra_lines: lista de dicts con keys opcionales:
        name, quantity, price_unit, account_id, product_id
    Si se pasa, reemplaza la lógica de línea única (account_id/amount_neto).
    """
    # journal_id se pasa solo si hay preferencia guardada (user_prefs diario_facturas_nombre)
    # Si es None, Odoo elige su diario de compras por defecto

    vals = {"move_type": move_type}
    if partner_id:       vals["partner_id"]   = partner_id
    if ref:              vals["ref"]          = ref
    if invoice_date:     vals["invoice_date"] = invoice_date
    if invoice_date_due: vals["invoice_date_due"] = invoice_date_due
    if journal_id:       vals["journal_id"]   = journal_id
    if doc_type_id:      vals["l10n_latam_document_type_id"] = doc_type_id
    if currency_id:      vals["currency_id"]  = currency_id
    if invoice_origin:   vals["invoice_origin"] = invoice_origin
    if l10n_latam_document_number:
        vals["l10n_latam_document_number"] = l10n_latam_document_number

    # Líneas explícitas (ej: de lineas_petdur)
    if extra_lines:
        bill_lines = []
        for ln in extra_lines:
            lv = {
                "name":       ln.get("name") or ref or "Artículo",
                "quantity":   float(ln.get("quantity") or ln.get("cantidad") or 1),
                "price_unit": float(ln.get("price_unit") or ln.get("precio_unit") or 0),
            }
            if ln.get("account_id"):
                lv["account_id"] = ln["account_id"]
            if ln.get("product_id"):
                lv["product_id"] = ln["product_id"]
            if analytic_account_id:
                lv["analytic_distribution"] = {str(analytic_account_id): 100}
            bill_lines.append((0, 0, lv))
        if bill_lines:
            vals["invoice_line_ids"] = bill_lines
    # Línea única legada (account_id/amount_neto)
    elif (account_id or product_id) and amount_neto:
        line_vals = {
            "name":       line_name or ref or "Factura proveedor",
            "price_unit": float(amount_neto),
            "quantity":   1,
        }
        if account_id:  line_vals["account_id"] = account_id
        if product_id:  line_vals["product_id"] = product_id
        if analytic_account_id:
            line_vals["analytic_distribution"] = {str(analytic_account_id): 100}
        if clear_taxes:
            line_vals["tax_ids"] = [(5, 0, 0)]   # limpiar impuestos (factura exenta)
        vals["invoice_line_ids"] = [(0, 0, line_vals)]

    # Las percepciones se agregan VÍA WRITE después de crear la factura
    # (Odoo sobreescribe tax_ids al crear si hay product_id)

    try:
        move_id = call(models, uid, api_key, "account.move", "create", [vals])
    except OdooError as e:
        raise OdooError(f"No se pudo crear la factura '{ref}': {e}") from e

    # Agregar percepciones IIBB/IVA — líneas contables con tax_line_id correcto
    # tax_line_id = tax de repartition → exclude_from_invoice_tab=True automáticamente
    if percepcion_lines and move_id:
        try:
            _perc_total = 0.0
            for _pl in percepcion_lines:
                _amt  = float(_pl.get("importe", 0))
                _aid  = _pl.get("account_id")
                if not _aid or _amt <= 0:
                    continue
                # Buscar el tax correcto via repartition line de esta cuenta
                _tid = None
                try:
                    _reps = models.execute_kw(_cfg.ODOO_DB, uid, api_key,
                        "account.tax.repartition.line", "search_read",
                        [[("account_id", "=", _aid), ("repartition_type", "=", "tax")]],
                        {"fields": ["tax_id"], "limit": 1})
                    if _reps:
                        _tv = _reps[0]["tax_id"]
                        _tid = _tv[0] if isinstance(_tv, (list, tuple)) else _tv
                except Exception:
                    pass
                _lv = {
                    "move_id":        move_id,
                    "account_id":     _aid,
                    "name":           str(_pl.get("provincia") or _pl.get("label") or "Percepción").strip(),
                    "debit":          _amt,
                    "credit":         0.0,
                    "amount_currency": _amt,
                }
                if _tid:
                    _lv["tax_line_id"] = _tid
                models.execute_kw(_cfg.ODOO_DB, uid, api_key,
                    "account.move.line", "create", [_lv])
                _perc_total += _amt

            # Actualizar línea Proveedores para que el asiento balancee
            if _perc_total > 0:
                _pay = models.execute_kw(_cfg.ODOO_DB, uid, api_key,
                    "account.move.line", "search_read",
                    [[("move_id", "=", move_id),
                      ("account_id.account_type", "in",
                       ["liability_payable", "liability_current"])]],
                    {"fields": ["id", "credit"], "limit": 1})
                if _pay:
                    _nc = float(_pay[0].get("credit", 0)) + _perc_total
                    models.execute_kw(_cfg.ODOO_DB, uid, api_key,
                        "account.move.line", "write",
                        [[_pay[0]["id"]], {"credit": _nc, "amount_currency": -_nc}])
        except Exception as _pe:
            _logger.warning("create_vendor_bill: percepcion lines: %s", _pe)
    if file_bytes:
        try:
            attach_file(models, uid, api_key, "account.move", move_id, filename, file_bytes, mimetype)
        except Exception:
            pass  # adjunto falla silencioso — la factura ya existe
    return move_id

def create_landed_cost(models, uid, api_key, picking_ids, cost_lines):
    vals = {
        "picking_ids": [(4, pid) for pid in picking_ids],
        "cost_lines": [(0, 0, {
            "product_id": line["product_id"],
            "price_unit": line["price_unit"],
            "split_method": "by_current_cost_price",
        }) for line in cost_lines],
    }
    return call(models, uid, api_key, "stock.landed.cost", "create", [vals])

def create_sale_order(models, uid, api_key, partner_id, note, lines, filename, file_bytes, mimetype,
                      client_order_ref=None, payment_term_id=None, date_order=None,
                      ejecutivo_field=None, ejecutivo_id=None):
    vals = {"partner_id": partner_id, "note": note or ""}
    if client_order_ref: vals["client_order_ref"] = client_order_ref
    if payment_term_id:  vals["payment_term_id"]  = payment_term_id
    if date_order:       vals["date_order"]        = date_order
    if ejecutivo_field and ejecutivo_id:
        vals[ejecutivo_field] = ejecutivo_id
    try:
        order_id = call(models, uid, api_key, "sale.order", "create", [vals])
    except OdooError as e:
        raise OdooError(f"No se pudo crear el pedido: {e}") from e

    for ln in lines:
        line_vals = {
            "order_id":        order_id,
            "name":            ln.get("descripcion") or ln.get("producto") or "Sin descripción",
            "product_uom_qty": _to_float(ln.get("cantidad", 1)),
            "price_unit":      _to_float(ln.get("precio_unit") or ln.get("precio", 0)),
        }
        if ln.get("product_id"):
            _pid = ln["product_id"]
            _vv = call(models, uid, api_key, "product.product", "search",
                       [[("product_tmpl_id", "=", _pid), ("active", "=", True)]], {"limit": 1})
            line_vals["product_id"] = _vv[0] if _vv else _pid
        elif ln.get("producto"):
            prod_ids = call(models, uid, api_key, "product.product", "search",
                            [[("name", "ilike", ln["producto"])]], {"limit": 1})
            if prod_ids:
                line_vals["product_id"] = prod_ids[0]
        try:
            call(models, uid, api_key, "sale.order.line", "create", [line_vals])
        except OdooError as e:
            _logger.warning("Línea de pedido %s no creada: %s", line_vals.get("name"), e)
            # Continuar con las demás líneas aunque una falle

    if file_bytes:
        try:
            attach_file(models, uid, api_key, "sale.order", order_id, filename, file_bytes, mimetype)
        except Exception:
            pass  # adjunto falla silencioso — el pedido ya existe
    # El pedido queda en estado Presupuesto (draft) para revisión y confirmación manual en Odoo
    return order_id

def _to_float(v):
    try:
        return float(str(v).replace(",", "."))
    except Exception:
        return 0.0

def parse_ar_date(raw):
    """
    Convierte fechas a ISO YYYY-MM-DD.
    Soporta: DD/MM/YYYY, DD-MM-YYYY, DD.MM.YYYY, YYYY-MM-DD, YYYY/MM/DD.
    """
    if not raw:
        return ""
    raw = raw.strip()
    # DD/MM/YYYY, DD-MM-YYYY, DD.MM.YYYY
    m = re.match(r"(\d{1,2})[/\-\.](\d{1,2})[/\-\.](\d{4})", raw)
    if m:
        d, mo, y = m.group(1).zfill(2), m.group(2).zfill(2), m.group(3)
        return f"{y}-{mo}-{d}"
    # YYYY-MM-DD, YYYY/MM/DD
    m2 = re.match(r"(\d{4})[/\-](\d{1,2})[/\-](\d{1,2})", raw)
    if m2:
        return raw[:10]
    return ""

def normalize_amount(raw):
    """
    Normaliza un número extraído de factura a float string con punto decimal.
    Soporta formato argentino (1.234,56) y formato US (1,234.56).
    """
    raw = raw.strip()
    last_comma = raw.rfind(",")
    last_dot   = raw.rfind(".")
    if last_comma > last_dot:
        # Formato argentino: último separador es coma → decimal
        return raw.replace(".", "").replace(",", ".")
    elif last_dot > last_comma:
        # Formato US o sin miles: último separador es punto → decimal
        return raw.replace(",", "")
    else:
        return raw.replace(",", ".")

_ODOO17_PATHS = {
    "account.move":       "odoo/accounting/vendor-bills",
    "purchase.order":     "odoo/purchase",
    "sale.order":         "odoo/sales",
    "stock.picking":      "odoo/inventory/receipts",
    "stock.landed.cost":       "odoo/inventory/landed-costs",
    "res.partner":             "odoo/contacts",
    "account.payment":         "odoo/accounting/customers/payments",
    "account.payment.group":   "odoo/accounting/payment-groups",
}

def odoo_url(model, record_id):
    """URL directa Odoo 17 para un registro. Funciona en Odoo 16+ también."""
    path = _ODOO17_PATHS.get(model)
    if path:
        return f"{_cfg.ODOO_URL}/{path}/{record_id}"
    # fallback hash-URL por si el modelo no está mapeado
    return f"{_cfg.ODOO_URL}/web#model={model}&id={record_id}"

def safe_float(v, default=0.0):
    """Convierte a float tolerando formato ARS (1.234,56), strings vacíos y None."""
    if v is None or v == "":
        return default
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace("$", "").replace(" ", "")
    if "," in s and "." in s:
        # 1.234,56 → quitar punto de miles, coma como decimal
        s = s.replace(".", "").replace(",", ".")
    elif "," in s:
        s = s.replace(",", ".")
    elif re.match(r"^\d+\.\d{3}$", s):
        # "77.896" → punto con exactamente 3 dígitos = separador de miles ARS → 77896
        s = s.replace(".", "")
    try:
        return float(s)
    except ValueError:
        return default


def fmt_ars(v):
    """Formatea número como moneda ARS: $ 1.234,56"""
    if not v:
        return ""
    try:
        s = "{:,.2f}".format(float(v))
        return "$ " + s.replace(",", "X").replace(".", ",").replace("X", ".")
    except Exception:
        return str(v)

def validate_cuit(raw: str) -> tuple[bool, str]:
    """Valida y normaliza un CUIT argentino.

    Retorna (ok, cuit_limpio_o_mensaje_error).
    El CUIT limpio es la cadena de 11 dígitos sin guiones ni espacios.
    """
    if not raw or not raw.strip():
        return False, "El CUIT es obligatorio."
    clean = re.sub(r"[\s\-\.]", "", raw.strip())
    if not clean.isdigit():
        return False, f"CUIT inválido '{raw}': debe contener solo dígitos (con o sin guiones)."
    if len(clean) != 11:
        return False, f"CUIT inválido '{raw}': debe tener exactamente 11 dígitos (tiene {len(clean)})."
    # Verificación del dígito verificador
    _factors = [5, 4, 3, 2, 7, 6, 5, 4, 3, 2]
    total = sum(int(clean[i]) * _factors[i] for i in range(10))
    rem = total % 11
    check = 0 if rem == 0 else (11 - rem if rem != 1 else None)
    if check is None:
        return False, f"CUIT inválido '{raw}': dígito verificador no válido."
    if int(clean[10]) != check:
        return False, f"CUIT inválido '{raw}': el dígito verificador no coincide."
    return True, clean


def validate_email(raw: str) -> tuple[bool, str]:
    """Validación básica de formato de email.

    Retorna (ok, mensaje_error_o_vacío).
    """
    if not raw or not raw.strip():
        return True, ""  # Email es opcional en la mayoría de formularios
    addr = raw.strip()
    import re as _re
    if not _re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", addr):
        return False, f"Email inválido '{addr}': debe tener el formato usuario@dominio.com."
    return True, ""


def fmt_usd(v):
    """Formatea número como moneda USD estilo Odoo: u$s 1.234,56"""
    try:
        s = "{:,.2f}".format(float(v))
        return "u$s " + s.replace(",", "X").replace(".", ",").replace("X", ".")
    except Exception:
        return "u$s 0,00"

def parse_payment_terms(text):
    """
    Extrae días de pago de condiciones de venta.
    Retorna int con cantidad de días, o None si no se detecta.
    Ej: "CUENTA CORRIENTE A 10 DIAS" → 10
        "30 DIAS FECHA FACTURA"       → 30
        "A 15 DIAS FF"                → 15
        "CUENTA CORRIENTE"            → None
    """
    pats = [
        r"CUENTA\s+CORRIENTE\s+A\s+(\d+)\s+D[IÍ]AS?",
        r"\bA\s+(\d+)\s+D[IÍ]AS?\b",
        r"(\d+)\s+D[IÍ]AS?\s+(?:FECHA\s+FACTURA|FF|FV)",
        r"(?:Cond\.?\s*Vta\.?|Condici[oó]n(?:es)?\s+de\s+Venta)[:\s]+[^\n]*?(\d+)\s+D[IÍ]AS?",
        r"\b(\d{1,3})\s+D[IÍ]AS?\b",
    ]
    for pat in pats:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            days = int(m.group(1))
            if 1 <= days <= 365:   # sanidad: descartar años o CAE que puedan colar
                return days
    return None

def compute_vencimiento(fecha_iso, days):
    """
    Calcula vencimiento = fecha_iso + days días.
    fecha_iso: 'YYYY-MM-DD'. Retorna 'YYYY-MM-DD' o ''.
    """
    if not fecha_iso or days is None:
        return ""
    try:
        from datetime import date, timedelta
        y, mo, d = int(fecha_iso[:4]), int(fecha_iso[5:7]), int(fecha_iso[8:10])
        vto = date(y, mo, d) + timedelta(days=days)
        return vto.strftime("%Y-%m-%d")
    except Exception:
        return ""

@st.cache_data(ttl=120, show_spinner=False)
def search_partner_by_cuit(models_url, uid, api_key, cuit):
    """
    Busca un partner en Odoo por CUIT (campo vat).
    Retorna (partner_id, nombre) o None si no existe.
    """
    if not cuit:
        return None
    cuit_norm = re.sub(r"[^\d]", "", str(cuit))
    if len(cuit_norm) < 10:
        return None
    try:
        m = xmlrpc.client.ServerProxy(models_url, allow_none=True)
        # Intentar con formato estándar XX-XXXXXXXX-X y sin guiones
        variants = [cuit_norm]
        if len(cuit_norm) == 11:
            variants.append(f"{cuit_norm[:2]}-{cuit_norm[2:10]}-{cuit_norm[10:]}")
        for vat_val in variants:
            rows = m.execute_kw(_cfg.ODOO_DB, uid, api_key, "res.partner", "search_read",
                [[("vat", "=", vat_val), ("active", "=", True)]],
                {"fields": ["id", "name", "vat"], "limit": 1})
            if rows:
                return (rows[0]["id"], rows[0]["name"])
        # Fallback: buscar por los últimos 8 dígitos del CUIT
        rows = m.execute_kw(_cfg.ODOO_DB, uid, api_key, "res.partner", "search_read",
            [[("vat", "ilike", cuit_norm[2:10]), ("active", "=", True)]],
            {"fields": ["id", "name", "vat"], "limit": 3})
        if rows:
            return (rows[0]["id"], rows[0]["name"])
    except Exception:
        pass
    return None

def search_partner_by_cuit_or_name(models_url, uid, api_key, query, limit=8):
    """
    Busca partners en Odoo por CUIT o razón social (parcial).
    - Si el query tiene >= 10 dígitos → busca por VAT (exacto + ilike)
    - Si parece texto → busca por nombre (ilike)
    - Si tiene dígitos y texto → busca por ambos y une resultados
    Retorna lista de dicts con keys: id, name, vat
    """
    if not query or not query.strip():
        return []
    try:
        m   = xmlrpc.client.ServerProxy(models_url, allow_none=True)
        q   = query.strip()
        digits = re.sub(r"[^\d]", "", q)
        results = []
        seen_ids = set()

        def _fetch(domain):
            rows = m.execute_kw(
                _cfg.ODOO_DB, uid, api_key, "res.partner", "search_read",
                [domain + [("active", "=", True)]],
                {"fields": ["id", "name", "vat"], "limit": limit, "order": "name asc"})
            return rows

        # Búsqueda por CUIT/VAT si hay suficientes dígitos
        if len(digits) >= 8:
            variants = [digits]
            if len(digits) == 11:
                variants.append(f"{digits[:2]}-{digits[2:10]}-{digits[10:]}")
            for vat_val in variants:
                for row in _fetch([("vat", "=", vat_val)]):
                    if row["id"] not in seen_ids:
                        results.append(row); seen_ids.add(row["id"])
            if not results:
                for row in _fetch([("vat", "ilike", digits[-8:])]):
                    if row["id"] not in seen_ids:
                        results.append(row); seen_ids.add(row["id"])

        # Búsqueda por nombre si el query tiene letras
        if re.search(r"[A-Za-záéíóúñÁÉÍÓÚÑ]", q):
            for row in _fetch([("name", "ilike", q)]):
                if row["id"] not in seen_ids:
                    results.append(row); seen_ids.add(row["id"])

        return results[:limit]
    except Exception:
        return []

@st.cache_data(ttl=30, show_spinner=False)
def check_invoice_exists(models_url, uid, api_key, ref):
    """
    Verifica si ya existe una factura de proveedor con esa referencia en Odoo.
    Retorna (True, move_id, name) si existe, (False, None, None) si no.
    """
    if not ref or not ref.strip():
        return False, None, None
    try:
        m = xmlrpc.client.ServerProxy(models_url, allow_none=True)
        rows = m.execute_kw(_cfg.ODOO_DB, uid, api_key, "account.move", "search_read",
            [[("ref", "=", ref.strip()),
              ("move_type", "=", "in_invoice"),
              ("state", "!=", "cancel")]],
            {"fields": ["id", "name", "partner_id", "invoice_date", "amount_total"], "limit": 1})
        if rows:
            r = rows[0]
            label = r.get("name") or f"ID {r['id']}"
            return True, r["id"], label
        return False, None, None
    except Exception:
        return False, None, None


@st.cache_data(ttl=3600, show_spinner=False)
def get_all_payment_terms(models_url, uid, api_key):
    """Retorna lista de (id, name) de plazos de pago activos en Odoo."""
    try:
        m = xmlrpc.client.ServerProxy(models_url, allow_none=True)
        rows = m.execute_kw(_cfg.ODOO_DB, uid, api_key, "account.payment.term", "search_read",
            [[("active", "=", True)]],
            {"fields": ["id", "name"], "order": "name asc"})
        return [(r["id"], r["name"]) for r in rows]
    except Exception:
        return []

@st.cache_data(ttl=120, show_spinner=False)
def get_customer_payment_terms(models_url, uid, api_key, partner_id):
    """Retorna (payment_term_id, payment_term_name) del cliente, o (None, None)."""
    try:
        m = xmlrpc.client.ServerProxy(models_url, allow_none=True)
        rows = m.execute_kw(_cfg.ODOO_DB, uid, api_key, "res.partner", "read",
            [[partner_id]],
            {"fields": ["property_payment_term_id"]})
        if rows:
            pt = rows[0].get("property_payment_term_id")
            if pt and isinstance(pt, (list, tuple)) and len(pt) == 2:
                return pt[0], pt[1]
    except Exception:
        pass
    return None, None

@st.cache_data(ttl=300, show_spinner=False)
def search_product_by_code_or_name(models_url, uid, api_key,
                                   code="", name_keywords="", limit=3, ean13=""):
    """
    Busca producto en Odoo. Prioridad:
      1. Código exacto/ilike en product.template  (ideal: LFANT00006, LCANO00022)
      2. Nombre en product.template — varias estrategias
      3. EAN13 barcode en product.product (último recurso)
    Siempre prefiere default_code que empiece con 'L' y mayor standard_price.
    """
    try:
        m   = xmlrpc.client.ServerProxy(models_url, allow_none=True)
        F   = ["id", "name", "default_code", "standard_price", "list_price", "qty_available"]

        def _best(rows):
            if not rows:
                return []
            l = [r for r in rows if str(r.get("default_code") or "").upper().startswith("L")]
            pool = l or rows
            return [max(pool, key=lambda r: float(r.get("standard_price") or 0))]

        def _tmpl(domain, lim=20):
            return m.execute_kw(_cfg.ODOO_DB, uid, api_key,
                                "product.template", "search_read",
                                [domain], {"fields": F, "limit": lim})

        # ── 1. CÓDIGO exacto / ilike en default_code ──────────────────────────
        for c in dict.fromkeys([code.strip(), code.strip().lstrip("0")]):
            if not c:
                continue
            r = _best(_tmpl([("default_code", "=",     c), ("active", "=", True)], 5))
            if r: return r
            r = _best(_tmpl([("default_code", "ilike", c), ("active", "=", True)], 10))
            if r: return r

        # ── 1b. CÓDIGO como fragmento de nombre ────────────────────────────────
        # Útil para códigos de fabricante (GI-16, BH-1) que aparecen en el nombre.
        # NO generamos variante con espacio ("GI 16") porque es demasiado amplia.
        _code_s = code.strip()
        if _code_s and len(_code_s) >= 2:
            _code_variants = dict.fromkeys([
                _code_s,                                   # "GI-16" (con guión: específico)
                re.sub(r"[-_/ ]", "", _code_s),            # "GI16" (sin separadores)
            ])
            for cv in _code_variants:
                if not cv or len(cv) < 2:
                    continue
                # Código puramente numérico: demasiado genérico solo → combinarlo con
                # palabras significativas de la descripción (≥5 chars, no stopwords)
                if cv.isdigit():
                    _stops = {"para", "con", "por", "original", "canon", "pack",
                              "color", "negro", "plano", "escaner", "formato"}
                    _desc_words = [
                        w for w in re.sub(r"[^\w\s]", " ", name_keywords).split()
                        if len(w) >= 4 and w.lower() not in _stops
                    ]
                    for dw in _desc_words[:3]:
                        r = _best(_tmpl([("active", "=", True),
                                         ("name", "ilike", cv),
                                         ("name", "ilike", dw)], 10))
                        if r: return r
                else:
                    r = _best(_tmpl([("active", "=", True), ("name", "ilike", cv)], 10))
                    if r: return r

        # ── 2. NOMBRE por keywords ──────────────────────────────────────────────
        if name_keywords and name_keywords.strip():
            # Preservar guiones para capturar códigos como "GI-16", "BH-1", "LiDE-300"
            kw_hyphens = re.sub(r"[^\w\s\-]", " ", name_keywords)
            kw         = re.sub(r"[^\w\s]",   " ", name_keywords)
            words_hyph = kw_hyphens.split()
            words      = kw.split()
            # Tokens con guión Y dígitos: "GI-16", "BH-1", "LiDE-300" → tratar como modelo
            hyphen_model = [w for w in words_hyph
                            if "-" in w and re.search(r"\d", w) and len(w) >= 3]
            # Tokens con letras Y dígitos (sin guión): G1110, 190C, 190BK…
            model  = [w for w in words
                      if re.search(r"[A-Za-z]", w) and re.search(r"\d", w) and len(w) >= 4]
            # Tokens solo letras, cortos (contexto tipo "GI")
            short  = [w for w in words if w.isalpha() and len(w) == 2]
            # Tokens alfanuméricos cortos (E1, S1, V8, X9…) con palabra extra de contexto
            short_model = [w for w in words
                           if re.search(r"[A-Za-z]", w) and re.search(r"\d", w)
                           and 2 <= len(w) <= 3]

            # 2-extra: buscar por código con guión directamente en el nombre
            for hm in hyphen_model[:2]:
                r = _best(_tmpl([("active", "=", True), ("name", "ilike", hm)], 10))
                if r: return r

            # 2-extra-b: modelo corto + siguiente palabra como contexto ("E1 maxt", "V8 mate")
            for sm in short_model[:2]:
                ctx_words = [w for w in words if w != sm and len(w) >= 3]
                if ctx_words:
                    r = _best(_tmpl([("active", "=", True),
                                     ("name", "ilike", sm),
                                     ("name", "ilike", ctx_words[0])], 10))
                    if r: return r
                # Segundo intento: solo el modelo corto (más amplio)
                r = _best(_tmpl([("active", "=", True), ("name", "ilike", sm)], 10))
                if r: return r

            # 2a. Número de modelo literal ("G1110" → "PIXMA G1110")
            # Con contexto corto (ej: "GI") para evitar falsos positivos:
            # "190C" sin ctx matchea "190CM" en PILETA → con "GI" solo matchea GI-190 C
            if model:
                dom_2a = [("active", "=", True), ("name", "ilike", model[0])]
                if short:
                    dom_2a.append(("name", "ilike", short[0]))
                r = _best(_tmpl(dom_2a))
                if r: return r

            # 2b. Token alfanumérico separado con espacio + contexto 2-char
            # "190C" → partes ["190","C"] → busca "190 C" AND "GI" → "GI-190 C"
            # "190BK"→ partes ["190","BK"]→ "190 BK" no existe → AND("190","BK","GI")
            if model:
                parts = [p for p in
                         re.split(r"(?<=[A-Za-z])(?=\d)|(?<=\d)(?=[A-Za-z])", model[0])
                         if p]
                if len(parts) >= 2:
                    spaced = " ".join(parts)          # "190 C", "190 BK"
                    ctx    = short[:1]                # ["GI"] si existe

                    # 2b-i: spaced + contexto (más específico)
                    base = [("active", "=", True), ("name", "ilike", spaced)]
                    if ctx:
                        base.append(("name", "ilike", ctx[0]))
                    r = _best(_tmpl(base))
                    if r: return r

                    # 2b-ii: AND de partes + contexto (para "PGBK" q no tiene espacio)
                    base2 = ([("active", "=", True)]
                             + [("name", "ilike", p) for p in parts[:2]]
                             + ([("name", "ilike", ctx[0])] if ctx else []))
                    r = _best(_tmpl(base2))
                    if r: return r

            # ── 2c. Fallback texto puro: palabras largas, sin dígitos ──────────────
            # Cubre productos como "sillas gamer", "linterna recargable", etc.
            _stops_txt = {"para", "con", "por", "original", "pack", "color",
                          "negro", "unidad", "unidades", "precio", "total",
                          "costo", "envio", "marca", "modelo", "articulo",
                          "producto", "colores", "varios"}
            _txt_words = [
                w.lower() for w in re.sub(r"[^\w\s]", " ", name_keywords).split()
                if len(w) >= 5
                and w.lower() not in _stops_txt
                and not any(c.isdigit() for c in w)
            ]
            if _txt_words:
                # Dos palabras clave juntas (más específico)
                if len(_txt_words) >= 2:
                    r = _best(_tmpl([("active", "=", True),
                                     ("name", "ilike", _txt_words[0]),
                                     ("name", "ilike", _txt_words[1])], 10))
                    if r: return r
                # Una sola palabra clave
                r = _best(_tmpl([("active", "=", True),
                                 ("name", "ilike", _txt_words[0])], 15))
                if r: return r

        # ── 3. EAN13 ────────────────────────────────────────────────────────────
        if ean13 and len(str(ean13)) == 13 and str(ean13).isdigit():
            r = m.execute_kw(_cfg.ODOO_DB, uid, api_key, "product.product", "search_read",
                             [[("barcode", "=", str(ean13)), ("active", "=", True)]],
                             {"fields": F, "limit": 1})
            if r: return r

    except Exception:
        pass
    return []


def get_ejecutivo_field(models_url, uid, api_key):
    """Detecta el nombre técnico y el modelo de relación del campo
    'Ejecutivo de cuenta' / 'Referrer' en sale.order.
    Retorna (field_name, relation_model) o (None, None).
    En Odoo 17 AR el campo nativo es 'referrer_id' (Many2one → res.partner).
    Como fallback busca campos x_ con keywords ejecutivo/referido."""
    try:
        _mx = xmlrpc.client.ServerProxy(models_url)
        fields = _mx.execute_kw(_cfg.ODOO_DB, uid, api_key,
            "sale.order", "fields_get", [],
            {"attributes": ["string", "type", "relation"]})
        # 1. Primero: campo nativo conocido
        if "referrer_id" in fields:
            finfo = fields["referrer_id"]
            if finfo.get("type") == "many2one":
                return "referrer_id", finfo.get("relation", "res.partner")
        # 2. Fallback: cualquier campo (incluyendo x_) con keywords
        keywords = ["ejecutivo", "referido", "referrer"]
        for fname, finfo in fields.items():
            label = finfo.get("string", "").lower()
            if any(kw in label for kw in keywords):
                return fname, finfo.get("relation", "res.partner")
        return None, None
    except Exception:
        return None, None

@st.cache_data(ttl=3600, show_spinner=False)
def get_referidos(models_url, uid, api_key):
    """Devuelve lista de (id, nombre) de partners usados como Referido en Odoo."""
    try:
        _mx = xmlrpc.client.ServerProxy(models_url)
        groups = _mx.execute_kw(
            _cfg.ODOO_DB, uid, api_key,
            "res.partner", "read_group",
            [[["x_studio_referido_1", "!=", False]],
             ["x_studio_referido_1"],
             ["x_studio_referido_1"]],
            {})
        result = []
        for g in groups:
            val = g.get("x_studio_referido_1")
            if val and isinstance(val, (list, tuple)) and len(val) == 2:
                result.append((val[0], val[1]))
        return sorted(result, key=lambda x: x[1])
    except Exception:
        return []

def create_partner(models, uid, api_key, name, vat, street="", phone="", email_addr=""):
    """Crea un nuevo cliente en Odoo y retorna su ID."""
    vals = {"name": name, "customer_rank": 1, "is_company": True}
    if vat:        vals["vat"]    = vat
    if street:     vals["street"] = street
    if phone:      vals["phone"]  = phone
    if email_addr: vals["email"]  = email_addr
    return call(models, uid, api_key, "res.partner", "create", [vals])

def create_vendor_partner(models, uid, api_key, name, vat, street="", phone="", email_addr=""):
    """Crea un nuevo proveedor en Odoo y retorna su ID."""
    vals = {"name": name, "supplier_rank": 1, "is_company": True}
    if vat:        vals["vat"]    = vat
    if street:     vals["street"] = street
    if phone:      vals["phone"]  = phone
    if email_addr: vals["email"]  = email_addr
    return call(models, uid, api_key, "res.partner", "create", [vals])


# ─────────────────────────────────────────────────────────────────────────────
# CONTACTOS — helpers ARCA + Odoo
# ─────────────────────────────────────────────────────────────────────────────

@st.cache_data(ttl=86400, show_spinner=False)
def get_ar_states(_models_url, uid, api_key):
    """Provincias argentinas: lista de (id, name)."""
    try:
        m = xmlrpc.client.ServerProxy(_models_url, allow_none=True)
        rows = m.execute_kw(_cfg.ODOO_DB, uid, api_key, "res.country.state", "search_read",
            [[["country_id.code", "=", "AR"]]],
            {"fields": ["id", "name"], "order": "name asc"})
        return [(r["id"], r["name"]) for r in rows]
    except Exception:
        return []


@st.cache_data(ttl=3600, show_spinner=False)
def get_afip_resp_types(_models_url, uid, api_key):
    """Tipos de responsabilidad AFIP: lista de (id, name)."""
    try:
        m = xmlrpc.client.ServerProxy(_models_url, allow_none=True)
        rows = m.execute_kw(_cfg.ODOO_DB, uid, api_key, "l10n_ar.afip.responsibility.type", "search_read",
            [[]], {"fields": ["id", "name"], "order": "sequence asc"})
        return [(r["id"], r["name"]) for r in rows]
    except Exception:
        return []


@st.cache_data(ttl=3600, show_spinner=False)
def get_cuit_id_type(_models_url, uid, api_key):
    """ID del tipo de identificación CUIT en Odoo."""
    try:
        m = xmlrpc.client.ServerProxy(_models_url, allow_none=True)
        rows = m.execute_kw(_cfg.ODOO_DB, uid, api_key, "l10n_latam.identification.type", "search_read",
            [[["name", "ilike", "CUIT"]]],
            {"fields": ["id", "name"], "limit": 1})
        return rows[0]["id"] if rows else None
    except Exception:
        return None


@st.cache_data(ttl=3600, show_spinner=False)
def get_odoo_users(_models_url, uid, api_key):
    """Usuarios activos de Odoo: lista de (id, name)."""
    try:
        m = xmlrpc.client.ServerProxy(_models_url, allow_none=True)
        rows = m.execute_kw(_cfg.ODOO_DB, uid, api_key, "res.users", "search_read",
            [[["active", "=", True], ["share", "=", False]]],
            {"fields": ["id", "name"], "order": "name asc"})
        return [(r["id"], r["name"]) for r in rows]
    except Exception:
        return []


@st.cache_data(ttl=3600, show_spinner=False)
def get_pricelists(_models_url, uid, api_key):
    """Listas de precios activas."""
    try:
        m = xmlrpc.client.ServerProxy(_models_url, allow_none=True)
        rows = m.execute_kw(_cfg.ODOO_DB, uid, api_key, "product.pricelist", "search_read",
            [[["active", "=", True]]],
            {"fields": ["id", "name"], "order": "name asc"})
        return [(r["id"], r["name"]) for r in rows]
    except Exception:
        return []


@st.cache_data(ttl=3600, show_spinner=False)
def get_ar_accounts(_models_url, uid, api_key, account_type=None):
    """Cuentas contables filtradas por tipo (asset_receivable / liability_payable)."""
    try:
        m = xmlrpc.client.ServerProxy(_models_url, allow_none=True)
        domain = []
        if account_type:
            domain = [["account_type", "=", account_type]]
        rows = m.execute_kw(_cfg.ODOO_DB, uid, api_key, "account.account", "search_read",
            [domain], {"fields": ["id", "name", "code"], "order": "code asc", "limit": 200})
        return [(r["id"], f"{r['code']} {r['name']}") for r in rows]
    except Exception:
        return []

def create_full_partner(models, uid, api_key, vals_dict):
    """
    Crea un res.partner completo en Odoo.
    vals_dict puede incluir cualquier campo válido de res.partner.
    Retorna partner_id. Lanza OdooError si falla.
    """
    name = vals_dict.get("name", "?")
    try:
        return call(models, uid, api_key, "res.partner", "create", [vals_dict])
    except OdooError as e:
        raise OdooError(f"No se pudo crear el contacto '{name}': {e}") from e


def match_ar_state(province_name, ar_states):
    """Busca el ID de la provincia argentina más parecida al nombre dado."""
    if not province_name or not ar_states:
        return None
    pn = province_name.strip().lower()
    # Exacto
    for sid, sname in ar_states:
        if sname.lower() == pn:
            return sid
    # Parcial (primera palabra significativa)
    pwords = [w for w in pn.split() if len(w) > 3]
    if pwords:
        for sid, sname in ar_states:
            if any(w in sname.lower() for w in pwords):
                return sid
    return None


@st.cache_data(ttl=300, show_spinner=False)
def get_pending_bills(models_url, uid, api_key):
    """Todas las FAs de proveedor confirmadas y con saldo pendiente."""
    try:
        m = xmlrpc.client.ServerProxy(models_url, allow_none=True)
        rows = m.execute_kw(_cfg.ODOO_DB, uid, api_key, "account.move", "search_read",
            [[("move_type",     "=",  "in_invoice"),
              ("state",         "=",  "posted"),
              ("payment_state", "in", ["not_paid", "partial"])]],
            {"fields": ["id", "name", "ref", "partner_id", "invoice_date",
                        "invoice_date_due", "amount_total", "amount_residual",
                        "currency_id", "payment_state", "journal_id"],
             "order":  "invoice_date asc",
             "limit":  300})
        return rows
    except Exception as e:
        return []

@st.cache_data(ttl=300, show_spinner=False)
def get_payment_journals(models_url, uid, api_key):
    """
    Diarios banco/caja activos en ARS + MercadoPago.
    Excluye diarios con moneda extranjera explícita (USD, EUR, etc.).
    Incluye diarios sin moneda (= ARS de la empresa) y los que dicen ARS/PESO.
    """
    try:
        m = xmlrpc.client.ServerProxy(models_url, allow_none=True)
        rows = m.execute_kw(_cfg.ODOO_DB, uid, api_key, "account.journal", "search_read",
            [[]],
            {"fields": ["id", "name", "currency_id", "type", "active"], "order": "name asc",
             "context": {"active_test": False}})
        result = []
        for r in rows:
            if r.get("type") not in ("bank", "cash"):
                continue
            if not r.get("active", True):
                continue
            cur       = r.get("currency_id")
            has_cur   = bool(cur and isinstance(cur, (list, tuple)) and cur[0])
            cur_name  = cur[1] if has_cur else "ARS"
            cur_upper = cur_name.upper()
            name_upper = r["name"].upper()
            is_mp      = "MERCADOPAGO" in name_upper or "MERCADO PAGO" in name_upper
            is_foreign = has_cur and not any(k in cur_upper for k in ("ARS", "PESO"))
            if is_foreign and not is_mp:
                continue
            result.append((r["id"], r["name"], cur_name))
        return result   # list of (id, label, currency_name)
    except Exception as e:
        return []


@st.cache_data(ttl=3600, show_spinner=False)
def get_all_banks(_models_url, uid, api_key):
    """Lista de (id, name) de res.bank para matchear bancos de cheques."""
    try:
        m = xmlrpc.client.ServerProxy(_models_url, allow_none=True)
        rows = m.execute_kw(_cfg.ODOO_DB, uid, api_key, "res.bank", "search_read",
            [[]], {"fields": ["id", "name"], "limit": 500, "order": "name asc"})
        return [(r["id"], r["name"]) for r in rows]
    except Exception:
        return []


def match_bank_id(bank_name, all_banks):
    """Nombre de banco del home banking -> res.bank.id o None."""
    if not bank_name or not all_banks:
        return None
    clean = re.sub(r"\s*-\s*\d{5,}\s*$", "", bank_name.strip()).upper()
    stop = {"BANCO","DEL","DE","LA","LAS","LOS","EL","Y","SA","SRL"}
    words = [w for w in re.sub(r"[^\w\s]","",clean).split() if len(w)>=4 and w not in stop]
    kw = words[0].lower() if words else clean[:15].lower()
    for bid, bname in all_banks:
        if kw in bname.lower(): return bid
    return None


def register_payment_wizard(models, uid, api_key, move_ids, payment_date, journal_id):
    """
    Genera un pago via el wizard account.payment.register.
    Funciona para uno o varios bills del mismo proveedor/moneda.
    """
    ctx = {
        "active_model": "account.move",
        "active_ids":   move_ids,
        "active_id":    move_ids[0] if move_ids else None,
    }
    try:
        wiz_id = models.execute_kw(_cfg.ODOO_DB, uid, api_key,
            "account.payment.register", "create",
            [{"payment_date": payment_date, "journal_id": journal_id}],
            {"context": ctx})
        result = models.execute_kw(_cfg.ODOO_DB, uid, api_key,
            "account.payment.register", "action_create_payments",
            [[wiz_id]], {"context": ctx})
        return True, result
    except Exception as e:
        return False, str(e)

def _get_payment_doc_type(models, uid, api_key, payment_type, partner_type, journal_id):
    """Devuelve el l10n_latam_document_type_id correcto para un pago usando default_get de Odoo.
    Esto garantiza que el pago aparezca en Clientes>Recibos o Proveedores>Órdenes de pago."""
    try:
        ctx = {
            "default_payment_type": payment_type,
            "default_partner_type": partner_type,
            "default_journal_id":   journal_id,
        }
        defaults = models.execute_kw(_cfg.ODOO_DB, uid, api_key, "account.payment", "default_get",
            [["l10n_latam_document_type_id"]], {"context": ctx})
        return defaults.get("l10n_latam_document_type_id") or False
    except Exception:
        return False

def create_advance_payment(models, uid, api_key, partner_id, amount,
                           currency_id, payment_date, journal_id, memo=""):
    """Pago a cuenta: crea y confirma un pago SIN vincular a ninguna FA.
    Setea l10n_latam_document_type_id para que aparezca en Proveedores > Órdenes de pago."""
    vals = {
        "payment_type": "outbound",
        "partner_type": "supplier",
        "partner_id":   partner_id,
        "amount":       float(amount),
        "currency_id":  currency_id,
        "date":         payment_date,
        "journal_id":   journal_id,
        "ref":          memo or "",
    }
    _doc_type = _get_payment_doc_type(models, uid, api_key, "outbound", "supplier", journal_id)
    if _doc_type:
        vals["l10n_latam_document_type_id"] = _doc_type
    pay_id = models.execute_kw(_cfg.ODOO_DB, uid, api_key, "account.payment", "create", [vals])
    models.execute_kw(_cfg.ODOO_DB, uid, api_key, "account.payment", "action_post", [[pay_id]])
    return pay_id

@st.cache_data(ttl=300, show_spinner=False)
def get_pending_expense_sheets(models_url, uid, api_key):
    """Notas de gastos aprobadas pendientes de pago (hr.expense.sheet state=post)."""
    try:
        m = xmlrpc.client.ServerProxy(models_url, allow_none=True)
        rows = m.execute_kw(_cfg.ODOO_DB, uid, api_key, "hr.expense.sheet", "search_read",
            [[("state", "=", "post")]],
            {"fields": ["id", "name", "employee_id", "total_amount",
                        "currency_id", "payment_state"],
             "order": "id asc", "limit": 100})
        return rows
    except Exception:
        return []

def register_expense_payment(models, uid, api_key, sheet_id, payment_date, journal_id):
    """Registra el pago de una nota de gastos aprobada via el wizard de Odoo."""
    ctx = {
        "active_model": "hr.expense.sheet",
        "active_id":    sheet_id,
        "active_ids":   [sheet_id],
    }
    try:
        wiz_id = models.execute_kw(_cfg.ODOO_DB, uid, api_key,
            "account.payment.register", "create",
            [{"payment_date": payment_date, "journal_id": journal_id}],
            {"context": ctx})
        result = models.execute_kw(_cfg.ODOO_DB, uid, api_key,
            "account.payment.register", "action_create_payments",
            [[wiz_id]], {"context": ctx})
        return True, result
    except Exception as e:
        return False, str(e)


@st.cache_data(ttl=180, show_spinner=False)
def search_partners_by_cuits(models_url, uid, api_key, cuits_tuple):
    """Busca socios en Odoo por tupla de CUITs. Retorna dict {cuit_sin_guiones: (id, name)}.
    Maneja que Odoo puede guardar el VAT con guiones (30-71189948-7) o sin (30711899487)."""
    try:
        m = xmlrpc.client.ServerProxy(models_url, allow_none=True)

        def _cuit_variants(c):
            """Devuelve variantes de un CUIT para cubrir todos los formatos de Odoo:
            - sin guiones:        30711899487
            - con guiones:        30-71189948-7
            - con prefijo AR:     AR30711899487   (Odoo l10n_ar)
            - con prefijo AR+gu:  AR30-71189948-7
            """
            clean = str(c).replace("-", "").replace(" ", "").replace(".", "").strip()
            # Si ya viene con prefijo AR, quitarlo para la base
            if clean.upper().startswith("AR"):
                clean = clean[2:]
            variants = [clean]
            if len(clean) == 11 and clean.isdigit():
                fmt = f"{clean[:2]}-{clean[2:10]}-{clean[10]}"
                variants.append(fmt)
                variants.append(f"AR{clean}")
                variants.append(f"AR{fmt}")
            return variants

        norm_set = set()   # CUITs sin guiones para lookup final
        all_variants = []  # todas las variantes para el query
        for c in cuits_tuple:
            vs = _cuit_variants(c)
            norm_set.add(vs[0])
            all_variants.extend(vs)

        rows = m.execute_kw(_cfg.ODOO_DB, uid, api_key, "res.partner", "search_read",
            [[("vat", "in", all_variants), ("active", "=", True)]],
            {"fields": ["id", "name", "vat", "is_company", "parent_id",
                        "customer_rank", "type"], "limit": 300})

        def _partner_score(r):
            """Mayor puntaje = registro más apropiado para asociar un cobro.
            Prioriza: cliente activo > empresa raíz > sin tipo especial."""
            score = 0
            if (r.get("customer_rank") or 0) > 0: score += 100
            if r.get("is_company"):                score += 50
            if not r.get("parent_id"):             score += 30
            if r.get("type") in (False, "contact", "other"): score += 10
            return score

        # Agrupar por CUIT normalizado y quedarse con el de mayor score
        _candidates = {}   # cuit_clean → list of rows
        for r in rows:
            vat_raw   = (r.get("vat") or "").strip().upper()
            if vat_raw.startswith("AR"):
                vat_raw = vat_raw[2:]
            vat_clean = vat_raw.replace("-", "").replace(" ", "")
            if vat_clean in norm_set:
                _candidates.setdefault(vat_clean, []).append(r)

        result = {}
        for vat_clean, candidates in _candidates.items():
            best = max(candidates, key=_partner_score)
            result[vat_clean] = (best["id"], best["name"])
        return result
    except Exception:
        return {}

@st.cache_data(ttl=180, show_spinner=False)
def get_customer_unpaid_invoices(models_url, uid, api_key, partner_ids_tuple):
    """Facturas de cliente sin pagar (posted, not_paid/partial/in_payment).
    Usa child_of para incluir contactos secundarios del mismo socio.
    Incluye in_payment porque en Odoo AR muchas FAs quedan en ese estado
    cuando el pago está registrado pero sin conciliar con extracto bancario."""
    try:
        m = xmlrpc.client.ServerProxy(models_url, allow_none=True)
        rows = m.execute_kw(_cfg.ODOO_DB, uid, api_key, "account.move", "search_read",
            [[("move_type", "=", "out_invoice"),
              ("state", "=", "posted"),
              ("payment_state", "in", ["not_paid", "partial", "in_payment"]),
              ("partner_id", "child_of", list(partner_ids_tuple))]],
            {"fields": ["id", "name", "invoice_date", "invoice_date_due",
                        "amount_total", "amount_residual", "currency_id", "partner_id"],
             "order": "invoice_date asc", "limit": 300})
        return rows
    except Exception as _e:
        st.warning(f"⚠️ Error al cargar facturas pendientes: {_e}")
        return []

@st.cache_data(ttl=180, show_spinner=False)
def get_customer_pending_credit_notes(models_url, uid, api_key, partner_ids_tuple):
    """Notas de crédito de cliente con saldo pendiente de aplicar (out_refund posted, not_paid/partial)."""
    try:
        m = xmlrpc.client.ServerProxy(models_url, allow_none=True)
        rows = m.execute_kw(_cfg.ODOO_DB, uid, api_key, "account.move", "search_read",
            [[("move_type", "=", "out_refund"),
              ("state", "=", "posted"),
              ("partner_id", "child_of", list(partner_ids_tuple))]],
            {"fields": ["id", "name", "invoice_date", "amount_total",
                        "amount_residual", "currency_id", "partner_id"],
             "order": "invoice_date asc", "limit": 300})
        # amount_residual en out_refund puede ser negativo en Odoo → normalizar
        result = []
        for _r in rows:
            _res = abs(float(_r.get("amount_residual") or 0))
            _tot = abs(float(_r.get("amount_total")    or 0))
            if _res > 0.009:   # solo NCs con saldo pendiente real
                _r["amount_residual"] = _res
                _r["amount_total"]    = _tot
                result.append(_r)
        return result
    except Exception as _e:
        st.warning(f"⚠️ Error al cargar notas de crédito pendientes: {_e}")
        return []

@st.cache_data(ttl=60, show_spinner=False)

def search_registered_orders(models_url, uid, api_key,
                              partner_id=None, date_from=None, date_to=None,
                              limit=50):
    """Busca pedidos de venta ya registrados en Odoo.
    Retorna lista de dicts con id, name, partner, fecha, total, estado, url."""
    try:
        m = xmlrpc.client.ServerProxy(models_url, allow_none=True)
        domain = [("state", "not in", ["cancel"])]
        if partner_id:
            domain.append(("partner_id", "=", partner_id))
        if date_from:
            domain.append(("date_order", ">=", str(date_from)))
        if date_to:
            domain.append(("date_order", "<=", str(date_to) + " 23:59:59"))
        records = m.execute_kw(
            _cfg.ODOO_DB, uid, api_key,
            "sale.order", "search_read",
            [domain],
            {"fields": ["id", "name", "partner_id", "date_order",
                        "amount_total", "state", "client_order_ref"],
             "order":  "date_order desc",
             "limit":  limit})
        result = []
        for r in (records or []):
            partner_name = r["partner_id"][1] if r.get("partner_id") else "—"
            estado_map = {"draft": "Borrador", "sent": "Enviado",
                          "sale": "Confirmado", "done": "Completado"}
            result.append({
                "id":      r["id"],
                "name":    r.get("name") or f"Pedido #{r['id']}",
                "partner": partner_name,
                "fecha":   (r.get("date_order") or "—")[:10],
                "total":   r.get("amount_total") or 0,
                "estado":  estado_map.get(r.get("state"), r.get("state") or "—"),
                "oc_ref":  r.get("client_order_ref") or "",
                "url":     f"{_cfg.ODOO_URL}/odoo/sales/{r['id']}",
            })
        return result
    except Exception as e:
        _logger.error("search_registered_orders: %s", e)
        return []

def search_registered_payments(models_url, uid, api_key,
                                partner_id=None, date_from=None, date_to=None,
                                limit=50):
    """Busca pagos (recibos) ya registrados en Odoo.

    Filtra en account.payment.group con estado 'posted'.
    Retorna lista de dicts con id, name, partner, date, amount, state, url.
    """
    try:
        m = xmlrpc.client.ServerProxy(models_url, allow_none=True)
        domain = [("state", "=", "posted")]
        if partner_id:
            domain.append(("partner_id", "=", partner_id))
        if date_from:
            domain.append(("payment_date", ">=", str(date_from)))
        if date_to:
            domain.append(("payment_date", "<=", str(date_to)))
        records = m.execute_kw(
            _cfg.ODOO_DB, uid, api_key,
            "account.payment.group", "search_read",
            [domain],
            {"fields": ["id", "name", "partner_id", "payment_date",
                        "total_amount", "state", "receiptbook_id"],
             "order":  "payment_date desc",
             "limit":  limit},
        )
        result = []
        for r in (records or []):
            partner_name = r["partner_id"][1] if r.get("partner_id") else "—"
            result.append({
                "id":      r["id"],
                "name":    r.get("name") or f"Pago #{r['id']}",
                "partner": partner_name,
                "fecha":   r.get("payment_date") or "—",
                "importe": r.get("total_amount") or 0,
                "estado":  r.get("state") or "—",
                "url":     f"{_cfg.ODOO_URL}/odoo/accounting/payment-groups/{r['id']}",
            })
        return result
    except Exception as e:
        _logger.error("search_registered_payments: %s", e)
        return []


def register_customer_payment(models, uid, api_key,
                               partner_id, amount, currency_id,
                               payment_date, journal_id,
                               move_ids=None, memo="", cheques=None,
                               withholdings=None,
                               writeoff_account_id=None,
                               writeoff_label="Diferencia de redondeo"):
    """Registra un recibo de cobro de cliente usando account.payment.group.
    Flujo correcto para Odoo AR (módulo account_payments_group):
      1. Crear account.payment.group con payment_type='receivable'
      2. Crear account.payment vinculado al grupo (payment_group_id)
      3. Opcionalmente linkear facturas (move_line_ids)
      4. Llamar post() en el grupo para confirmar
    Esto garantiza que el recibo aparezca en Clientes > Recibos."""
    try:
        # 1. Obtener las líneas receivable de las facturas seleccionadas
        inv_line_ids = []
        if move_ids:
            inv_lines = models.execute_kw(_cfg.ODOO_DB, uid, api_key,
                "account.move.line", "search_read",
                [[("move_id", "in", move_ids),
                  ("account_id.account_type", "=", "asset_receivable"),
                  ("reconciled", "=", False)]],
                {"fields": ["id"], "limit": 100})
            inv_line_ids = [l["id"] for l in inv_lines]

        # 2. Crear el grupo (solo campos del grupo, sin pagos inline)
        group_vals = {
            "payment_type": "receivable",
            "partner_id":   partner_id,
            "currency_id":  currency_id,
            "date":         payment_date,
        }
        if inv_line_ids:
            group_vals["move_line_ids"] = [(6, 0, inv_line_ids)]
        group_id = models.execute_kw(_cfg.ODOO_DB, uid, api_key,
            "account.payment.group", "create", [group_vals])

        # 3. Crear el payment vinculado al grupo
        #    Nota: en esta instalación el campo se llama "memo" (no "ref")

        # 3a. Buscar la linea de metodo de pago "Cheque de Terceros Existente"
        #     para el journal seleccionado (code: out_third_party_checks = Existing Third Party Checks)
        try:
            pml_lines = models.execute_kw(_cfg.ODOO_DB, uid, api_key,
                "account.payment.method.line", "search_read",
                [[["journal_id", "=", journal_id],
                  ["payment_method_id.code", "=", "new_third_party_checks"]]],
                {"fields": ["id"], "limit": 1})
            pml_id = pml_lines[0]["id"] if pml_lines else None
        except Exception:
            pml_id = None

        # Si hay retenciones Y cheques, el pago principal debe ser el monto TOTAL
        # de los cheques. Odoo AR valida: payment.amount == sum(cheques.amount).
        # Las retenciones se crean como pagos adicionales en el mismo grupo.
        _cheque_total = sum(float(ch.get("amount") or 0) for ch in (cheques or []))
        _pay_amount = _cheque_total if (cheques and withholdings) else float(amount)

        pay_vals = {
            "payment_type":   "inbound",
            "partner_type":   "customer",
            "partner_id":     partner_id,
            "amount":         _pay_amount,
            "currency_id":    currency_id,
            "date":           payment_date,
            "journal_id":     journal_id,
            "memo":           memo or "",
            "payment_group_id": group_id,
        }
        if pml_id:
            pay_vals["payment_method_line_id"] = pml_id

        # 3b. Adjuntar cheques inline (l10n_latam_new_check_ids)
        if cheques:
            check_lines = []
            for ch in cheques:
                ch_vals = {
                    "payment_date": ch.get("payment_date") or payment_date,
                    "amount":       float(ch.get("amount") or amount),
                }
                if ch.get("nro"):       ch_vals["name"] = str(ch["nro"])
                if ch.get("bank_id"):   ch_vals["bank_id"] = ch["bank_id"]
                if ch.get("issuer_vat"):ch_vals["issuer_vat"] = str(ch["issuer_vat"])
                if pml_id:             ch_vals["payment_method_line_id"] = pml_id
                check_lines.append((0, 0, ch_vals))
            pay_vals["l10n_latam_new_check_ids"] = check_lines

        models.execute_kw(_cfg.ODOO_DB, uid, api_key,
            "account.payment", "create", [pay_vals])

        # 3c. Pagos adicionales por retenciones (en el mismo grupo)
        if withholdings:
            # Cargar todos los diarios generales/cash una sola vez
            _all_jrnls = []
            try:
                _all_jrnls = models.execute_kw(_cfg.ODOO_DB, uid, api_key,
                    "account.journal", "search_read",
                    [[("type", "in", ["general", "cash"])]],
                    {"fields": ["id", "name"], "limit": 100})
            except Exception:
                pass

            def _find_journal_for_wh(concepto_str):
                """Elige el diario más específico para la retención dada su descripción."""
                _clow = (concepto_str or "").lower()
                # Provincias argentinas para matchear en nombre de diario
                _provs = [
                    "misiones", "buenos aires", "cordoba", "córdoba",
                    "santa fe", "mendoza", "corrientes", "chaco",
                    "formosa", "salta", "jujuy", "tucuman", "tucumán",
                    "catamarca", "rioja", "san juan", "san luis",
                    "neuquen", "neuquén", "rio negro", "chubut",
                    "santa cruz", "entre rios", "la pampa", "tierra del fuego",
                ]
                # 1. Diario que coincide en provincia + "retenc"/"iibb"
                for _prov in _provs:
                    if _prov in _clow:
                        for _j in _all_jrnls:
                            _jlow = _j["name"].lower()
                            if _prov in _jlow and ("retenc" in _jlow or "iibb" in _jlow):
                                return _j["id"]
                # 2. Diario que coincide solo en provincia
                for _prov in _provs:
                    if _prov in _clow:
                        for _j in _all_jrnls:
                            if _prov in _j["name"].lower():
                                return _j["id"]
                # 3. Cualquier diario con "retenc" o "iibb"
                for _kw in ["retenc", "retencion", "iibb"]:
                    for _j in _all_jrnls:
                        if _kw in _j["name"].lower():
                            return _j["id"]
                # 4. Fallback misc/general
                for _kw in ["misc", "varios", "general", "op"]:
                    for _j in _all_jrnls:
                        if _kw in _j["name"].lower():
                            return _j["id"]
                return _all_jrnls[0]["id"] if _all_jrnls else None

            _wh_errors = []
            for _wh in withholdings:
                _wh_amt = float(_wh.get("monto") or _wh.get("amount") or 0)
                if _wh_amt <= 0:
                    continue
                # Diario específico según provincia/concepto de esta retención
                _ret_journal_id = _find_journal_for_wh(_wh.get("concepto", ""))
                if not _ret_journal_id:
                    _wh_errors.append(f"Sin diario para retención: {_wh.get('concepto','')}")
                    continue
                # payment_method_line_id para este diario
                _wh_pml_id = None
                try:
                    _wh_pml_lines = models.execute_kw(_cfg.ODOO_DB, uid, api_key,
                        "account.payment.method.line", "search_read",
                        [[["journal_id", "=", _ret_journal_id]]],
                        {"fields": ["id"], "limit": 1})
                    _wh_pml_id = _wh_pml_lines[0]["id"] if _wh_pml_lines else None
                except Exception:
                    pass
                # Pago outbound en el grupo: reduce el neto aplicado a la factura
                _wh_vals = {
                    "payment_type":     "outbound",
                    "partner_type":     "customer",
                    "partner_id":       partner_id,
                    "amount":           _wh_amt,
                    "currency_id":      currency_id,
                    "date":             payment_date,
                    "journal_id":       _ret_journal_id,
                    "memo":             _wh.get("concepto") or "Retención",
                    "payment_group_id": group_id,
                }
                if _wh_pml_id:
                    _wh_vals["payment_method_line_id"] = _wh_pml_id
                try:
                    models.execute_kw(_cfg.ODOO_DB, uid, api_key,
                        "account.payment", "create", [_wh_vals])
                except Exception as _whe:
                    _wh_errors.append(str(_whe))
            if _wh_errors:
                # Retornar los errores como advertencia (el recibo principal ya quedó)
                return True, f"__WH_WARN__{'|'.join(_wh_errors)}"

        # 3d. Configurar writeoff si hay diferencia de redondeo a saldar
        if writeoff_account_id and inv_line_ids:
            try:
                models.execute_kw(_cfg.ODOO_DB, uid, api_key,
                    "account.payment.group", "write",
                    [[group_id], {
                        "payment_difference_handling": "reconcile",
                        "writeoff_account_id": writeoff_account_id,
                        "writeoff_label": writeoff_label or "Diferencia de redondeo",
                    }])
            except Exception as _wo_err:
                _logger.warning("register_customer_payment: writeoff config: %s", _wo_err)

        # 4. Confirmar el grupo
        # Nota: post() retorna None en esta instalacion, lo que causa un error
        # de marshalling en XML-RPC. Se ignora ese error especifico y se verifica
        # el estado real del grupo para confirmar que se confirmo correctamente.
        try:
            models.execute_kw(_cfg.ODOO_DB, uid, api_key,
                "account.payment.group", "post", [[group_id]])
        except Exception as post_err:
            err_str = str(post_err)
            if "marshal" in err_str.lower() or "none" in err_str.lower() or "nil" in err_str.lower():
                # post() retorno None -> XML-RPC no puede serializarlo,
                # pero la accion se ejecuto. Verificar estado real.
                try:
                    grp_check = models.execute_kw(_cfg.ODOO_DB, uid, api_key,
                        "account.payment.group", "read",
                        [[group_id]], {"fields": ["state", "name"]})
                    if grp_check and grp_check[0].get("state") == "posted":
                        return True, group_id  # Confirmado OK a pesar del error XML-RPC
                except Exception:
                    pass
            raise  # Re-lanzar si no es el error esperado

        return True, group_id
    except OdooError as e:
        return False, str(e)
    except Exception as e:
        msg = _clean_odoo_fault(str(e)) if "Traceback" in str(e) else str(e)
        _logger.error("register_customer_payment: %s", msg)
        return False, msg
