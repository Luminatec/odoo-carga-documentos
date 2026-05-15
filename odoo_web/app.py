"""
Carga de documentos → Odoo
Luminatec / GPowerByte
v2 — Módulo Importaciones (Modo Claude · Etapas 0-6)
"""

import streamlit as st
import xmlrpc.client
import base64
import re
import hashlib
from io import BytesIO

import pandas as pd

# ─── Page config ──────────────────────────────────────────────
st.set_page_config(
    page_title="Luminatec · Odoo",
    page_icon="🛒",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ─── CSS / Branding ───────────────────────────────────────────
st.markdown("""
<style>
  /* Sidebar oscuro con branding */
  [data-testid="stSidebar"] {
    background: #111111 !important;
  }
  [data-testid="stSidebar"] label,
  [data-testid="stSidebar"] .stMarkdown,
  [data-testid="stSidebar"] p,
  [data-testid="stSidebar"] small,
  [data-testid="stSidebar"] span {
    color: #eeeeee !important;
  }
  [data-testid="stSidebar"] .stTextInput input {
    background: #222 !important;
    color: #eee !important;
    border-color: #444 !important;
  }
  [data-testid="stSidebar"] .stButton button {
    background: #CC0000 !important;
    color: white !important;
    border: none !important;
    font-weight: 700 !important;
  }
  [data-testid="stSidebar"] .stButton button:hover {
    background: #AA0000 !important;
  }

  /* Logo Luminatec en sidebar */
  .lumi-sidebar-logo {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 6px 0 12px 0;
  }
  .lumi-logo-text {
    font-size: 1.6rem;
    font-weight: 900;
    color: #CC0000 !important;
    letter-spacing: -1px;
    line-height: 1;
  }
  .lumi-logo-dot {
    color: #F5C200 !important;
    font-size: 1.8rem;
  }

  /* Título principal */
  .main-title {
    font-size: 1.9rem;
    font-weight: 900;
    color: #CC0000;
    letter-spacing: -1px;
  }
  .main-title span { color: #F5C200; }

  /* File uploader */
  [data-testid="stFileUploader"] {
    border: 2px dashed #CC000033;
    border-radius: 10px;
    padding: 8px;
  }

  /* Badge admin */
  .admin-badge {
    display: inline-block;
    background: #F5C200;
    color: #111;
    font-size: 0.7rem;
    font-weight: 800;
    padding: 3px 10px;
    border-radius: 10px;
    text-transform: uppercase;
    letter-spacing: 0.5px;
  }

  /* Etapas */
  .etapa-ok   { color: #198754; font-weight: 700; }
  .etapa-pend { color: #aaa; }
  .etapa-card {
    background: #f8f8f8;
    border-left: 4px solid #CC0000;
    padding: 8px 14px;
    border-radius: 6px;
    margin-bottom: 6px;
  }
</style>
""", unsafe_allow_html=True)

# ─── Configuración fija ───────────────────────────────────────
ODOO_URL = "https://gpowerbyte-luminatec.odoo.com"
ODOO_DB  = "gpowerbyte-luminatec"

# PIN: SHA-256 de "IMPORT2026" — sobreescribible via st.secrets["ADMIN_PIN_HASH"]
DEFAULT_PIN_HASH = "b6a25b50b5c8065a2186e952f4bdcba54857070e446b5528919997c56a2858bc"

MIMETYPES = {
    "pdf":  "application/pdf",
    "jpg":  "image/jpeg",
    "jpeg": "image/jpeg",
    "png":  "image/png",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "xls":  "application/vnd.ms-excel",
}

# ─── Partners conocidos (Modo Claude) ─────────────────────────
PARTNERS_IMPORT = {
    "PETDUR CORPORATION":  {"id": 49328, "journal_id": 71, "tipo": "petdur",  "doc_type": None},
    "AFIP":                {"id": 9,     "journal_id": 10, "tipo": "di_afip", "doc_type": 66},
    "TRICE TRANSPORT":     {"id": 48825, "journal_id": 10, "tipo": "nac",     "doc_type": None},
    "TERMINAL 4 SA":       {"id": 48828, "journal_id": 10, "tipo": "nac",     "doc_type": None},
    "MUNDO COMEX":         {"id": 48826, "journal_id": 10, "tipo": "nac",     "doc_type": None},
    "SENASA":              {"id": 48827, "journal_id": 10, "tipo": "nac",     "doc_type": None},
}

# Productos Landed Cost (expense → Cuenta Puente 3284)
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
    "BAs #8 y #11 sin línea `standard_price` (verificado hoy)",
    "Productos LC 20241-20245 con expense_id = 3284",
    "Productos LC 20276-20277 con expense_id = 3284",
    "Todos los LCs usaron split_method = by_current_cost_price",
    "❌ NO se usó by_quantity en ningún LC",
    "❌ NO se crearon asientos correctivos preventivos",
    "Costo USD/u del lote coincide con modelo CFO (Δ < $0.10 USD/u)",
    "WAC ponderado verificado en libro mayor estándar Odoo UI",
    "Suppliers AFIP residual = deuda real del DI (Derechos+Tasas+IVA real)",
    "BA #23 actualizó x_studio_ppp al validar el Landed Cost",
    "Picking IN en estado Done / WH/PreIngreso confirmado",
    "Internal Transfer ejecutado → stock en WH/Disponible",
    "Referencia LUMI_XXX en todos los asientos de la cohorte",
]

# ─── Odoo helpers ─────────────────────────────────────────────
def odoo_connect(url, db, email, api_key):
    common = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/common", allow_none=True)
    uid = common.authenticate(db, email, api_key, {})
    if not uid:
        raise ValueError("Credenciales inválidas. Verificá el email y la API key.")
    models = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/object", allow_none=True)
    return uid, models


def call(models, uid, api_key, model, method, args, kw=None):
    return models.execute_kw(ODOO_DB, uid, api_key, model, method, args, kw or {})


@st.cache_data(ttl=300, show_spinner=False)
def search_partners(models_url, uid, api_key, name, limit=8):
    m = xmlrpc.client.ServerProxy(models_url, allow_none=True)
    rows = m.execute_kw(
        ODOO_DB, uid, api_key, "res.partner", "search_read",
        [[("name", "ilike", name), ("active", "=", True)]],
        {"fields": ["id", "name"], "limit": limit, "order": "name asc"},
    )
    return [(r["id"], r["name"]) for r in rows]


@st.cache_data(ttl=120, show_spinner=False)
def search_purchase_orders(models_url, uid, api_key, query):
    m = xmlrpc.client.ServerProxy(models_url, allow_none=True)
    rows = m.execute_kw(
        ODOO_DB, uid, api_key, "purchase.order", "search_read",
        [[("name", "ilike", query), ("state", "in", ["purchase", "done"])]],
        {"fields": ["id", "name", "partner_id", "date_order", "amount_total"], "limit": 10},
    )
    return rows


@st.cache_data(ttl=60, show_spinner=False)
def get_pickings_for_po(models_url, uid, api_key, po_id):
    m = xmlrpc.client.ServerProxy(models_url, allow_none=True)
    rows = m.execute_kw(
        ODOO_DB, uid, api_key, "stock.picking", "search_read",
        [[("purchase_id", "=", po_id), ("state", "!=", "cancel")]],
        {"fields": ["id", "name", "state", "location_dest_id"], "limit": 10},
    )
    return rows


@st.cache_data(ttl=60, show_spinner=False)
def get_bills_for_carpeta(models_url, uid, api_key, carpeta_ref):
    m = xmlrpc.client.ServerProxy(models_url, allow_none=True)
    rows = m.execute_kw(
        ODOO_DB, uid, api_key, "account.move", "search_read",
        [[("move_type", "=", "in_invoice"), ("ref", "ilike", carpeta_ref), ("state", "!=", "cancel")]],
        {"fields": ["id", "name", "partner_id", "invoice_date", "amount_total", "state", "journal_id"], "limit": 50},
    )
    return rows


def attach_file(models, uid, api_key, res_model, res_id, filename, file_bytes, mimetype):
    call(models, uid, api_key, "ir.attachment", "create", [{
        "name": filename,
        "res_model": res_model,
        "res_id": res_id,
        "datas": base64.b64encode(file_bytes).decode(),
        "mimetype": mimetype,
    }])


def create_vendor_bill(models, uid, api_key, partner_id, ref, invoice_date,
                       filename, file_bytes, mimetype,
                       journal_id=None, doc_type_id=None):
    vals = {"move_type": "in_invoice"}
    if partner_id:  vals["partner_id"]  = partner_id
    if ref:         vals["ref"]         = ref
    if invoice_date: vals["invoice_date"] = invoice_date
    if journal_id:  vals["journal_id"]  = journal_id
    if doc_type_id: vals["l10n_latam_document_type_id"] = doc_type_id
    move_id = call(models, uid, api_key, "account.move", "create", [vals])
    if file_bytes:
        attach_file(models, uid, api_key, "account.move", move_id, filename, file_bytes, mimetype)
    return move_id


def create_landed_cost(models, uid, api_key, picking_ids, cost_lines):
    """
    cost_lines: list of {"product_id": int, "price_unit": float}
    split_method: by_current_cost_price (CFO Dios estricto)
    """
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
    order_id = call(models, uid, api_key, "sale.order", "create", [{
        "partner_id": partner_id,
        "note": note or "",
    }])
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


# ─── PDF extraction ───────────────────────────────────────────
def extract_pdf_fields(file_bytes):
    try:
        import pdfplumber
        with pdfplumber.open(BytesIO(file_bytes)) as pdf:
            text = "\n".join(page.extract_text() or "" for page in pdf.pages)
    except Exception:
        return {}, ""

    fields = {"numero": "", "fecha": "", "proveedor": "", "total": ""}

    for pat in [
        r"(?:Factura|Invoice|N[°º.]|N[úu]mero)[:\s#]*([A-Z0-9\-]{4,20})",
        r"(F(?:CV|CT|CA|CE)[:\s\-]*\d[\d\-]+)",
    ]:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            fields["numero"] = m.group(1).strip()
            break

    for pat in [r"(\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4})", r"(\d{4}[/\-]\d{1,2}[/\-]\d{1,2})"]:
        m = re.search(pat, text)
        if m:
            fields["fecha"] = m.group(1)
            break

    for pat in [
        r"(?:TOTAL|Total a pagar|Importe total)[^\d]*(\d[\d.,]+)",
        r"(?:^|\s)\$\s*([\d.,]+)\s*$",
    ]:
        m = re.search(pat, text, re.IGNORECASE | re.MULTILINE)
        if m:
            fields["total"] = m.group(1).replace(".", "").replace(",", ".")
            break

    lines = [l.strip() for l in text.split("\n") if l.strip()]
    if lines:
        fields["proveedor"] = lines[0][:80]

    return fields, text


def classify_document(text):
    """Auto-clasifica el tipo de documento por texto extraído del PDF."""
    tu = text.upper()
    rules = [
        ("PETDUR",                    {"tipo": "petdur",  "label": "Bill PETDUR (Etapa 1)",         "partner_id": 49328, "journal_id": 71, "doc_type": None}),
        ("DECLARACI",                  {"tipo": "di_afip", "label": "DI AFIP (Etapa 2)",             "partner_id": 9,     "journal_id": 10, "doc_type": 66}),
        ("33693450239",                {"tipo": "di_afip", "label": "DI AFIP (Etapa 2)",             "partner_id": 9,     "journal_id": 10, "doc_type": 66}),
        ("TRICE",                      {"tipo": "nac",     "label": "Bill TRICE (Etapa 2a)",         "partner_id": 48825, "journal_id": 10, "doc_type": None}),
        ("TERMINAL 4",                 {"tipo": "nac",     "label": "Bill Terminal 4 (Etapa 2a)",    "partner_id": 48828, "journal_id": 10, "doc_type": None}),
        ("MUNDO COMEX",                {"tipo": "nac",     "label": "Bill Mundo Comex (Etapa 2a)",   "partner_id": 48826, "journal_id": 10, "doc_type": None}),
        ("SENASA",                     {"tipo": "nac",     "label": "Bill SENASA (Etapa 2a)",        "partner_id": 48827, "journal_id": 10, "doc_type": None}),
    ]
    for keyword, cfg in rules:
        if keyword in tu:
            return cfg
    return {"tipo": "other", "label": "Otro comprobante", "partner_id": None, "journal_id": 10, "doc_type": None}


def check_pin(pin_input):
    expected = st.secrets.get("ADMIN_PIN_HASH", DEFAULT_PIN_HASH)
    return hashlib.sha256(pin_input.encode()).hexdigest() == expected


# ─── Session state ────────────────────────────────────────────
DEFAULTS = {
    "uid": None, "models": None, "api_key": "", "email": "",
    "history": [], "admin_unlocked": False,
    "carpeta_id": "", "carpeta_po": None,
    "carpeta_bills": [], "carpeta_lc_id": None,
    "etapas": {k: False for k, *_ in ETAPAS_DEF},
}
for k, v in DEFAULTS.items():
    if k not in st.session_state:
        st.session_state[k] = v


# ═══════════════════════════════════════════════════
# SIDEBAR
# ═══════════════════════════════════════════════════
with st.sidebar:
    # ─── Logo ────────────────────────────────────────────────
    import os
    if os.path.exists("logo.png"):
        st.image("logo.png", width=180)
    else:
        st.markdown("""
<div class="lumi-sidebar-logo">
  <span class="lumi-logo-dot">🛒</span>
  <span class="lumi-logo-text">LUMINATEC</span>
</div>""", unsafe_allow_html=True)
    st.markdown("---")

    # ─── Conexión ─────────────────────────────────────────────
    st.markdown("### ⚙️ Conexión a Odoo")
    st.caption(f"`{ODOO_URL}`")
    st.caption(f"Base de datos: `{ODOO_DB}`")
    st.divider()

    email   = st.text_input("Email",   value=st.session_state.email,   placeholder="tu@empresa.com")
    api_key = st.text_input("API Key", value=st.session_state.api_key, type="password", placeholder="Pegá tu clave aquí")

    if st.button("🔌 Conectar", use_container_width=True):
        if email and api_key:
            try:
                with st.spinner("Conectando..."):
                    uid, models = odoo_connect(ODOO_URL, ODOO_DB, email, api_key)
                st.session_state.uid     = uid
                st.session_state.models  = models
                st.session_state.email   = email
                st.session_state.api_key = api_key
                st.rerun()
            except Exception as e:
                st.error(str(e))
        else:
            st.warning("Completá email y API key.")

    if st.session_state.uid:
        st.success("✅ Sesión activa")
        if st.button("🔓 Desconectar", use_container_width=True):
            st.session_state.uid    = None
            st.session_state.models = None
            st.session_state.admin_unlocked = False
            st.rerun()

    st.divider()

    # ─── Acceso Importaciones (PIN) ───────────────────────────
    if not st.session_state.admin_unlocked:
        with st.expander("🔐 Acceso Importaciones"):
            pin_inp = st.text_input("PIN", type="password", placeholder="••••••••", key="pin_input", label_visibility="collapsed")
            if st.button("Desbloquear", use_container_width=True, key="btn_unlock"):
                if check_pin(pin_inp):
                    st.session_state.admin_unlocked = True
                    st.rerun()
                else:
                    st.error("PIN incorrecto.")
    else:
        st.markdown('<span class="admin-badge">🔓 Importaciones activo</span>', unsafe_allow_html=True)
        st.caption(f"Carpeta: **{st.session_state.carpeta_id or 'sin selección'}**")
        if st.button("🔒 Bloquear", use_container_width=True, key="btn_lock"):
            st.session_state.admin_unlocked = False
            st.rerun()

    st.divider()
    with st.expander("¿Cómo genero la API Key?"):
        st.markdown("""
1. Odoo → **avatar** → **Mi perfil**
2. Pestaña **Seguridad de la cuenta**
3. **Claves API** → **Nueva clave API**
4. Copiá la clave y pegala arriba.

⚠️ Se muestra **una sola vez**.
""")


# ═══════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════
st.markdown('<h1 class="main-title">🛒 <span>LUMINA</span>TEC · Carga Odoo</h1>', unsafe_allow_html=True)
st.caption("Facturas de proveedores, pedidos de clientes e importaciones — todo en un lugar.")

if not st.session_state.uid:
    st.info("👈 Conectate a Odoo desde el panel lateral para empezar.")
    st.stop()

uid        = st.session_state.uid
models     = st.session_state.models
api_key    = st.session_state.api_key
models_url = f"{ODOO_URL}/xmlrpc/2/object"

# Tabs dinámicos (Importaciones solo si admin)
_tabs = ["🧾 Facturas de proveedores", "📦 Pedidos de clientes"]
if st.session_state.admin_unlocked:
    _tabs.append("🛳️ Importaciones")
_tabs.append("📋 Historial de sesión")

_tab_objs = st.tabs(_tabs)

if st.session_state.admin_unlocked:
    tab_bills, tab_orders, tab_import, tab_history = _tab_objs
else:
    tab_bills, tab_orders, tab_history = _tab_objs
    tab_import = None


# ─── Helper: partner selector ─────────────────────────────────
def partner_selector(label, default_name, key_prefix):
    name = st.text_input(label, value=default_name[:60] if default_name else "", key=f"{key_prefix}_name")
    partner_id = None
    if name:
        with st.spinner("Buscando en Odoo..."):
            matches = search_partners(models_url, uid, api_key, name)
        if matches:
            options = {m[1]: m[0] for m in matches}
            chosen  = st.selectbox("Seleccioná el registro en Odoo", list(options.keys()), key=f"{key_prefix}_sel")
            partner_id = options[chosen]
        else:
            st.warning(f"'{name}' no encontrado en Odoo.")
    return partner_id, name


# ═══════════════════════════════════════════════════
# TAB 1 — FACTURAS DE PROVEEDORES
# ═══════════════════════════════════════════════════
with tab_bills:
    st.subheader("Facturas de proveedores")
    files = st.file_uploader(
        "Arrastrá o elegí archivos (PDF, JPG, PNG, XLSX)",
        type=["pdf", "jpg", "jpeg", "png", "xlsx", "xls"],
        accept_multiple_files=True,
        key="bills_upload",
    )
    if not files:
        st.caption("Subí uno o más archivos para empezar.")

    for uf in (files or []):
        st.divider()
        ext        = uf.name.rsplit(".", 1)[-1].lower()
        file_bytes = uf.read()
        mimetype   = MIMETYPES.get(ext, "application/octet-stream")
        st.markdown(f"**📎 {uf.name}**  `{ext.upper()}`  ({len(file_bytes)//1024} KB)")

        # Excel
        if ext in ("xlsx", "xls"):
            try:
                df = pd.read_excel(BytesIO(file_bytes), dtype=str).fillna("")
                df.columns = [c.strip() for c in df.columns]
            except Exception as e:
                st.error(f"No se pudo leer el Excel: {e}")
                continue
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
                        st.session_state.history.append({"tipo": "Factura proveedor",
                            "archivo": f"{uf.name}·fila{i+1}", "id": move_id, "url": url, "estado": "✅"})
                    except Exception as e:
                        errs.append(f"Fila {i+1}: {str(e)[:100]}")
                    bar.progress((i + 1) / len(df))
                if ok:
                    st.success(f"✅ {ok} de {len(df)} facturas creadas en Odoo.")
                for err in errs:
                    st.warning(err)

        # PDF / Imagen
        else:
            extracted, raw_text = {}, ""
            if ext == "pdf":
                with st.spinner("Leyendo PDF..."):
                    extracted, raw_text = extract_pdf_fields(file_bytes)
                st.caption("🤖 Datos detectados — revisá antes de confirmar." if extracted.get("proveedor")
                           else "ℹ️ PDF sin texto extraíble. Completá los datos a mano.")
            elif ext in ("jpg", "jpeg", "png"):
                st.image(file_bytes, caption="Vista previa", width=380)

            with st.form(key=f"bill_form_{uf.name}"):
                c1, c2  = st.columns(2)
                prov_i  = c1.text_input("Proveedor",         value=extracted.get("proveedor", "")[:60], placeholder="Nombre exacto en Odoo")
                ref_i   = c2.text_input("N° de factura",     value=extracted.get("numero", ""))
                fecha_i = c1.text_input("Fecha (AAAA-MM-DD)", value="", placeholder="2026-05-12")
                c2.text_input("Total (referencia)", value=extracted.get("total", ""), disabled=True)
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
                                st.caption(f"Proveedor asignado: **{m2[0]