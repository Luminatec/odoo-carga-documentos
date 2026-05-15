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
  [data-testid="stSidebar"] .stButton button {
    background: #CC0000 !important; color: white !important;
    border: none !important; font-weight: 700 !important;
  }
  [data-testid="stSidebar"] .stButton button:hover { background: #AA0000 !important; }
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
ODOO_DB  = "gpowerbyte-luminatec"

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
    """
    Establece la conexión de servicio usando la API key central de st.secrets.
    Retorna (service_uid, models_proxy, api_key) o (None, None, "").
    Requiere en secrets:
      ODOO_API_KEY      = "la_api_key_del_usuario_de_servicio"
      ODOO_SERVICE_EMAIL = "servicio@luminatec.com"  (el dueño de esa API key)
    """
    api_key   = st.secrets.get("ODOO_API_KEY", "")
    svc_email = st.secrets.get("ODOO_SERVICE_EMAIL", "")
    if not api_key or not svc_email:
        return None, None, ""
    try:
        common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common", allow_none=True)
        uid    = common.authenticate(ODOO_DB, svc_email, api_key, {})
        if not uid:
            return None, None, ""
        models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object", allow_none=True)
        return uid, models, api_key
    except Exception:
        return None, None, ""

def verify_user(email, password):
    """
    Verifica que email + contraseña sean válidos en Odoo.
    Solo autentica — las operaciones usan la API key de servicio.
    """
    try:
        common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common", allow_none=True)
        uid    = common.authenticate(ODOO_DB, email.strip(), password, {})
        return bool(uid)
    except Exception:
        return False

def call(models, uid, api_key, model, method, args, kw=None):
    return models.execute_kw(ODOO_DB, uid, api_key, model, method, args, kw or {})

@st.cache_data(ttl=300, show_spinner=False)
def search_partners(models_url, uid, api_key, name, limit=8):
    m = xmlrpc.client.ServerProxy(models_url, allow_none=True)
    rows = m.execute_kw(ODOO_DB, uid, api_key, "res.partner", "search_read",
        [[("name", "ilike", name), ("active", "=", True)]],
        {"fields": ["id", "name"], "limit": limit, "order": "name asc"})
    return [(r["id"], r["name"]) for r in rows]

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
                       filename, file_bytes, mimetype, journal_id=None, doc_type_id=None):
    vals = {"move_type": "in_invoice"}
    if partner_id:   vals["partner_id"]  = partner_id
    if ref:          vals["ref"]         = ref
    if invoice_date: vals["invoice_date"] = invoice_date
    if journal_id:   vals["journal_id"]  = journal_id
    if doc_type_id:  vals["l10n_latam_document_type_id"] = doc_type_id
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

def create_sale_order(models, uid, api_key, partner_id, note, lines, filename, file_bytes, mimetype):
    order_id = call(models, uid, api_key, "sale.order", "create", [{"partner_id": partner_id, "note": note or ""}])
    for ln in lines:
        prod_ids = call(models, uid, api_key, "product.product", "search",
                        [[("name", "ilike", ln.get("producto", ""))]], {"limit": 1})
        line_vals = {
            "order_id": order_id,
            "name": ln.get("producto") or "Sin descripción",
            "product_uom_qty": _to_float(ln.get("cantidad", 1)),
            "price_unit": _to_float(ln.get("precio", 0)),
        }
        if prod_ids:
            line_vals["product_id"] = prod_ids[0]
        call(models, uid, api_key, "sale.order.line", "create", [line_vals])
    attach_file(models, uid, api_key, "sale.order", order_id, filename, file_bytes, mimetype)
    return order_id

def _to_float(v):
    try:
        return float(str(v).replace(",", "."))
    except Exception:
        return 0.0

def extract_pdf_fields(file_bytes):
    try:
        import pdfplumber
        with pdfplumber.open(BytesIO(file_bytes)) as pdf:
            text = "\n".join(page.extract_text() or "" for page in pdf.pages)
    except Exception:
        return {}, ""
    fields = {"numero": "", "fecha": "", "proveedor": "", "total": ""}
    for pat in [r"(?:Factura|Invoice|N[°º.]|N[úu]mero)[:\s#]*([A-Z0-9\-]{4,20})", r"(F(?:CV|CT|CA|CE)[:\s\-]*\d[\d\-]+)"]:
        m = re.search(pat, text, re.IGNORECASE)
        if m: fields["numero"] = m.group(1).strip(); break
    for pat in [r"(\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4})", r"(\d{4}[/\-]\d{1,2}[/\-]\d{1,2})"]:
        m = re.search(pat, text)
        if m: fields["fecha"] = m.group(1); break
    for pat in [r"(?:TOTAL|Total a pagar|Importe total)[^\d]*(\d[\d.,]+)", r"(?:^|\s)\$\s*([\d.,]+)\s*$"]:
        m = re.search(pat, text, re.IGNORECASE | re.MULTILINE)
        if m: fields["total"] = m.group(1).replace(".", "").replace(",", "."); break
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    if lines: fields["proveedor"] = lines[0][:80]
    return fields, text

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
                    ok = verify_user(email_in, pass_in)
                if ok:
                    st.session_state.logged_in  = True
                    st.session_state.user_email = email_in.strip().lower()
                    st.rerun()
                else:
                    st.error("Email o contraseña incorrectos.")
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
svc_uid, svc_models, svc_api_key = get_service_connection()
if not svc_uid:
    st.error("⚠️ La app no está configurada correctamente. "
             "Falta `ODOO_API_KEY` o `ODOO_SERVICE_EMAIL` en los secrets de Streamlit. "
             "Contactá al administrador.")
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
            with st.form(key=f"bill_form_{uf.name}"):
                c1, c2  = st.columns(2)
                prov_i  = c1.text_input("Proveedor",          value=extracted.get("proveedor","")[:60], placeholder="Nombre exacto en Odoo")
                ref_i   = c2.text_input("N° de factura",      value=extracted.get("numero",""))
                fecha_i = c1.text_input("Fecha (AAAA-MM-DD)", value="", placeholder="2026-05-12")
                c2.text_input("Total (referencia)", value=extracted.get("total",""), disabled=True)
                st.text_area("Notas internas", height=55)
                go = st.form_submit_button("⬆️ Cargar en Odoo", use_container_width=True)
            if go:
                with st.spinner("Procesando..."):
                    try:
                        partner_id = False
                        if prov_i:
                            m2 = search_partners(models_url, uid, api_key, prov_i, limit=3)
                            if m2:
                                partner_id = m2[0][0]
                                st.caption("Proveedor asignado: " + m2[0][1])
                            else:
                                st.warning(f"'{prov_i}' no encontrado — se creará sin proveedor.")
                        move_id = create_vendor_bill(models, uid, api_key,
                            partner_id=partner_id, ref=ref_i,
                            invoice_date=fecha_i or False,
                            filename=uf.name, file_bytes=file_bytes, mimetype=mimetype)
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
            extracted, _ = {}, ""
            if ext == "pdf":
                with st.spinner("Leyendo PDF..."):
                    extracted, _ = extract_pdf_fields(file_bytes)
            elif ext in ("jpg","jpeg","png"):
                st.image(file_bytes, caption="Vista previa", width=380)
            with st.form(key=f"order_form_{uf.name}"):
                c1, c2  = st.columns(2)
                cli_i   = c1.text_input("Cliente", value=extracted.get("proveedor","")[:60], placeholder="Nombre exacto en Odoo")
                ref_i   = c2.text_input("Referencia / N° pedido", value=extracted.get("numero",""))
                notas_i = st.text_area("Notas", height=55)
                st.caption("Líneas del pedido (opcional)")
                lines_txt = st.text_area("Formato: Producto | Cantidad | Precio", height=90,
                    placeholder="Camiseta azul M | 10 | 2500\nPantalon negro L | 5 | 4000")
                go = st.form_submit_button("⬆️ Crear pedido en Odoo", use_container_width=True)
            if go:
                with st.spinner("Procesando..."):
                    try:
                        matches = search_partners(models_url, uid, api_key, cli_i, limit=3)
                        if not matches:
                            st.error(f"Cliente '{cli_i}' no encontrado en Odoo."); st.stop()
                        partner_id = matches[0][0]
                        st.caption("Cliente asignado: " + matches[0][1])
                        lines = []
                        for line in (lines_txt or "").strip().split("\n"):
                            if not line.strip(): continue
                            parts = [p.strip() for p in line.split("|")]
                            lines.append({
                                "producto": parts[0] if len(parts)>0 else "",
                                "cantidad": parts[1] if len(parts)>1 else 1,
                                "precio":   parts[2] if len(parts)>2 else 0,
                            })
                        order_id = create_sale_order(models, uid, api_key,
                            partner_id=partner_id, note=notas_i, lines=lines,
                            filename=uf.name, file_bytes=file_bytes, mimetype=mimetype)
                        url = f"{ODOO_URL}/web#id={order_id}&model=sale.order&view_type=form"
                        st.success(f"✅ Pedido creado — [Abrir en Odoo]({url})")
                        st.session_state.history.append({"tipo":"Pedido cliente",
                            "archivo":uf.name,"id":order_id,"url":url,"estado":"✅"})
                    except Exception as e:
                        st.error(f"❌ {e}")


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
                        ct1, ct2, ct3 = st.columns([3, 2, 2])
                        tipo_sel = ct1.selectbox("Tipo", list(TIPO_OPTIONS.keys()),
                            index=list(TIPO_OPTIONS.keys()).index(default_label), key=f"tipo_{uf.name}")
                        ref_doc   = ct2.text_input("N° comprobante", key=f"ref_d_{uf.name}")
                        fecha_doc = ct3.text_input("Fecha (AAAA-MM-DD)", key=f"fec_d_{uf.name}", placeholder="2026-05-12")
                        classified_docs.append({
                            "filename":uf.name, "file_bytes":file_bytes, "mimetype":mimetype,
                            "tipo_cfg":TIPO_OPTIONS[tipo_sel], "ref":ref_doc, "fecha":fecha_doc,
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
                                move_id = create_vendor_bill(models, uid, api_key,
                                    partner_id  = doc["tipo_cfg"]["partner_id"],
                                    ref         = full_ref,
                                    invoice_date= doc["fecha"] or False,
                                    filename    = doc["filename"],
                                    file_bytes  = doc["file_bytes"],
                                    mimetype    = doc["mimetype"],
                                    journal_id  = doc["tipo_cfg"]["journal_id"],
                                    doc_type_id = doc["tipo_cfg"]["doc_type"],
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
