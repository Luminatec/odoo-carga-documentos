"""
Carga de documentos → Odoo
Luminatec / GPowerByte
v3 — Auth por email/contraseña + API key de servicio central
"""

import streamlit as st
import xmlrpc.client
import base64
import re
from io import BytesIO

import pandas as pd

st.set_page_config(
    page_title="Luminatec · Odoo",
    page_icon="🛒",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown("""
<style>
  [data-testid="stSidebar"] { background: #111111 !important; }
  [data-testid="stSidebar"] label,
  [data-testid="stSidebar"] .stMarkdown,
  [data-testid="stSidebar"] p,
  [data-testid="stSidebar"] small,
  [data-testid="stSidebar"] span { color: #eeeeee !important; }
  [data-testid="stSidebar"] .stTextInput input {
    background: #222 !important; color: #eee !important; border-color: #444 !important;
  }
  [data-testid="stSidebar"] .stButton button,
  [data-testid="stSidebar"] .stFormSubmitButton button {
    background: #CC0000 !important; color: white !important;
    border: none !important; font-weight: 700 !important;
  }
  [data-testid="stSidebar"] .stButton button:hover,
  [data-testid="stSidebar"] .stFormSubmitButton button:hover { background: #AA0000 !important; }
  .lumi-sidebar-logo { display:flex; align-items:center; gap:8px; padding:6px 0 12px 0; }
  .lumi-logo-text { font-size:1.6rem; font-weight:900; color:#CC0000 !important; letter-spacing:-1px; }
  .lumi-logo-dot  { color:#F5C200 !important; font-size:1.8rem; }
  .main-title { font-size:1.9rem; font-weight:900; color:#CC0000; letter-spacing:-1px; }
  .main-title span { color:#F5C200; }
  [data-testid="stFileUploader"] { border:2px dashed #CC000033; border-radius:10px; padding:8px; }
  .admin-badge {
    display:inline-block; background:#F5C200; color:#111;
    font-size:0.7rem; font-weight:800; padding:3px 10px;
    border-radius:10px; text-transform:uppercase; letter-spacing:0.5px;
  }
  .user-chip {
    display:inline-block; background:#1a1a1a; color:#eee;
    font-size:0.78rem; padding:4px 12px; border-radius:20px;
    border:1px solid #444; margin-bottom:4px;
  }
</style>
""", unsafe_allow_html=True)

ODOO_URL = "https://gpowerbyte-luminatec.odoo.com"
ODOO_DB  = "gpowerbyte-luminatec-master-22753148"

# Emails con acceso a Importaciones — también configurable en st.secrets["ADMIN_EMAILS"]
_raw_admin = st.secrets.get("ADMIN_EMAILS", "ivarela@luminatec.com,dario@luminatec.com")
ADMIN_EMAILS = {e.strip().lower() for e in _raw_admin.split(",")}

MIMETYPES = {
    "pdf":  "application/pdf",
    "jpg":  "image/jpeg",
    "jpeg": "image/jpeg",
    "png":  "image/png",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "xls":  "application/vnd.ms-excel",
}

LC_PRODUCTS = {
    20241: "CMV - Agente de carga",
    20242: "CMV - Honorarios despachante",
    20243: "CMV - Otros",
    20244: "CMV - Terminal portuaria",
    20245: "CMV - Tasas y Derechos",
    20276: "Despacho Cta Transitoria 21%",
    20277: "Despacho Cta Transitoria 10,5%",
}

ETAPAS_DEF = [
    ("0",    "Etapa 0",    "OC PETDUR confirmada y bloqueada"),
    ("1",    "Etapa 1",    "Bill PETDUR posted (DR 3283 / CR Prov USD)"),
    ("2",    "Etapa 2",    "DI AFIP posted + OP automática BA#26"),
    ("2a",   "Etapa 2a",   "Bills de nacionalización (TRICE, T4, MUNDO COMEX...)"),
    ("2.5",  "Etapa 2.5",  "Reclasificación Tránsito si TC factura ≠ TC despacho"),
    ("3",    "Etapa 3",    "Picking IN validado → WH/PreIngreso"),
    ("T3.5", "Etapa T3.5", "Reclasificación Tránsito → Cuenta Puente"),
    ("4",    "Etapa 4 Bis","Landed Cost validado (by_current_cost_price)"),
    ("5",    "Etapa 5",    "Internal Transfer → WH/Disponible"),
    ("6",    "Etapa 6",    "Acta CFO firmada — 15 checks Decálogo"),
]

DECALOGO = [
    "Cuenta Puente Recepciones (3284) cohorte = $0",
    "Mercadería en Tránsito (3283) cohorte = $0",
    "BAs #8 y #11 sin línea standard_price (verificado hoy)",
    "Productos LC 20241-20245 con expense_id = 3284",
    "Productos LC 20276-20277 con expense_id = 3284",
    "Todos los LCs usaron split_method = by_current_cost_price",
    "NO se usó by_quantity en ningún LC",
    "NO se crearon asientos correctivos preventivos",
    "Costo USD/u del lote coincide con modelo CFO (delta < $0.10 USD/u)",
    "WAC ponderado verificado en libro mayor estándar Odoo UI",
    "Suppliers AFIP residual = deuda real del DI",
    "BA #23 actualizó x_studio_ppp al validar el Landed Cost",
    "Picking IN en estado Done / WH/PreIngreso confirmado",
    "Internal Transfer ejecutado → stock en WH/Disponible",
    "Referencia LUMI_XXX en todos los asientos de la cohorte",
]


# ───────────────────────────────────────────────────
# CONEXIÓN DE SERVICIO (API key central, cacheada)
# ───────────────────────────────────────────────────
@st.cache_resource(show_spinner=False)
def get_service_connection():
    """Raises on error so @st.cache_resource never caches a failed attempt."""
    api_key   = st.secrets.get("ODOO_API_KEY", "")
    svc_email = st.secrets.get("ODOO_SERVICE_EMAIL", "")
    if not api_key or not svc_email:
        raise RuntimeError("Faltan ODOO_API_KEY o ODOO_SERVICE_EMAIL en los secrets.")
    common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common", allow_none=True)
    uid    = common.authenticate(ODOO_DB, svc_email, api_key, {})
    if not uid:
        raise RuntimeError(f"authenticate() devolvió uid=0. Verificá que la API key corresponde a {svc_email} en {ODOO_DB}.")
    models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object", allow_none=True)
    return uid, models, api_key

def verify_user(email, password):
    """
    Verifica email + contraseña contra st.secrets.
    APP_USERS    = "email1:clave1,email2:clave2"  (por usuario)
    APP_PASSWORD = "clave_compartida"              (todos igual)
    """
    import hashlib
    email = email.strip().lower()
    pw_hash = hashlib.sha256(password.encode()).hexdigest()

    app_users_raw = st.secrets.get("APP_USERS", "")
    if app_users_raw:
        user_map = {}
        for entry in app_users_raw.split(","):
            parts = entry.strip().split(":", 1)
            if len(parts) == 2:
                user_map[parts[0].strip().lower()] = parts[1].strip()
        if email not in user_map:
            return False, "Email no autorizado."
        expected = user_map[email]
        if password == expected or pw_hash == expected:
            return True, ""
        return False, "Contraseña incorrecta."

    app_password = st.secrets.get("APP_PASSWORD", "")
    if app_password:
        if password == app_password or pw_hash == app_password:
            return True, ""
        return False, "Contraseña incorrecta."

    return False, "Configurá APP_PASSWORD o APP_USERS en los secrets de Streamlit."

def call(models, uid, api_key, model, method, args, kw=None):
    return models.execute_kw(ODOO_DB, uid, api_key, model, method, args, kw or {})

@st.cache_data(ttl=300, show_spinner=False)
def search_partners(models_url, uid, api_key, name, limit=8):
    m = xmlrpc.client.ServerProxy(models_url, allow_none=True)
    rows = m.execute_kw(ODOO_DB, uid, api_key, "res.partner", "search_read",
        [[("name", "ilike", name), ("active", "=", True)]],
        {"fields": ["id", "name"], "limit": limit, "order": "name asc"})
    return [(r["id"], r["name"]) for r in rows]

@st.cache_data(ttl=600, show_spinner=False)
def get_all_accounts(models_url, uid, api_key):
    """Carga todas las cuentas contables activas de Odoo (cacheado 10 min)."""
    try:
        m = xmlrpc.client.ServerProxy(models_url, allow_none=True)
        rows = m.execute_kw(ODOO_DB, uid, api_key, "account.account", "search_read",
            [[("deprecated", "=", False)]],
            {"fields": ["id", "code", "name"], "order": "code asc"})
        return [(r["id"], f"{r['code']}  {r['name']}") for r in rows]
    except Exception:
        return []

@st.cache_data(ttl=3600, show_spinner=False)
def get_currency_id(models_url, uid, api_key, name):
    """Retorna el ID de la moneda por nombre (ej: 'USD', 'ARS')."""
    try:
        m = xmlrpc.client.ServerProxy(models_url, allow_none=True)
        rows = m.execute_kw(ODOO_DB, uid, api_key, "res.currency", "search_read",
            [[("name", "=", name)]],
            {"fields": ["id", "name"], "limit": 1})
        return rows[0]["id"] if rows else None
    except Exception:
        return None

@st.cache_data(ttl=120, show_spinner=False)
def search_purchase_orders(models_url, uid, api_key, query):
    m = xmlrpc.client.ServerProxy(models_url, allow_none=True)
    rows = m.execute_kw(ODOO_DB, uid, api_key, "purchase.order", "search_read",
        [[("name", "ilike", query), ("state", "in", ["purchase", "done"])]],
        {"fields": ["id", "name", "partner_id", "date_order", "amount_total"], "limit": 10})
    return rows

@st.cache_data(ttl=60, show_spinner=False)
def get_pickings_for_po(models_url, uid, api_key, po_id):
    m = xmlrpc.client.ServerProxy(models_url, allow_none=True)
    rows = m.execute_kw(ODOO_DB, uid, api_key, "stock.picking", "search_read",
        [[("purchase_id", "=", po_id), ("state", "!=", "cancel")]],
        {"fields": ["id", "name", "state", "location_dest_id"], "limit": 10})
    return rows

@st.cache_data(ttl=60, show_spinner=False)
def get_bills_for_carpeta(models_url, uid, api_key, carpeta_ref):
    m = xmlrpc.client.ServerProxy(models_url, allow_none=True)
    rows = m.execute_kw(ODOO_DB, uid, api_key, "account.move", "search_read",
        [[("move_type", "=", "in_invoice"), ("ref", "ilike", carpeta_ref), ("state", "!=", "cancel")]],
        {"fields": ["id", "name", "partner_id", "invoice_date", "amount_total", "state", "journal_id"], "limit": 50})
    return rows

def attach_file(models, uid, api_key, res_model, res_id, filename, file_bytes, mimetype):
    call(models, uid, api_key, "ir.attachment", "create", [{
        "name": filename, "res_model": res_model, "res_id": res_id,
        "datas": base64.b64encode(file_bytes).decode(), "mimetype": mimetype,
    }])

def create_vendor_bill(models, uid, api_key, partner_id, ref, invoice_date,
                       filename, file_bytes, mimetype, journal_id=None, doc_type_id=None,
                       invoice_date_due=None, account_id=None, amount_neto=None,
                       currency_id=None):
    vals = {"move_type": "in_invoice"}
    if partner_id:       vals["partner_id"]   = partner_id
    if ref:              vals["ref"]          = ref
    if invoice_date:     vals["invoice_date"] = invoice_date
    if invoice_date_due: vals["invoice_date_due"] = invoice_date_due
    if journal_id:       vals["journal_id"]   = journal_id
    if doc_type_id:      vals["l10n_latam_document_type_id"] = doc_type_id
    if currency_id:      vals["currency_id"]  = currency_id
    if account_id and amount_neto:
        vals["invoice_line_ids"] = [(0, 0, {
            "account_id": account_id,
            "name": ref or "Factura proveedor",
            "price_unit": float(amount_neto),
            "quantity": 1,
        })]
    move_id = call(models, uid, api_key, "account.move", "create", [vals])
    if file_bytes:
        attach_file(models, uid, api_key, "account.move", move_id, filename, file_bytes, mimetype)
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
                      client_order_ref=None, payment_term_id=None, date_order=None):
    vals = {"partner_id": partner_id, "note": note or ""}
    if client_order_ref: vals["client_order_ref"] = client_order_ref
    if payment_term_id:  vals["payment_term_id"]  = payment_term_id
    if date_order:       vals["date_order"]        = date_order
    order_id = call(models, uid, api_key, "sale.order", "create", [vals])
    for ln in lines:
        line_vals = {
            "order_id":       order_id,
            "name":           ln.get("descripcion") or ln.get("producto") or "Sin descripción",
            "product_uom_qty": _to_float(ln.get("cantidad", 1)),
            "price_unit":     _to_float(ln.get("precio_unit") or ln.get("precio", 0)),
        }
        if ln.get("product_id"):
            line_vals["product_id"] = ln["product_id"]
        elif ln.get("producto"):
            prod_ids = call(models, uid, api_key, "product.product", "search",
                            [[("name", "ilike", ln["producto"])]], {"limit": 1})
            if prod_ids:
                line_vals["product_id"] = prod_ids[0]
        call(models, uid, api_key, "sale.order.line", "create", [line_vals])
    if file_bytes:
        attach_file(models, uid, api_key, "sale.order", order_id, filename, file_bytes, mimetype)
    return order_id

def _to_float(v):
    try:
        return float(str(v).replace(",", "."))
    except Exception:
        return 0.0

def parse_ar_date(raw):
    """Convierte DD/MM/YYYY → YYYY-MM-DD. Retorna '' si no puede parsear."""
    if not raw:
        return ""
    raw = raw.strip()
    m = re.match(r"(\d{1,2})[/\-](\d{1,2})[/\-](\d{4})", raw)
    if m:
        d, mo, y = m.group(1).zfill(2), m.group(2).zfill(2), m.group(3)
        return f"{y}-{mo}-{d}"
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

def fmt_ars(v):
    """Formatea número como moneda ARS: $ 1.234,56"""
    if not v:
        return ""
    try:
        s = "{:,.2f}".format(float(v))
        return "$ " + s.replace(",", "X").replace(".", ",").replace("X", ".")
    except Exception:
        return str(v)

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
            rows = m.execute_kw(ODOO_DB, uid, api_key, "res.partner", "search_read",
                [[("vat", "=", vat_val), ("active", "=", True)]],
                {"fields": ["id", "name", "vat"], "limit": 1})
            if rows:
                return (rows[0]["id"], rows[0]["name"])
        # Fallback: buscar por los últimos 8 dígitos del CUIT
        rows = m.execute_kw(ODOO_DB, uid, api_key, "res.partner", "search_read",
            [[("vat", "ilike", cuit_norm[2:10]), ("active", "=", True)]],
            {"fields": ["id", "name", "vat"], "limit": 3})
        if rows:
            return (rows[0]["id"], rows[0]["name"])
    except Exception:
        pass
    return None

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
        rows = m.execute_kw(ODOO_DB, uid, api_key, "account.move", "search_read",
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

def extract_pdf_fields(file_bytes):
    """
    Parser especializado para facturas electrónicas argentinas (AFIP/CAE/CAEA).
    Extrae: proveedor, número de comprobante, fecha de emisión,
            fecha de vencimiento de pago, total.
    Retorna (fields_dict, raw_text).
    """
    try:
        import pdfplumber
        with pdfplumber.open(BytesIO(file_bytes)) as pdf:
            text = "\n".join(page.extract_text() or "" for page in pdf.pages)
    except Exception:
        return {}, ""
    if not text.strip():
        return {}, ""

    fields = {"numero": "", "fecha": "", "fecha_iso": "", "fecha_vencimiento": "",
              "fecha_vto_iso": "", "proveedor": "", "total": "", "neto": "", "iva": "",
              "cuit": "", "condiciones_venta": "", "dias_pago": None}

    # ── NÚMERO DE COMPROBANTE ─────────────────────────────────────────────
    # Soporta:
    #   Formato AFIP estándar:     "Nro. Comp.: 00002-00013670"
    #   Formato con letra prefijo: "FACTURA A00005-00029174"
    num_pats = [
        r"(?:Nro\.?\s*Comp\.?(?:\s*\(Nro\.?\s*Orig\.?\))?|N[°º]\s*Comp\.?|Comprobante\s*N[°º]?)[:\s]*(\d{4,5}[-\s]\d{6,8})",
        r"(?:Punto\s+de\s+Venta[:\s]+\d+\s+)?(?:Comp\.?\s*Nro\.?|Nro\.)[:\s]+(\d{4,5}-\d{6,8})",
        r"(?:FACTURA|NOTA\s+DE\s+CR[EÉ]DITO|NOTA\s+DE\s+D[EÉ]BITO|RECIBO)\s+([A-Z]\d{4,5}-\d{6,8})",
        r"\b([A-Z]\d{4,5}-\d{6,8})\b",
        r"\b(\d{4,5}-\d{6,8})\b",
        r"(?:Factura|Invoice|N[°º.])[:\s#]*([A-Z0-9\-]{5,20})",
    ]
    for pat in num_pats:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            fields["numero"] = m.group(1).strip()
            break

    # ── FECHA DE EMISIÓN ──────────────────────────────────────────────────
    emision_pats = [
        r"(?:Fecha\s+de\s+[Ee]misi[oó]n|Fecha\s+[Ee]mis\.?)[:\s]+(\d{1,2}/\d{1,2}/\d{4})",
        r"(?:^|\n|\s)(?:FECHA|Fecha)[:\s]+(\d{1,2}/\d{1,2}/\d{4})",
    ]
    for pat in emision_pats:
        m = re.search(pat, text, re.IGNORECASE | re.MULTILINE)
        if m:
            fields["fecha"] = m.group(1).strip()
            fields["fecha_iso"] = parse_ar_date(fields["fecha"])
            break
    if not fields["fecha"]:
        m = re.search(r"(\d{1,2}/\d{1,2}/\d{4})", text)
        if m:
            fields["fecha"] = m.group(1)
            fields["fecha_iso"] = parse_ar_date(fields["fecha"])

    # ── CONDICIONES DE VENTA ──────────────────────────────────────────────
    cond_pats = [
        r"(?:Condici[oó]n(?:es)?\s+de\s+Venta|Cond\.?\s*Vta\.?)[:\s]+([^\n]{3,80})",
    ]
    for pat in cond_pats:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            fields["condiciones_venta"] = m.group(1).strip()
            break

    # ── FECHA DE VENCIMIENTO DE PAGO ─────────────────────────────────────
    # Se calcula desde condiciones de venta (ej: "CUENTA CORRIENTE A 10 DIAS").
    # "Fecha de Vto." en facturas AFIP/CAE es el vencimiento del CAE, NO el de pago.
    cond_text = fields["condiciones_venta"] or text
    fields["dias_pago"] = parse_payment_terms(cond_text)
    if fields["dias_pago"] and fields["fecha_iso"]:
        fields["fecha_vencimiento"] = compute_vencimiento(fields["fecha_iso"], fields["dias_pago"])
        fields["fecha_vto_iso"]     = fields["fecha_vencimiento"]

    # ── IMPORTE TOTAL ─────────────────────────────────────────────────────
    total_pats = [
        r"(?:Importe\s+Total|Total\s+Factura|TOTAL\s+FACTURA)[:\s$]*\$?\s*([\d.,]+)",
        r"(?:^|\n|\s)TOTAL\s*:\s*\$?\s*([\d.,]+)",        # TOTAL: $amount
        r"(?:^|\n|\s)TOTAL\s+\$\s*([\d.,]+)",             # TOTAL $ amount
        r"(?:^|\n|\s)TOTAL\s+([\d.,]+)(?:\s|$)",          # TOTAL amount
        r"(?:^|\n|\s)PESOS\s+TOTAL[:\s$]*\$?\s*([\d.,]+)",
        r"Total\s+a\s+pagar[:\s$]*\$?\s*([\d.,]+)",
    ]
    for pat in total_pats:
        m = re.search(pat, text, re.IGNORECASE | re.MULTILINE)
        if m:
            fields["total"] = normalize_amount(m.group(1).strip())
            break

    # ── NETO GRAVADO ──────────────────────────────────────────────────────
    neto_pats = [
        r"(?:Subtotal\s+Gravado|Neto\s+Gravado|Base\s+Imponible)[:\s$]*\$?\s*([\d.,]+)",
        r"SUBTOTAL\s+\$\s*([\d.,]+)",                      # SUBTOTAL $ amount
        r"SUBTOTAL\s+([\d.,]+)(?:\s|$)",                   # SUBTOTAL amount
        r"(?:Gravado|Subtotal)[:\s$]*\$?\s*([\d.,]+)",
    ]
    for pat in neto_pats:
        m = re.search(pat, text, re.IGNORECASE | re.MULTILINE)
        if m:
            fields["neto"] = normalize_amount(m.group(1).strip())
            break

    # ── IVA ───────────────────────────────────────────────────────────────
    iva_pats = [
        r"IVA\s+(?:21|10[.,]5|27)\s*%\s*\$?\s*([\d.,]+)",  # IVA 21 % $ amount
        r"I\.?V\.?A[^:\n]*(?:21|10\.5|27)[^:\n]*:[:\s$]*\$?\s*([\d.,]+)",
        r"I\.?V\.?A[:\s$%\d.]*:\s*([\d.,]+)",
        r"(?:Impuesto\s+)?IVA[:\s$]*\$?\s*([\d.,]+)",
    ]
    for pat in iva_pats:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            fields["iva"] = normalize_amount(m.group(1).strip())
            break

    # ── RAZÓN SOCIAL / PROVEEDOR EMISOR ──────────────────────────────────
    razon_pats = [
        r"(?:Raz[oó]n\s+[Ss]ocial|Denominaci[oó]n)[:\s]+([^\n\d][^\n]{2,79})",
        r"(?:Apellido\s+y\s+Nombre\s+o\s+Raz[oó]n\s+[Ss]ocial|Nombre\s+y\s+Apellido)[:\s]+([^\n]{3,79})",
    ]
    for pat in razon_pats:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            name = m.group(1).strip()
            name = re.sub(r'\s*\d{2}-\d{8}-\d\s*', '', name).strip()
            if len(name) >= 3:
                fields["proveedor"] = name[:80]
                break

    # Fallback: primera línea significativa que no sea keyword AFIP
    if not fields["proveedor"]:
        skip = {"FACTURA", "NOTA", "RECIBO", "REMITO", "CUIT", "AFIP", "CAE",
                "PUNTO", "FECHA", "IMPORTE", "TOTAL", "VENCIMIENTO", "INGRESOS",
                "IVA", "MONOTRIBUTO", "RESPONSABLE", "INSCRIPTO", "ORIGINAL",
                "DUPLICADO", "TRIPLICADO", "CÓDIGO", "DOMICILIO", "PROVINCIA",
                "CODIGO", "SUBTOTAL", "DESCRIPCION", "CANTIDAD", "PRECIO",
                "CONDICION", "COMPROBANTE", "PESOS", "SON"}
        for line in (l.strip() for l in text.split("\n") if l.strip()):
            if len(line) < 4 or re.match(r'^[\d$.,/\s\-()]+$', line):
                continue
            upper = line.upper()
            if any(w in upper for w in skip):
                continue
            # Descartar líneas que son claramente el número de comprobante
            if re.match(r'^[A-Z]\d{4,5}-\d{6,8}$', line):
                continue
            fields["proveedor"] = line[:80]
            break

    # ── CUIT EMISOR ───────────────────────────────────────────────────────
    # Toma el primer CUIT encontrado en el documento (suele ser el emisor)
    cuit_m = re.search(r'(?:CUIT|C\.U\.I\.T)[:\s.]*(\d{2}[-\s]?\d{8}[-\s]?\d)', text, re.IGNORECASE)
    if cuit_m:
        fields["cuit"] = re.sub(r'[\s\-]', '', cuit_m.group(1))

    return fields, text

@st.cache_data(ttl=600, show_spinner=False)
def get_all_payment_terms(models_url, uid, api_key):
    """Retorna lista de (id, name) de plazos de pago activos en Odoo."""
    try:
        m = xmlrpc.client.ServerProxy(models_url, allow_none=True)
        rows = m.execute_kw(ODOO_DB, uid, api_key, "account.payment.term", "search_read",
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
        rows = m.execute_kw(ODOO_DB, uid, api_key, "res.partner", "read",
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
def search_product_by_code_or_name(models_url, uid, api_key, code="", name_keywords="", limit=3):
    """
    Busca producto en Odoo por código (default_code) y/o palabras clave del nombre.
    Retorna lista de dicts con id, name, default_code, standard_price, list_price.
    """
    try:
        m = xmlrpc.client.ServerProxy(models_url, allow_none=True)
        fields = ["id", "name", "default_code", "standard_price", "list_price"]
        # 1. Exact code match
        if code and code.strip():
            rows = m.execute_kw(ODOO_DB, uid, api_key, "product.product", "search_read",
                [[("default_code", "=", code.strip()), ("active", "=", True)]],
                {"fields": fields, "limit": 1})
            if rows:
                return rows
            # ilike fallback
            rows = m.execute_kw(ODOO_DB, uid, api_key, "product.product", "search_read",
                [[("default_code", "ilike", code.strip()), ("active", "=", True)]],
                {"fields": fields, "limit": limit})
            if rows:
                return rows
        # 2. Name keywords (primeras 3 palabras significativas)
        if name_keywords and name_keywords.strip():
            keywords = [w for w in name_keywords.strip().split() if len(w) >= 3][:3]
            if keywords:
                domain = [("active", "=", True)]
                for kw in keywords:
                    domain.append(("name", "ilike", kw))
                rows = m.execute_kw(ODOO_DB, uid, api_key, "product.product", "search_read",
                    [domain], {"fields": fields, "limit": limit})
                if rows:
                    return rows
                # fallback: solo primera keyword
                rows = m.execute_kw(ODOO_DB, uid, api_key, "product.product", "search_read",
                    [[("active", "=", True), ("name", "ilike", keywords[0])]],
                    {"fields": fields, "limit": limit})
                if rows:
                    return rows
    except Exception:
        pass
    return []

def create_partner(models, uid, api_key, name, vat, street="", phone="", email_addr=""):
    """Crea un nuevo cliente en Odoo y retorna su ID."""
    vals = {"name": name, "customer_rank": 1, "is_company": True}
    if vat:        vals["vat"]    = vat
    if street:     vals["street"] = street
    if phone:      vals["phone"]  = phone
    if email_addr: vals["email"]  = email_addr
    return call(models, uid, api_key, "res.partner", "create", [vals])

def extract_oc_fields(file_bytes):
    """
    Parser para Órdenes de Compra de clientes (formato heterogéneo).
    Extrae: CUIT del emisor (cliente/comprador), número OC, fecha, condiciones
    de pago, líneas de productos (código, descripción, qty, precio_unit, iva%,
    subtotal), y totales (neto, IVA 21%, IVA 10.5%, total OC).
    Retorna (fields_dict, all_tables, raw_text).
    """
    try:
        import pdfplumber
        with pdfplumber.open(BytesIO(file_bytes)) as pdf:
            pages_text = [page.extract_text() or "" for page in pdf.pages]
            text = "\n".join(pages_text)
            all_tables = []
            for page in pdf.pages:
                tbls = page.extract_tables()
                if tbls:
                    all_tables.extend(tbls)
    except Exception:
        return {}, [], ""
    if not text.strip():
        return {}, [], ""

    fields = {
        "cuit": "", "numero_oc": "", "fecha": "", "fecha_iso": "",
        "condiciones_pago": "", "dias_pago": None,
        "lineas": [],
        "subtotal_neto": "", "iva_21": "", "iva_105": "", "total": "",
    }

    # ── CUIT emisor ──────────────────────────────────────────────────────
    cuits_found = re.findall(r'\b(\d{2}-\d{8}-\d)\b', text)
    if not cuits_found:
        cuits_found = re.findall(r'\bCUIT[:\s.]*(\d{11})\b', text, re.IGNORECASE)
    if cuits_found:
        fields["cuit"] = re.sub(r'[-\s]', '', cuits_found[0])

    # ── Número de OC ─────────────────────────────────────────────────────
    oc_pats = [
        r"(?:Orden\s+de\s+[Cc]ompra|O\.?C\.?|ORDEN\s+DE\s+COMPRA)\s*[N°#:Nro.]*\s*([0-9]{4}[-/][0-9]{4,})",
        r"(?:N[°º]\s*(?:de\s+)?[Oo]rden|Pedido\s+N[°º])[:\s]*([0-9]{4}[-/][0-9]{4,})",
        r"\b(0{4}[-/]\d{4,})\b",
    ]
    for pat in oc_pats:
        mo = re.search(pat, text, re.IGNORECASE)
        if mo:
            fields["numero_oc"] = mo.group(1).strip()
            break

    # ── Fecha ─────────────────────────────────────────────────────────────
    date_pats = [
        r"(?:Fecha|FECHA|Date)[:\s]+(\d{1,2}/\d{1,2}/\d{4})",
        r"(?:^|\s)(\d{1,2}/\d{1,2}/\d{4})(?:\s|$)",
    ]
    for pat in date_pats:
        mo = re.search(pat, text, re.IGNORECASE | re.MULTILINE)
        if mo:
            fields["fecha"] = mo.group(1).strip()
            fields["fecha_iso"] = parse_ar_date(fields["fecha"])
            break

    # ── Condiciones de pago ───────────────────────────────────────────────
    cond_pats = [
        r"(?:Condici[oó]n(?:es)?\s+de\s+[Pp]ago|Forma\s+de\s+[Pp]ago)[:\s]+([^\n]{3,80})",
        r"(CUENTA\s+CORRIENTE[^\n]{0,60})",
    ]
    for pat in cond_pats:
        mo = re.search(pat, text, re.IGNORECASE)
        if mo:
            fields["condiciones_pago"] = mo.group(1).strip()
            break

    # Días: buscar "Intervalo: 30" primero, luego parse genérico
    intervalo_mo = re.search(r"[Ii]ntervalo[:\s]+(\d+)", text)
    if intervalo_mo:
        fields["dias_pago"] = int(intervalo_mo.group(1))
    else:
        fields["dias_pago"] = parse_payment_terms(fields["condiciones_pago"] or text)

    # ── Totales ───────────────────────────────────────────────────────────
    mo = re.search(r"(?:Sub[-\s]?[Tt]otal\s+[Nn]eto|SUBTOTAL\s+NETO)[:\s$]*\$?\s*([\d.,]+)", text, re.IGNORECASE)
    if mo:
        fields["subtotal_neto"] = normalize_amount(mo.group(1))
    mo = re.search(r"IVA\s+21\s*%[:\s$]*\$?\s*([\d.,]+)", text, re.IGNORECASE)
    if mo:
        fields["iva_21"] = normalize_amount(mo.group(1))
    mo = re.search(r"IVA\s+10[.,]5\s*%[:\s$]*\$?\s*([\d.,]+)", text, re.IGNORECASE)
    if mo:
        fields["iva_105"] = normalize_amount(mo.group(1))
    mo = re.search(r"(?:Total\s+OC|TOTAL\s+OC|Total\s+[Oo]rden)[:\s$]*\$?\s*([\d.,]+)", text, re.IGNORECASE)
    if not mo:
        mo = re.search(r"(?:^|\n)\s*TOTAL[:\s$]*\$?\s*([\d.,]+)\s*(?:$|\n)", text, re.IGNORECASE | re.MULTILINE)
    if mo:
        fields["total"] = normalize_amount(mo.group(1))

    # ── Líneas de productos (desde tablas pdfplumber) ─────────────────────
    def _try_num(s):
        s = str(s or "").strip()
        if not s or not re.search(r'\d', s):
            return None
        try:
            return float(normalize_amount(s))
        except Exception:
            return None

    for table in all_tables:
        if not table or len(table) < 2:
            continue
        header = [str(c or "").strip().lower() for c in table[0]]
        col_map = {}
        for i, h in enumerate(header):
            if re.search(r"c[oó]d|sku|art[ií]culo|codigo", h) and "codigo" not in col_map:
                col_map["codigo"] = i
            elif re.search(r"cant|qty|cantidad", h) and "cantidad" not in col_map:
                col_map["cantidad"] = i
            elif re.search(r"detal|descri|product|nombre|item", h) and "descripcion" not in col_map:
                col_map["descripcion"] = i
            elif re.search(r"\bneto\b|p\.?\s*unit|precio\s*unit|unitario|unit\s*price", h) and "precio_unit" not in col_map:
                col_map["precio_unit"] = i
            elif re.search(r"\biva\b|tax|%\s*iva|alicuota", h) and "iva_pct" not in col_map:
                col_map["iva_pct"] = i
            elif re.search(r"sub.?total|importe|c\.?final|precio\s*final", h) and "subtotal" not in col_map:
                col_map["subtotal"] = i
        if not col_map or ("descripcion" not in col_map and "codigo" not in col_map):
            continue
        for row in table[1:]:
            if not row or all(not c for c in row):
                continue
            def _gcol(key, default=""):
                idx = col_map.get(key)
                if idx is None or idx >= len(row):
                    return default
                return str(row[idx] or "").strip()
            desc = _gcol("descripcion")
            cod  = _gcol("codigo")
            if not desc and not cod:
                continue
            if re.match(r'^(detalle|descripci[oó]n|producto|item|total|subtotal)$', desc, re.IGNORECASE):
                continue
            qty   = _try_num(_gcol("cantidad"))
            price = _try_num(_gcol("precio_unit"))
            sub   = _try_num(_gcol("subtotal"))
            iva   = _try_num(_gcol("iva_pct"))
            if qty is None and price is None and sub is None:
                continue
            fields["lineas"].append({
                "codigo":      cod,
                "descripcion": desc,
                "cantidad":    qty   if qty   is not None else 0,
                "precio_unit": price if price is not None else 0,
                "iva_pct":     iva   if iva   is not None else 21.0,
                "subtotal":    sub   if sub   is not None else (
                                   (qty * price) if (qty and price) else 0),
            })

    return fields, all_tables, text


def classify_document(text):
    tu = text.upper()
    rules = [
        ("PETDUR",       {"tipo":"petdur",  "label":"Bill PETDUR (Etapa 1)",        "partner_id":49328,"journal_id":71,"doc_type":None}),
        ("DECLARACI",    {"tipo":"di_afip", "label":"DI AFIP (Etapa 2)",            "partner_id":9,    "journal_id":10,"doc_type":66}),
        ("33693450239",  {"tipo":"di_afip", "label":"DI AFIP (Etapa 2)",            "partner_id":9,    "journal_id":10,"doc_type":66}),
        ("TRICE",        {"tipo":"nac",     "label":"Bill TRICE (Etapa 2a)",        "partner_id":48825,"journal_id":10,"doc_type":None}),
        ("TERMINAL 4",   {"tipo":"nac",     "label":"Bill Terminal 4 (Etapa 2a)",   "partner_id":48828,"journal_id":10,"doc_type":None}),
        ("MUNDO COMEX",  {"tipo":"nac",     "label":"Bill Mundo Comex (Etapa 2a)",  "partner_id":48826,"journal_id":10,"doc_type":None}),
        ("SENASA",       {"tipo":"nac",     "label":"Bill SENASA (Etapa 2a)",       "partner_id":48827,"journal_id":10,"doc_type":None}),
    ]
    for keyword, cfg in rules:
        if keyword in tu:
            return cfg
    return {"tipo":"other","label":"Otro comprobante","partner_id":None,"journal_id":10,"doc_type":None}


# ───────────────────────────────────────────────────
# SESSION STATE
# ───────────────────────────────────────────────────
DEFAULTS = {
    "logged_in": False,
    "user_email": "",
    "history": [],
    "carpeta_id": "", "carpeta_po": None, "carpeta_bills": [], "carpeta_lc_id": None,
    "etapas": {k: False for k, *_ in ETAPAS_DEF},
}
for k, v in DEFAULTS.items():
    if k not in st.session_state:
        st.session_state[k] = v


# ═══════════════════════════════════════════════════
# SIDEBAR
# ═══════════════════════════════════════════════════
with st.sidebar:
    import os
    if os.path.exists("logo.png"):
        st.image("logo.png", width=180)
    else:
        st.markdown("""<div class="lumi-sidebar-logo">
  <span class="lumi-logo-dot">🛒</span>
  <span class="lumi-logo-text">LUMINATEC</span>
</div>""", unsafe_allow_html=True)
    st.markdown("---")

    if not st.session_state.logged_in:
        st.markdown("### 🔐 Iniciar sesión")
        st.caption(f"`{ODOO_URL}`")
        with st.form("login_form"):
            email_in = st.text_input("Email", placeholder="tu@luminatec.com")
            pass_in  = st.text_input("Contraseña", type="password", placeholder="••••••••")
            login_btn = st.form_submit_button("Ingresar", use_container_width=True)
        if login_btn:
            if email_in and pass_in:
                with st.spinner("Verificando..."):
                    ok, err_msg = verify_user(email_in, pass_in)
                if ok:
                    st.session_state.logged_in  = True
                    st.session_state.user_email = email_in.strip().lower()
                    st.rerun()
                else:
                    st.error(err_msg or "Email o contraseña incorrectos.")
            else:
                st.warning("Completá email y contraseña.")
    else:
        is_admin = st.session_state.user_email in ADMIN_EMAILS
        st.success("✅ Sesión activa")
        st.markdown(
            '<span class="user-chip">👤 ' + st.session_state.user_email + '</span>',
            unsafe_allow_html=True
        )
        if is_admin:
            st.markdown('<span class="admin-badge">🛳️ Importaciones habilitado</span>',
                        unsafe_allow_html=True)
            st.caption(f"Carpeta: **{st.session_state.carpeta_id or 'sin selección'}**")
        if st.button("🔓 Cerrar sesión", use_container_width=True):
            st.session_state.logged_in  = False
            st.session_state.user_email = ""
            st.session_state.history    = []
            st.session_state.carpeta_id = ""
            st.session_state.carpeta_po = None
            st.session_state.carpeta_bills = []
            st.session_state.carpeta_lc_id = None
            st.session_state.etapas = {k: False for k, *_ in ETAPAS_DEF}
            st.rerun()

    st.divider()
    st.caption(f"Base de datos: `{ODOO_DB}`")


# ═══════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════
st.markdown('<h1 class="main-title">🛒 <span>LUMINA</span>TEC · Carga Odoo</h1>', unsafe_allow_html=True)
st.caption("Facturas de proveedores, pedidos de clientes e importaciones — todo en un lugar.")

if not st.session_state.logged_in:
    st.info("👈 Iniciá sesión desde el panel lateral para empezar.")
    st.stop()

# Conexión de servicio (API key central)
try:
    svc_uid, svc_models, svc_api_key = get_service_connection()
except Exception as _conn_err:
    st.error(f"⚠️ No se pudo conectar a Odoo: {_conn_err}")
    st.stop()

uid        = svc_uid
models     = svc_models
api_key    = svc_api_key
models_url = f"{ODOO_URL}/xmlrpc/2/object"

is_admin = st.session_state.user_email in ADMIN_EMAILS

_tabs = ["🧾 Facturas de proveedores", "📦 Pedidos de clientes"]
if is_admin:
    _tabs.append("🛳️ Importaciones")
_tabs.append("📋 Historial de sesión")
_tab_objs = st.tabs(_tabs)

if is_admin:
    tab_bills, tab_orders, tab_import, tab_history = _tab_objs
else:
    tab_bills, tab_orders, tab_history = _tab_objs
    tab_import = None


# ═══════════════════════════════════════════════════
# TAB 1 — FACTURAS DE PROVEEDORES
# ═══════════════════════════════════════════════════
with tab_bills:
    st.subheader("Facturas de proveedores")
    files = st.file_uploader("Arrastrá o elegí archivos (PDF, JPG, PNG, XLSX)",
        type=["pdf","jpg","jpeg","png","xlsx","xls"], accept_multiple_files=True, key="bills_upload")
    if not files:
        st.caption("Subí uno o más archivos para empezar.")
    for uf in (files or []):
        st.divider()
        ext        = uf.name.rsplit(".", 1)[-1].lower()
        file_bytes = uf.read()
        mimetype   = MIMETYPES.get(ext, "application/octet-stream")
        st.markdown(f"**📎 {uf.name}**  `{ext.upper()}`  ({len(file_bytes)//1024} KB)")
        if ext in ("xlsx", "xls"):
            try:
                df = pd.read_excel(BytesIO(file_bytes), dtype=str).fillna("")
                df.columns = [c.strip() for c in df.columns]
            except Exception as e:
                st.error(f"No se pudo leer el Excel: {e}"); continue
            st.caption(f"📊 {len(df)} filas · Columnas: {', '.join(df.columns)}")
            st.dataframe(df.head(10), use_container_width=True, height=180)
            cols_opts = ["(ninguna)"] + list(df.columns)
            c1, c2, c3, c4 = st.columns(4)
            col_prov  = c1.selectbox("Proveedor",    cols_opts, key=f"bp_{uf.name}")
            col_fecha = c2.selectbox("Fecha",         cols_opts, key=f"bf_{uf.name}")
            col_ref   = c3.selectbox("N° Factura",   cols_opts, key=f"br_{uf.name}")
            col_total = c4.selectbox("Total (info)",  cols_opts, key=f"bt_{uf.name}")
            if st.button(f"⬆️ Cargar {len(df)} facturas en Odoo", key=f"load_bills_xls_{uf.name}"):
                bar = st.progress(0)
                ok, errs = 0, []
                for i, row in df.iterrows():
                    try:
                        prov_name  = row.get(col_prov, "")  if col_prov  != "(ninguna)" else ""
                        fecha      = row.get(col_fecha, "") if col_fecha != "(ninguna)" else ""
                        ref        = row.get(col_ref, "")   if col_ref   != "(ninguna)" else ""
                        partner_id = False
                        if prov_name:
                            m2 = search_partners(models_url, uid, api_key, prov_name, limit=1)
                            partner_id = m2[0][0] if m2 else False
                        move_id = create_vendor_bill(models, uid, api_key,
                            partner_id=partner_id, ref=str(ref),
                            invoice_date=str(fecha) if fecha else False,
                            filename=f"{uf.name}_fila{i+1}.pdf",
                            file_bytes=file_bytes, mimetype=mimetype)
                        ok += 1
                        url = f"{ODOO_URL}/web#id={move_id}&model=account.move&view_type=form"
                        st.session_state.history.append({"tipo":"Factura proveedor",
                            "archivo":f"{uf.name}·fila{i+1}","id":move_id,"url":url,"estado":"✅"})
                    except Exception as e:
                        errs.append(f"Fila {i+1}: {str(e)[:100]}")
                    bar.progress((i+1)/len(df))
                if ok: st.success(f"✅ {ok} de {len(df)} facturas creadas en Odoo.")
                for err in errs: st.warning(err)
        else:
            extracted, raw_text = {}, ""
            if ext == "pdf":
                with st.spinner("Leyendo PDF..."):
                    extracted, raw_text = extract_pdf_fields(file_bytes)
                st.caption("🤖 Datos detectados — revisá antes de confirmar." if extracted.get("proveedor")
                           else "ℹ️ PDF sin texto extraíble. Completá los datos a mano.")
            elif ext in ("jpg","jpeg","png"):
                st.image(file_bytes, caption="Vista previa", width=380)

            # Cargar cuentas contables (cacheado)
            _bill_accounts = get_all_accounts(models_url, uid, api_key)
            _acct_labels   = ["— Sin cuenta —"] + [lbl for _, lbl in _bill_accounts]

            # ── Pre-lookup de proveedor por CUIT (fuera del form, en tiempo real) ─
            _cuit_raw    = extracted.get("cuit", "")
            _cond_venta  = extracted.get("condiciones_venta", "")
            _dias_pago   = extracted.get("dias_pago")
            _vto_auto    = extracted.get("fecha_vencimiento", "")

            _partner_preloaded = None
            if _cuit_raw:
                _partner_preloaded = search_partner_by_cuit(models_url, uid, api_key, _cuit_raw)

            if _partner_preloaded:
                st.info(f"🏢 Proveedor detectado por CUIT: **{_partner_preloaded[1]}**")
            elif _cuit_raw:
                odoo_new_url = f"{ODOO_URL}/web#action=base.action_res_partner_form&view_type=form"
                st.warning(
                    f"⚠️ CUIT **{_cuit_raw}** no encontrado en Odoo. "
                    f"[Crear proveedor manualmente]({odoo_new_url}) antes de cargar."
                )

            if _cond_venta:
                if _dias_pago:
                    st.caption(f"📅 Condición de venta: **{_cond_venta}** → vencimiento calculado: `{_vto_auto}`")
                else:
                    st.caption(f"📅 Condición de venta: **{_cond_venta}** (sin días detectados — completá el vencimiento a mano)")

            # ── Chequeo de duplicado en tiempo real ───────────────────────────────
            _num_raw = extracted.get("numero","")
            _dup_exists, _dup_id, _dup_name = False, None, None
            if _num_raw:
                _dup_exists, _dup_id, _dup_name = check_invoice_exists(models_url, uid, api_key, _num_raw)
            if _dup_exists:
                _dup_url = f"{ODOO_URL}/web#id={_dup_id}&model=account.move&view_type=form"
                st.error(
                    f"🚫 Esta factura **ya fue cargada** en Odoo ({_dup_name}). "
                    f"[Ver factura existente]({_dup_url})"
                )

            with st.form(key=f"bill_form_{uf.name}"):
                c1, c2 = st.columns(2)
                cuit_i  = c1.text_input("CUIT del proveedor",
                            value=_cuit_raw,
                            placeholder="30-12345678-9",
                            help="El sistema buscará al proveedor por CUIT en Odoo")
                ref_i   = c2.text_input("N° de factura",
                            value=extracted.get("numero",""))
                fecha_i = c1.text_input("Fecha emisión (AAAA-MM-DD)",
                            value=extracted.get("fecha_iso",""),
                            placeholder="2026-05-12")
                fecha_vto_i = c2.text_input("Fecha vencimiento (AAAA-MM-DD)",
                            value=_vto_auto,
                            placeholder="2026-05-20",
                            help="Se calcula automáticamente si se detectan días en las condiciones de venta")

                # Proveedor por nombre como fallback si no hay CUIT
                prov_i = st.text_input("Nombre del proveedor (fallback si no hay CUIT)",
                            value="" if _partner_preloaded else extracted.get("proveedor","")[:60],
                            placeholder="Nombre exacto en Odoo",
                            help="Se usa solo si el CUIT no resuelve a ningún proveedor")

                # Montos extraídos (sólo referencia)
                _ca, _cb, _cc = st.columns(3)
                _ca.text_input("Neto gravado (ref.)",
                    value=fmt_ars(extracted.get("neto","")), disabled=True,
                    key=f"neto_ref_{uf.name}")
                _cb.text_input("IVA (ref.)",
                    value=fmt_ars(extracted.get("iva","")), disabled=True,
                    key=f"iva_ref_{uf.name}")
                _cc.text_input("Total c/imp. (ref.)",
                    value=fmt_ars(extracted.get("total","")), disabled=True,
                    key=f"total_ref_{uf.name}")

                st.text_area("Notas internas", height=55, key=f"notas_{uf.name}")

                # Cuentas contables
                st.markdown("##### 📒 Cuenta contable")
                cuenta_sel = st.selectbox(
                    "Cuenta de gasto / activo",
                    options=_acct_labels,
                    index=0,
                    key=f"cta_g_{uf.name}",
                    help="La cuenta se pre-selecciona según el proveedor. Cambiala solo si esta operación usa una cuenta distinta a la habitual.",
                )

                _btn_label = "⬆️ Cargar en Odoo"
                if _dup_exists:
                    _btn_label = "⚠️ Ya existe — Cargar igual"
                go = st.form_submit_button(_btn_label, use_container_width=True)

            # Asiento estimado — visible siempre que haya montos
            _neto_f = extracted.get("neto","")
            _iva_f  = extracted.get("iva","")
            _tot_f  = extracted.get("total","")
            if _neto_f or _tot_f:
                _cuenta_disp = (cuenta_sel if (cuenta_sel and cuenta_sel != "— Sin cuenta —")
                                else "*(cuenta de gasto — seleccioná arriba)*")
                st.markdown("**📒 Asiento estimado en Odoo:**")
                st.markdown(
                    f"| Cuenta | Debe | Haber |\n"
                    f"|---|---|---|\n"
                    f"| {_cuenta_disp} | {fmt_ars(_neto_f)} | |\n"
                    f"| IVA Crédito Fiscal (si aplica) | {fmt_ars(_iva_f)} | |\n"
                    f"| Proveedor (por pagar) | | {fmt_ars(_tot_f)} |"
                )

            if go:
                with st.spinner("Procesando..."):
                    try:
                        partner_id = False
                        # 1. Buscar por CUIT ingresado en el form
                        if cuit_i and cuit_i.strip():
                            _found = search_partner_by_cuit(models_url, uid, api_key, cuit_i.strip())
                            if _found:
                                partner_id = _found[0]
                                st.caption(f"Proveedor por CUIT: {_found[1]}")
                        # 2. Fallback: buscar por nombre
                        if not partner_id and prov_i and prov_i.strip():
                            m2 = search_partners(models_url, uid, api_key, prov_i.strip(), limit=3)
                            if m2:
                                partner_id = m2[0][0]
                                st.caption(f"Proveedor por nombre: {m2[0][1]}")
                            else:
                                st.warning(f"'{prov_i}' no encontrado — se creará sin proveedor asignado.")

                        # 3. Chequeo de duplicado en el submit (segunda línea de defensa)
                        if ref_i and ref_i.strip():
                            _dup2, _dup2_id, _dup2_name = check_invoice_exists(
                                models_url, uid, api_key, ref_i.strip())
                            if _dup2:
                                _dup2_url = f"{ODOO_URL}/web#id={_dup2_id}&model=account.move&view_type=form"
                                st.error(
                                    f"🚫 La factura **{ref_i}** ya existe en Odoo ({_dup2_name}). "
                                    f"[Ver factura existente]({_dup2_url})"
                                )
                                st.stop()

                        # 4. Resolver cuenta seleccionada
                        account_id_sel = None
                        if cuenta_sel and cuenta_sel != "— Sin cuenta —":
                            for _aid, _albl in _bill_accounts:
                                if _albl == cuenta_sel:
                                    account_id_sel = _aid
                                    break
                        move_id = create_vendor_bill(models, uid, api_key,
                            partner_id=partner_id, ref=ref_i,
                            invoice_date=fecha_i or False,
                            invoice_date_due=fecha_vto_i or None,
                            filename=uf.name, file_bytes=file_bytes, mimetype=mimetype,
                            account_id=account_id_sel,
                            amount_neto=extracted.get("neto") or None)
                        url = f"{ODOO_URL}/web#id={move_id}&model=account.move&view_type=form"
                        st.success(f"✅ Factura creada — [Abrir en Odoo]({url})")
                        st.session_state.history.append({"tipo":"Factura proveedor",
                            "archivo":uf.name,"id":move_id,"url":url,"estado":"✅"})
                    except Exception as e:
                        st.error(f"❌ {e}")


# ═══════════════════════════════════════════════════
# TAB 2 — PEDIDOS DE CLIENTES
# ═══════════════════════════════════════════════════
with tab_orders:
    st.subheader("Pedidos de clientes")
    files_o = st.file_uploader("Arrastrá o elegí archivos (PDF, JPG, PNG, XLSX)",
        type=["pdf","jpg","jpeg","png","xlsx","xls"], accept_multiple_files=True, key="orders_upload")
    if not files_o:
        st.caption("Subí uno o más archivos para empezar.")
    for uf in (files_o or []):
        st.divider()
        ext        = uf.name.rsplit(".", 1)[-1].lower()
        file_bytes = uf.read()
        mimetype   = MIMETYPES.get(ext, "application/octet-stream")
        st.markdown(f"**📎 {uf.name}**  `{ext.upper()}`  ({len(file_bytes)//1024} KB)")
        if ext in ("xlsx","xls"):
            try:
                df = pd.read_excel(BytesIO(file_bytes), dtype=str).fillna("")
                df.columns = [c.strip() for c in df.columns]
            except Exception as e:
                st.error(f"No se pudo leer el Excel: {e}"); continue
            st.caption(f"📊 {len(df)} filas · Columnas: {', '.join(df.columns)}")
            st.dataframe(df.head(10), use_container_width=True, height=180)
            cols_opts = ["(ninguna)"] + list(df.columns)
            c1, c2, c3, c4 = st.columns(4)
            col_cli   = c1.selectbox("Cliente",  cols_opts, key=f"oc_{uf.name}")
            col_prod  = c2.selectbox("Producto", cols_opts, key=f"op_{uf.name}")
            col_qty   = c3.selectbox("Cantidad", cols_opts, key=f"oq_{uf.name}")
            col_price = c4.selectbox("Precio",   cols_opts, key=f"opr_{uf.name}")
            if col_cli == "(ninguna)":
                st.warning("Seleccioná al menos la columna de Cliente.")
            elif st.button("⬆️ Cargar pedidos en Odoo", key=f"load_orders_xls_{uf.name}"):
                clientes = df[col_cli].unique()
                bar = st.progress(0)
                ok, errs = 0, []
                for i, cliente in enumerate(clientes):
                    try:
                        rows = df[df[col_cli] == cliente]
                        matches = search_partners(models_url, uid, api_key, str(cliente), limit=1)
                        if not matches:
                            errs.append(f"Cliente '{cliente}' no encontrado."); continue
                        partner_id = matches[0][0]
                        lines = []
                        for _, row in rows.iterrows():
                            lines.append({
                                "producto": row.get(col_prod,"") if col_prod  != "(ninguna)" else "",
                                "cantidad": row.get(col_qty, 1)  if col_qty   != "(ninguna)" else 1,
                                "precio":   row.get(col_price,0) if col_price != "(ninguna)" else 0,
                            })
                        order_id = create_sale_order(models, uid, api_key,
                            partner_id=partner_id, note=f"Importado desde {uf.name}",
                            lines=lines, filename=uf.name, file_bytes=file_bytes, mimetype=mimetype)
                        ok += 1
                        url = f"{ODOO_URL}/web#id={order_id}&model=sale.order&view_type=form"
                        st.success(f"✅ Pedido de {cliente} creado — [Abrir en Odoo]({url})")
                        st.session_state.history.append({"tipo":"Pedido cliente",
                            "archivo":f"{uf.name}·{cliente}","id":order_id,"url":url,"estado":"✅"})
                    except Exception as e:
                        errs.append(f"Cliente '{cliente}': {str(e)[:100]}")
                    bar.progress((i+1)/len(clientes))
                if ok: st.success(f"✅ {ok} pedidos creados.")
                for err in errs: st.warning(err)
        else:
            # ── Parseo del PDF de OC ──────────────────────────────────────
            oc_fields, _oc_tables, _oc_raw = {}, [], ""
            if ext == "pdf":
                with st.spinner("Leyendo OC..."):
                    oc_fields, _oc_tables, _oc_raw = extract_oc_fields(file_bytes)
            elif ext in ("jpg","jpeg","png"):
                st.image(file_bytes, caption="Vista previa", width=380)

            # ── Session state para partner de esta OC ─────────────────────
            _ss_pid   = f"oc_pid_{uf.name}"
            _ss_pname = f"oc_pname_{uf.name}"
            if _ss_pid not in st.session_state:
                st.session_state[_ss_pid] = None
            if _ss_pname not in st.session_state:
                st.session_state[_ss_pname] = ""

            # ── SECCIÓN 1: CLIENTE ────────────────────────────────────────
            st.markdown("##### 🏢 Cliente")
            _oc_cuit = oc_fields.get("cuit","")

            # Lookup por CUIT si todavía no tenemos partner resuelto
            if _oc_cuit and not st.session_state[_ss_pid]:
                _partner_oc = search_partner_by_cuit(models_url, uid, api_key, _oc_cuit)
                if _partner_oc:
                    st.session_state[_ss_pid]   = _partner_oc[0]
                    st.session_state[_ss_pname] = _partner_oc[1]

            _partner_id_oc   = st.session_state[_ss_pid]
            _partner_name_oc = st.session_state[_ss_pname]

            if _partner_id_oc:
                st.success(f"✅ Cliente identificado por CUIT **{_oc_cuit}**: **{_partner_name_oc}**")
                _pt_id, _pt_name = get_customer_payment_terms(models_url, uid, api_key, _partner_id_oc)
            else:
                if _oc_cuit:
                    st.warning(f"⚠️ CUIT **{_oc_cuit}** no encontrado en Odoo.")
                else:
                    st.warning("⚠️ No se detectó CUIT en el documento. Completá los datos del cliente.")
                _pt_id, _pt_name = None, None

                with st.expander("➕ Crear nuevo cliente en Odoo", expanded=True):
                    _nc1, _nc2 = st.columns(2)
                    nc_name   = _nc1.text_input("Razón social *", key=f"nc_name_{uf.name}")
                    nc_cuit   = _nc2.text_input("CUIT *", value=_oc_cuit,
                                    key=f"nc_cuit_{uf.name}", placeholder="30-12345678-9")
                    nc_street = _nc1.text_input("Dirección", key=f"nc_street_{uf.name}")
                    nc_phone  = _nc2.text_input("Teléfono", key=f"nc_phone_{uf.name}")
                    nc_email  = st.text_input("Email", key=f"nc_email_{uf.name}")
                    if st.button("Crear cliente en Odoo", key=f"btn_nc_{uf.name}"):
                        if nc_name and nc_cuit:
                            try:
                                _new_pid = create_partner(models, uid, api_key,
                                    nc_name, nc_cuit, nc_street, nc_phone, nc_email)
                                st.session_state[_ss_pid]   = _new_pid
                                st.session_state[_ss_pname] = nc_name
                                st.success(f"✅ Cliente **{nc_name}** creado en Odoo (ID {_new_pid})")
                                st.rerun()
                            except Exception as _e:
                                st.error(f"❌ {_e}")
                        else:
                            st.warning("Razón social y CUIT son obligatorios.")

            # ── SECCIÓN 2: DATOS DE LA OC ─────────────────────────────────
            st.markdown("##### 📋 Datos de la Orden de Compra")
            _oc1, _oc2, _oc3 = st.columns(3)
            _oc_num_i  = _oc1.text_input("N° OC",   value=oc_fields.get("numero_oc",""),
                                          key=f"ocnum_{uf.name}")
            _oc_fec_i  = _oc2.text_input("Fecha",   value=oc_fields.get("fecha_iso",""),
                                          key=f"ocfec_{uf.name}", placeholder="AAAA-MM-DD")
            _oc_cond_i = _oc3.text_input("Condición de pago",
                                          value=oc_fields.get("condiciones_pago",""),
                                          key=f"occond_{uf.name}")

            # ── SECCIÓN 3: PRODUCTOS ──────────────────────────────────────
            st.markdown("##### 📦 Productos")
            _lineas_oc = oc_fields.get("lineas", [])
            _enriched  = []

            if _lineas_oc:
                for _ln in _lineas_oc:
                    _prods = search_product_by_code_or_name(
                        models_url, uid, api_key,
                        code=_ln.get("codigo",""),
                        name_keywords=_ln.get("descripcion",""),
                        limit=1,
                    )
                    _op    = _prods[0] if _prods else None
                    _cost  = float(_op["standard_price"]) if _op else 0.0
                    _price = float(_ln.get("precio_unit") or 0)
                    _margin = ((_price - _cost) / _price * 100) if _price > 0 else 0.0
                    _enriched.append({**_ln,
                        "odoo_product": _op,
                        "cost": _cost,
                        "margin_pct": _margin,
                    })

                _df_rows = []
                for _el in _enriched:
                    _df_rows.append({
                        "Código":        _el.get("codigo",""),
                        "Descripción":   _el.get("descripcion",""),
                        "Cant.":         _el.get("cantidad",0),
                        "Precio unit.":  fmt_ars(_el.get("precio_unit",0)),
                        "IVA %":         _el.get("iva_pct","21"),
                        "Subtotal":      fmt_ars(_el.get("subtotal",0)),
                        "Costo Odoo":    fmt_ars(_el.get("cost",0)),
                        "Margen %":      f"{_el.get('margin_pct',0):.1f}%",
                        "Match Odoo":    (_el["odoo_product"]["name"]
                                          if _el.get("odoo_product") else "⚠️ Sin match"),
                    })
                st.dataframe(pd.DataFrame(_df_rows), use_container_width=True, hide_index=True)
            else:
                st.info("No se detectaron líneas de productos automáticamente.")

            # Cálculo de totales desde líneas enriquecidas
            _calc_neto = sum(float(_el.get("subtotal",0)) for _el in _enriched)
            _calc_iva21 = sum(
                float(_el.get("subtotal",0)) * 0.21
                for _el in _enriched if abs(float(_el.get("iva_pct",21)) - 21) < 1)
            _calc_iva105 = sum(
                float(_el.get("subtotal",0)) * 0.105
                for _el in _enriched if abs(float(_el.get("iva_pct",21)) - 10.5) < 1)
            _calc_iva   = _calc_iva21 + _calc_iva105
            _calc_total = _calc_neto + _calc_iva
            _calc_costo = sum(float(_el.get("cost",0)) * float(_el.get("cantidad",1))
                              for _el in _enriched)
            _margin_total = ((_calc_neto - _calc_costo) / _calc_neto * 100
                             if _calc_neto > 0 else 0.0)

            # Usar valores detectados en el PDF cuando estén disponibles
            _show_neto  = float(oc_fields.get("subtotal_neto") or _calc_neto or 0)
            _show_iva   = float(oc_fields.get("iva_21")        or _calc_iva  or 0)
            _show_total = float(oc_fields.get("total")         or _calc_total or 0)
            if not _show_total and _show_neto:
                _show_total = _show_neto + _show_iva

            # ── SECCIÓN 4: RESUMEN FINANCIERO ────────────────────────────
            st.markdown("##### 💰 Resumen financiero")
            _rf1, _rf2, _rf3, _rf4 = st.columns(4)
            _rf1.metric("Total Neto",   fmt_ars(_show_neto))
            _rf2.metric("IVA",          fmt_ars(_show_iva))
            _rf3.metric("Total c/IVA",  fmt_ars(_show_total))
            _rf4.metric("Margen total", f"{_margin_total:.1f}%")

            # ── SECCIÓN 5: PLAZO DE PAGO ──────────────────────────────────
            st.markdown("##### 📅 Plazo de pago")
            _oc_dias     = oc_fields.get("dias_pago")
            _oc_cond_str = oc_fields.get("condiciones_pago","")
            _pt_choice_id = _pt_id  # default: plazo del cliente en Odoo

            if _pt_id and _pt_name:
                _odoo_dias_est = parse_payment_terms(_pt_name)
                _hay_disc = (
                    _oc_dias is not None
                    and _odoo_dias_est is not None
                    and abs(_odoo_dias_est - _oc_dias) > 3
                )
                if _hay_disc:
                    st.warning(
                        f"⚠️ Discrepancia en plazo: la OC indica **{_oc_dias} días** "
                        f"({_oc_cond_str}), pero el cliente tiene configurado **{_pt_name}** en Odoo."
                    )
                    _all_pts = get_all_payment_terms(models_url, uid, api_key)
                    _pt_opts_map = {name: pid for pid, name in _all_pts}
                    _radio_opts  = [
                        f"Odoo: {_pt_name}",
                        f"OC: {_oc_cond_str or f'{_oc_dias} días'}",
                        "Elegir otro plazo",
                    ]
                    _radio_sel = st.radio(
                        "¿Qué plazo usar en el pedido?",
                        _radio_opts, key=f"pt_radio_{uf.name}"
                    )
                    if _radio_sel == _radio_opts[0]:
                        _pt_choice_id = _pt_id
                    elif _radio_sel == _radio_opts[1]:
                        # Buscar el plazo de la OC en Odoo por días
                        _pt_choice_id = None
                        for _pid2, _pname2 in _all_pts:
                            _d2 = parse_payment_terms(_pname2)
                            if _d2 is not None and abs(_d2 - _oc_dias) <= 3:
                                _pt_choice_id = _pid2
                                break
                    else:
                        _pt_other_sel = st.selectbox(
                            "Plazo de pago", [n for _, n in _all_pts],
                            key=f"pt_other_{uf.name}"
                        )
                        _pt_choice_id = _pt_opts_map.get(_pt_other_sel)
                else:
                    _oc_info = f" — OC: {_oc_cond_str}" if _oc_cond_str else ""
                    st.info(f"✅ Plazo del cliente en Odoo: **{_pt_name}**{_oc_info}")
            elif _oc_dias:
                st.info(f"📅 OC indica **{_oc_dias} días** ({_oc_cond_str}) "
                        f"— cliente sin plazo configurado en Odoo.")
            elif _pt_id:
                st.info(f"📅 Plazo del cliente en Odoo: **{_pt_name}**")

            # ── SECCIÓN 6: ASIENTO ESTIMADO ───────────────────────────────
            st.markdown("##### 📒 Asiento estimado en Odoo")
            st.markdown(
                f"| Cuenta | Debe | Haber |\n"
                f"|---|---|---|\n"
                f"| Cuentas por Cobrar (Clientes) | {fmt_ars(_show_total)} | |\n"
                f"| Ventas / Ingresos | | {fmt_ars(_show_neto)} |\n"
                f"| IVA Débito Fiscal 21% | | {fmt_ars(_show_iva)} |"
            )

            # ── SECCIÓN 7: CREAR PEDIDO ───────────────────────────────────
            st.markdown("---")
            _btn_disabled = not bool(_partner_id_oc)
            if _btn_disabled:
                st.caption("🔒 Identificá o creá el cliente para habilitar la creación del pedido.")
            if st.button("⬆️ Crear pedido en Odoo", key=f"btn_order_{uf.name}",
                         type="primary", disabled=_btn_disabled):
                with st.spinner("Creando pedido en Odoo..."):
                    try:
                        _order_lines = []
                        for _el in _enriched:
                            _order_lines.append({
                                "product_id":  _el["odoo_product"]["id"] if _el.get("odoo_product") else None,
                                "descripcion": _el.get("descripcion",""),
                                "cantidad":    _el.get("cantidad", 1),
                                "precio_unit": _el.get("precio_unit", 0),
                            })
                        _ref_oc = _oc_num_i or oc_fields.get("numero_oc","")
                        _fec_oc = _oc_fec_i or oc_fields.get("fecha_iso","") or None
                        order_id = create_sale_order(
                            models, uid, api_key,
                            partner_id       = _partner_id_oc,
                            note             = f"OC {_ref_oc}" if _ref_oc else "",
                            lines            = _order_lines,
                            filename         = uf.name,
                            file_bytes       = file_bytes,
                            mimetype         = mimetype,
                            client_order_ref = _ref_oc or None,
                            payment_term_id  = _pt_choice_id or None,
                            date_order       = _fec_oc,
                        )
                        url = f"{ODOO_URL}/web#id={order_id}&model=sale.order&view_type=form"
                        st.success(f"✅ Pedido creado — [Abrir en Odoo]({url})")
                        st.session_state.history.append({"tipo":"Pedido cliente",
                            "archivo":uf.name,"id":order_id,"url":url,"estado":"✅"})
                    except Exception as _e:
                        st.error(f"❌ {_e}")


# ═══════════════════════════════════════════════════
# TAB 3 — IMPORTACIONES (ADMIN)
# ═══════════════════════════════════════════════════
if tab_import is not None:
    with tab_import:
        st.subheader("🛳️ Importaciones — Modo Claude")
        st.caption("Flujo Etapas 0→6 · BAs fixeadas · split = by_current_cost_price · Cuenta Puente 3284")

        # ── Carpeta ──────────────────────────────────────────────
        st.markdown("#### 📂 Carpeta de importación")
        col_carp, col_reset = st.columns([5, 1])
        carp_in = col_carp.text_input("Carpeta", value=st.session_state.carpeta_id,
            placeholder="LUMI_293", key="input_carpeta", label_visibility="collapsed")
        col_carp.caption("Ingresá el número de carpeta, ej: `LUMI_293`")

        if carp_in and carp_in != st.session_state.carpeta_id:
            st.session_state.carpeta_id    = carp_in
            st.session_state.carpeta_po    = None
            st.session_state.carpeta_bills = []
            st.session_state.etapas        = {k: False for k, *_ in ETAPAS_DEF}

        if col_reset.button("🔄", help="Nueva carpeta", key="btn_reset_carp"):
            for k in ["carpeta_id","carpeta_po","carpeta_bills","carpeta_lc_id"]:
                st.session_state[k] = DEFAULTS[k]
            st.session_state.etapas = {k: False for k, *_ in ETAPAS_DEF}
            st.rerun()

        # ── OC vinculada ─────────────────────────────────────────
        if st.session_state.carpeta_id:
            with st.expander("🔗 Vincular OC en Odoo (Etapa 0)", expanded=not st.session_state.etapas.get("0")):
                oc_q = st.text_input("Buscar OC", placeholder="P00025", key="oc_search_q")
                if oc_q:
                    with st.spinner("Buscando OC..."):
                        pos = search_purchase_orders(models_url, uid, api_key, oc_q)
                    if pos:
                        po_opts = {}
                        for p in pos:
                            pname    = p["name"]
                            ppartner = p["partner_id"][1] if p.get("partner_id") else "?"
                            ptotal   = p.get("amount_total", 0)
                            label    = f"{pname}  ·  {ppartner}  ·  ${ptotal:,.0f}"
                            po_opts[label] = p
                        sel_po = st.selectbox("Seleccioná la OC", list(po_opts.keys()), key="sel_po_imp")
                        if st.button("✅ Vincular OC", key="btn_link_po"):
                            st.session_state.carpeta_po  = po_opts[sel_po]
                            st.session_state.etapas["0"] = True
                            st.rerun()
                    else:
                        st.warning("No se encontraron OC confirmadas con ese nombre.")

            if st.session_state.carpeta_po:
                po = st.session_state.carpeta_po
                c1p, c2p = st.columns(2)
                pname    = po["name"]
                ppartner = po.get("partner_id", ["?","?"])[1]
                c1p.success(f"✅ OC: {pname} · {ppartner}")
                po_url = f"{ODOO_URL}/web#id={po['id']}&model=purchase.order&view_type=form"
                c2p.markdown(f"[🔗 Abrir OC en Odoo]({po_url})")

        st.divider()

        # ── Progreso ─────────────────────────────────────────────
        st.markdown("#### 📋 Progreso de la carpeta")
        cols_et = st.columns(len(ETAPAS_DEF))
        for i, (key, label, desc) in enumerate(ETAPAS_DEF):
            done = st.session_state.etapas.get(key, False)
            icon = "✅" if done else "⏳"
            cols_et[i].markdown(f"**{icon}**  \n<small>{label}</small>", unsafe_allow_html=True, help=desc)
            if not done and st.session_state.carpeta_id:
                if cols_et[i].button("✓", key=f"et_{key}", help=f"Marcar {label}"):
                    st.session_state.etapas[key] = True
                    st.rerun()
        completadas = sum(1 for v in st.session_state.etapas.values() if v)
        st.progress(completadas / len(ETAPAS_DEF), text=f"{completadas}/{len(ETAPAS_DEF)} etapas completadas")

        st.divider()

        # ── Subir documentos ─────────────────────────────────────
        st.markdown("#### ⬆️ Subir documentos")
        if not st.session_state.carpeta_id:
            st.warning("Primero ingresá el número de carpeta.")
        else:
            TIPO_OPTIONS = {
                "Bill PETDUR (Etapa 1)":           {"tipo":"petdur",  "partner_id":49328,"journal_id":71,"doc_type":None},
                "DI AFIP (Etapa 2)":               {"tipo":"di_afip", "partner_id":9,    "journal_id":10,"doc_type":66},
                "Bill TRICE Transport (Etapa 2a)": {"tipo":"nac",     "partner_id":48825,"journal_id":10,"doc_type":None},
                "Bill Terminal 4 SA (Etapa 2a)":   {"tipo":"nac",     "partner_id":48828,"journal_id":10,"doc_type":None},
                "Bill Mundo Comex (Etapa 2a)":     {"tipo":"nac",     "partner_id":48826,"journal_id":10,"doc_type":None},
                "Bill SENASA (Etapa 2a)":          {"tipo":"nac",     "partner_id":48827,"journal_id":10,"doc_type":None},
                "Otro comprobante":                {"tipo":"other",   "partner_id":None, "journal_id":10,"doc_type":None},
            }
            imp_files = st.file_uploader(
                f"Documentos de {st.session_state.carpeta_id} — podés subir todos juntos",
                type=["pdf","jpg","jpeg","png"], accept_multiple_files=True, key="import_uploader")

            classified_docs = []
            if imp_files:
                st.markdown("**Clasificación automática — revisá y ajustá si es necesario**")
                for uf in imp_files:
                    ext        = uf.name.rsplit(".", 1)[-1].lower()
                    file_bytes = uf.read()
                    mimetype   = MIMETYPES.get(ext, "application/octet-stream")
                    raw_text   = ""
                    if ext == "pdf":
                        _, raw_text = extract_pdf_fields(file_bytes)
                    auto = classify_document(raw_text)
                    default_label = auto["label"] if auto["label"] in TIPO_OPTIONS else "Otro comprobante"
                    with st.expander(f"📎 {uf.name} — {auto['label']}", expanded=True):
                        ct1, ct2, ct3, ct4 = st.columns([3, 2, 2, 1])
                        tipo_sel  = ct1.selectbox("Tipo", list(TIPO_OPTIONS.keys()),
                            index=list(TIPO_OPTIONS.keys()).index(default_label), key=f"tipo_{uf.name}")
                        ref_doc   = ct2.text_input("N° comprobante", key=f"ref_d_{uf.name}")
                        fecha_doc = ct3.text_input("Fecha (AAAA-MM-DD)", key=f"fec_d_{uf.name}", placeholder="2026-05-12")
                        moneda    = ct4.selectbox("Moneda", ["ARS","USD"], key=f"cur_{uf.name}")
                        classified_docs.append({
                            "filename":uf.name, "file_bytes":file_bytes, "mimetype":mimetype,
                            "tipo_cfg":TIPO_OPTIONS[tipo_sel], "ref":ref_doc, "fecha":fecha_doc,
                            "moneda": moneda,
                        })

                if classified_docs:
                    if st.button(f"⬆️ Crear {len(classified_docs)} registro(s) en Odoo",
                                 type="primary", key="btn_create_all_imp"):
                        prog = st.progress(0)
                        ok, errs = 0, []
                        carp = st.session_state.carpeta_id
                        for i, doc in enumerate(classified_docs):
                            try:
                                full_ref = f"{carp} / {doc['ref']}" if doc["ref"] else carp
                                _cur_id = None
                                if doc.get("moneda","ARS") == "USD":
                                    _cur_id = get_currency_id(models_url, uid, api_key, "USD")
                                move_id = create_vendor_bill(models, uid, api_key,
                                    partner_id  = doc["tipo_cfg"]["partner_id"],
                                    ref         = full_ref,
                                    invoice_date= doc["fecha"] or False,
                                    filename    = doc["filename"],
                                    file_bytes  = doc["file_bytes"],
                                    mimetype    = doc["mimetype"],
                                    journal_id  = doc["tipo_cfg"]["journal_id"],
                                    doc_type_id = doc["tipo_cfg"]["doc_type"],
                                    currency_id = _cur_id,
                                )
                                url = f"{ODOO_URL}/web#id={move_id}&model=account.move&view_type=form"
                                st.success(f"✅ {doc['filename']} → Factura ID {move_id} — [Ver en Odoo]({url})")
                                tipo = doc["tipo_cfg"]["tipo"]
                                if tipo == "petdur":
                                    st.session_state.etapas["1"] = True
                                elif tipo == "di_afip":
                                    st.session_state.etapas["2"] = True
                                elif tipo == "nac":
                                    st.session_state.etapas["2a"] = True
                                if move_id not in st.session_state.carpeta_bills:
                                    st.session_state.carpeta_bills.append(move_id)
                                st.session_state.history.append({"tipo":f"Importación {carp}",
                                    "archivo":doc["filename"],"id":move_id,"url":url,"estado":"✅"})
                                ok += 1
                            except Exception as e:
                                errs.append(f"❌ {doc['filename']}: {str(e)[:120]}")
                            prog.progress((i+1)/len(classified_docs))
                        if ok: st.success(f"✅ {ok} registro(s) creados para {carp}.")
                        for err in errs: st.error(err)
                        st.rerun()

        st.divider()

        # ── Landed Cost ──────────────────────────────────────────
        st.markdown("#### 🔗 Crear Landed Cost — Etapa 4 Bis")
        st.caption("`split_method: by_current_cost_price` — CFO Dios estricto · distribuye por valor CFR proporcional")

        if not st.session_state.carpeta_po:
            st.info("Vinculá una OC primero para seleccionar el picking de recepción.")
        elif not st.session_state.carpeta_id:
            st.info("Ingresá el número de carpeta.")
        else:
            with st.spinner("Cargando pickings..."):
                pickings = get_pickings_for_po(models_url, uid, api_key, st.session_state.carpeta_po["id"])
            if not pickings:
                st.warning("No se encontraron pickings para esta OC. Verificá en Odoo.")
            else:
                pick_opts = {}
                for p in pickings:
                    dest = p.get("location_dest_id", ["?","?"])[1]
                    pick_opts[f"{p['name']}  ·  {p['state']}  ·  {dest}"] = p["id"]
                sel_pick    = st.selectbox("Picking IN a asociar", list(pick_opts.keys()), key="sel_pick_lc")
                sel_pick_id = pick_opts[sel_pick]

                with st.spinner(f"Cargando bills de {st.session_state.carpeta_id}..."):
                    bills_carp = get_bills_for_carpeta(models_url, uid, api_key, st.session_state.carpeta_id)

                if not bills_carp:
                    st.warning("No se encontraron facturas con esa referencia. Subí los comprobantes primero.")
                else:
                    st.markdown("**Líneas del Landed Cost** — una por cada bill de costo")
                    lc_prod_labels = {f"{pid}: {pname}": pid for pid, pname in LC_PRODUCTS.items()}
                    lc_lines = []
                    for bill in bills_carp:
                        bname    = bill.get("name") or f"ID {bill['id']}"
                        bpartner = bill["partner_id"][1] if bill.get("partner_id") else "?"
                        bstate   = bill.get("state","")
                        ba_total = float(bill.get("amount_total") or 0)
                        bc1, bc2, bc3, bc4 = st.columns([3,3,2,1])
                        bc1.caption(f"📄 {bname} — {bpartner} [{bstate}]")
                        chosen_prod = bc2.selectbox("Producto LC", list(lc_prod_labels.keys()), key=f"lc_p_{bill['id']}")
                        lc_amt      = bc3.number_input("Monto", min_value=0.0, value=ba_total,
                                         key=f"lc_a_{bill['id']}", format="%.2f")
                        incl        = bc4.checkbox("✓", value=True, key=f"lc_i_{bill['id']}")
                        if incl and lc_amt > 0:
                            lc_lines.append({"product_id": lc_prod_labels[chosen_prod], "price_unit": lc_amt})

                    if lc_lines:
                        st.caption(f"{len(lc_lines)} línea(s) seleccionadas para el Landed Cost")
                        if st.button("🔗 Crear Landed Cost en Odoo", type="primary", key="btn_lc_create"):
                            try:
                                lc_id = create_landed_cost(models, uid, api_key,
                                    picking_ids=[sel_pick_id], cost_lines=lc_lines)
                                st.session_state.carpeta_lc_id = lc_id
                                st.session_state.etapas["4"]   = True
                                lc_url = f"{ODOO_URL}/web#id={lc_id}&model=stock.landed.cost&view_type=form"
                                st.success(f"✅ Landed Cost ID {lc_id} creado — [Abrir en Odoo]({lc_url})")
                                st.info("⚠️ Recordá validarlo en Odoo para que BA #23 actualice el PPP USD.")
                                st.session_state.history.append({"tipo":f"Landed Cost {st.session_state.carpeta_id}",
                                    "archivo":f"LC {st.session_state.carpeta_id} · {len(lc_lines)} líneas",
                                    "id":lc_id,"url":lc_url,"estado":"✅"})
                            except Exception as e:
                                st.error(f"❌ Error creando Landed Cost: {e}")
                    else:
                        st.info("Seleccioná al menos una línea con monto > 0.")

        st.divider()

        # ── Acta CFO ─────────────────────────────────────────────
        st.markdown("#### ✅ Acta CFO — Etapa 6")
        st.caption("15 checks del Decálogo CFO Dios — completar antes de declarar carpeta cerrada.")
        checks = []
        c_left, c_right = st.columns(2)
        for j, item in enumerate(DECALOGO):
            col = c_left if j % 2 == 0 else c_right
            checks.append(col.checkbox(item, key=f"acta_{j}"))
        total_chk = sum(checks)
        st.progress(total_chk / len(DECALOGO), text=f"{total_chk}/{len(DECALOGO)} checks")
        if total_chk == len(DECALOGO):
            if st.button(f"🎉 Firmar Acta CFO — Cerrar carpeta {st.session_state.carpeta_id}",
                         type="primary", key="btn_acta_firmar"):
                st.session_state.etapas["6"] = True
                st.balloons()
                st.success(f"✅ Carpeta {st.session_state.carpeta_id} CERRADA. Acta CFO firmada.")
        else:
            st.warning(f"⚠️ Quedan {len(DECALOGO) - total_chk} checks pendientes del Decálogo.")

# ═══════════════════════════════════════════════════
# TAB 4 — HISTORIAL
# ═══════════════════════════════════════════════════
with tab_history:
    st.subheader("Historial de esta sesión")
    history = st.session_state.history
    if history:
        for r in reversed(history):
            c1, c2, c3 = st.columns([2, 4, 1])
            c1.markdown(f"**{r['tipo']}**")
            c2.markdown(f"{r['archivo']} — [Ver en Odoo (ID {r['id']})]({r['url']})")
            c3.markdown(r["estado"])
        st.divider()
        if st.button("🗑️ Limpiar historial"):
            st.session_state.history = []
            st.rerun()
    else:
        st.info("Todavía no se realizaron cargas en esta sesión.")
