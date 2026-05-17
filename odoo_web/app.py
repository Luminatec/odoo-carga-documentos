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
            "params": {"db": ODOO_DB, "login": email, "password": password}
        }).encode()
        _req = urllib.request.Request(
            f"{ODOO_URL}/web/session/authenticate",
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
        rows = m.execute_kw(ODOO_DB, uid, api_key, "res.currency.rate", "search_read",
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
        return m.execute_kw(ODOO_DB, uid, api_key, "purchase.order.line", "search_read",
            [[("order_id", "=", po_id), ("state", "!=", "cancel")]],
            {"fields": ["product_id", "product_qty", "price_unit", "price_subtotal", "name"]})
    except Exception:
        return []

@st.cache_data(ttl=30, show_spinner=False)
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
        try:
            pos = m.execute_kw(ODOO_DB, uid, api_key, "purchase.order", "search_read",
                [[("partner_ref", "ilike", carpeta_id), ("state", "in", ["purchase", "done"])]],
                {"fields": po_fields_ext, "limit": 5})
        except Exception:
            pos = m.execute_kw(ODOO_DB, uid, api_key, "purchase.order", "search_read",
                [[("partner_ref", "ilike", carpeta_id), ("state", "in", ["purchase", "done"])]],
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
        bills = m.execute_kw(ODOO_DB, uid, api_key, "account.move", "search_read",
            [[("move_type", "=", "in_invoice"), ("ref", "ilike", carpeta_id),
              ("state", "!=", "cancel")]],
            {"fields": bill_fields, "limit": 50})
        result["bills"] = bills

        # 3. Pickings vinculados a la OC
        pickings = []
        if po:
            pickings = m.execute_kw(ODOO_DB, uid, api_key, "stock.picking", "search_read",
                [[("purchase_id", "=", po["id"]), ("picking_type_code", "=", "incoming")]],
                {"fields": ["id", "name", "state", "date_done", "location_dest_id"]})
        result["pickings"] = pickings

        # 4. Landed Costs
        lc_ids = []
        if pickings:
            lcs = m.execute_kw(ODOO_DB, uid, api_key, "stock.landed.cost", "search_read",
                [[("picking_ids", "in", [p["id"] for p in pickings])]],
                {"fields": ["id", "name", "state"], "limit": 5})
            lc_ids = [lc["id"] for lc in lcs]
        result["lc_ids"] = lc_ids

        # 5. Detectar etapas automáticamente
        partner_ids = {b["partner_id"][0] for b in bills if b.get("partner_id")}
        stages = {k: False for k, *_ in ETAPAS_DEF}
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
    "stock.landed.cost":  "odoo/inventory/landed-costs",
}

def odoo_url(model, record_id):
    """URL directa Odoo 17 para un registro. Funciona en Odoo 16+ también."""
    path = _ODOO17_PATHS.get(model)
    if path:
        return f"{ODOO_URL}/{path}/{record_id}"
    # fallback hash-URL por si el modelo no está mapeado
    return odoo_url("{model}", record_id)

def fmt_ars(v):
    """Formatea número como moneda ARS: $ 1.234,56"""
    if not v:
        return ""
    try:
        s = "{:,.2f}".format(float(v))
        return "$ " + s.replace(",", "X").replace(".", ",").replace("X", ".")
    except Exception:
        return str(v)

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
        F   = ["id", "name", "default_code", "standard_price", "list_price"]

        def _best(rows):
            if not rows:
                return []
            l = [r for r in rows if str(r.get("default_code") or "").upper().startswith("L")]
            pool = l or rows
            return [max(pool, key=lambda r: float(r.get("standard_price") or 0))]

        def _tmpl(domain, lim=20):
            return m.execute_kw(ODOO_DB, uid, api_key,
                                "product.template", "search_read",
                                [domain], {"fields": F, "limit": lim})

        # ── 1. CÓDIGO ──────────────────────────────────────────────────────────
        for c in dict.fromkeys([code.strip(), code.strip().lstrip("0")]):
            if not c:
                continue
            r = _best(_tmpl([("default_code", "=",     c), ("active", "=", True)], 5))
            if r: return r
            r = _best(_tmpl([("default_code", "ilike", c), ("active", "=", True)], 10))
            if r: return r

        # ── 2. NOMBRE ──────────────────────────────────────────────────────────
        if name_keywords and name_keywords.strip():
            kw     = re.sub(r"[^\w\s]", " ", name_keywords)
            words  = kw.split()
            # Tokens con letras Y dígitos (números de modelo: G1110, 190C, 190BK…)
            model  = [w for w in words
                      if re.search(r"[A-Za-z]", w) and re.search(r"\d", w) and len(w) >= 4]
            # Tokens solo letras, cortos (contexto tipo "GI")
            short  = [w for w in words if w.isalpha() and len(w) == 2]

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

        # ── 3. EAN13 ────────────────────────────────────────────────────────────
        if ean13 and len(str(ean13)) == 13 and str(ean13).isdigit():
            r = m.execute_kw(ODOO_DB, uid, api_key, "product.product", "search_read",
                             [[("barcode", "=", str(ean13)), ("active", "=", True)]],
                             {"fields": F, "limit": 1})
            if r: return r

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
        # CASTILLO: "Orden de Compra N 0001-0118,667"
        r"(?:Orden\s+de\s+[Cc]ompra|O\.?C\.?\s*N[°o]?|ORDEN\s+DE\s+COMPRA\s*N?\s*)[:\s]*([0-9]{4}[-/][0-9,]{4,})",
        # Carsa/MUSIMUNDO: "Orden Definitiva de Provisión Número: 4501653808"
        r"(?:Orden\s+Definitiva|Orden\s+de\s+Provisi[oó]n)\b.{0,40}N[úu]mero[:\s]+(\d{6,})",
        r"(?:N[°º]\s*(?:de\s+)?[Oo]rden|Pedido\s+N[°º]|N[°º]\s*[Pp]edido)[:\s]*([0-9]{4}[-/][0-9,]{4,}|\d{6,})",
        r"\b(0{4}[-/][0-9,]{4,})\b",
        # La Anónima / genérico: "Número: 22620313"
        r"\bN[úu]mero[:\s]+(\d{5,})\b",
    ]
    for pat in oc_pats:
        mo = re.search(pat, text, re.IGNORECASE)
        if mo:
            fields["numero_oc"] = mo.group(1).strip().replace(",", "")
            break

    # ── Fecha ─────────────────────────────────────────────────────────────
    # Soporta DD/MM/YYYY, DD.MM.YYYY, DD-MM-YYYY
    date_pats = [
        r"(?:Fecha\s+[Ee]misi[oó]n|Fecha\s+OC|Fecha)[:\s]+(\d{1,2}[/.\-]\d{1,2}[/.\-]\d{4})",
        r"(?:^|\s)(\d{1,2}[/.\-]\d{1,2}[/.\-]\d{4})(?:\s|$)",
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
        # Carsa: "Condición: 0016 - 60 Dias"
        r"(?:^|\n)\s*Condici[oó]n[:\s]+([^\n]{3,60})",
        r"(CUENTA\s+CORRIENTE[^\n]{0,60})",
    ]
    for pat in cond_pats:
        mo = re.search(pat, text, re.IGNORECASE | re.MULTILINE)
        if mo:
            fields["condiciones_pago"] = mo.group(1).strip()
            break

    # Días: buscar "Intervalo: 30", "60 Dias", "LOS XX DIAS"
    intervalo_mo = re.search(r"[Ii]ntervalo[:\s]+(\d+)", text)
    if intervalo_mo:
        fields["dias_pago"] = int(intervalo_mo.group(1))
    else:
        fields["dias_pago"] = parse_payment_terms(fields["condiciones_pago"] or text)

    # ── Totales ───────────────────────────────────────────────────────────
    mo = re.search(r"(?:Sub[-\s]?[Tt]otal\s+[Nn]eto|SUBTOTAL\s+NETO|Neto\s+Gravado)[:\s$]*\$?\s*([\d.,]+)", text, re.IGNORECASE)
    if mo:
        fields["subtotal_neto"] = normalize_amount(mo.group(1))
    # Solo capturar IVA si hay un monto en la MISMA línea (no cruzar newline)
    # Esto evita capturar EAN13 del producto siguiente como monto de IVA
    mo = re.search(r"IVA\s+21\s*%[ \t:$]*\$?[ \t]*([\d.,]+)", text, re.IGNORECASE)
    if mo:
        _iva21_raw = mo.group(1)
        # Descartar si parece un EAN13 u otro código de barras (≥12 dígitos sin coma/punto)
        if not re.match(r'^\d{12,}$', _iva21_raw.replace(',','').replace('.','')):
            fields["iva_21"] = normalize_amount(_iva21_raw)
    mo = re.search(r"IVA\s+10[.,]5\s*%[ \t:$]*\$?[ \t]*([\d.,]+)", text, re.IGNORECASE)
    if mo:
        _iva105_raw = mo.group(1)
        if not re.match(r'^\d{12,}$', _iva105_raw.replace(',','').replace('.','')):
            fields["iva_105"] = normalize_amount(_iva105_raw)
    # Carsa: "TOTAL PEDIDO DE COMPRAS ARS 35.923.359,50"
    total_pats = [
        r"(?:Total\s+OC|TOTAL\s+OC|Total\s+[Oo]rden)[:\s$]*\$?\s*([\d.,]+)",
        r"TOTAL\s+PEDIDO[^\d]+([\d.,]+)",
        r"(?:^|\n)\s*TOTAL[:\s$]*\$?\s*([\d.,]+)\s*(?:$|\n)",
    ]
    for _tp in total_pats:
        mo = re.search(_tp, text, re.IGNORECASE | re.MULTILINE)
        if mo:
            fields["total"] = normalize_amount(mo.group(1))
            break

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

    # ── Fallback EAN13: formato Carsa/MUSIMUNDO ──────────────────────────
    # Líneas: {EAN13} {INTCODE-DESCRIPCION} {M3} {QTY} UN {PRECIO} {SUBTOTAL}
    # pdfplumber puede wrappear la línea; combinamos hasta 3 líneas siguientes
    # para armar el registro completo antes de aplicar el regex.
    # IVA en línea posterior ("IVA 21%" / "IVA 10,5%")
    if not fields["lineas"] and text:
        _ean_lines = text.split("\n")
        _ean_pat   = re.compile(
            r'^(\d{13})\s+(.+?)\s+UN\s+([\d.]+,\d{2})\s+([\d.]+,\d{2})\s*$')
        _ean_stop  = re.compile(
            r'^(?:\d{13}\s|Entregar:|TOTAL|Vencimiento|P[aá]gina)',
            re.IGNORECASE)
        for _ei, _eln in enumerate(_ean_lines):
            _strip = _eln.strip()
            if not re.match(r'^\d{13}\b', _strip):
                continue
            # Combinar con las siguientes líneas SOLO si la línea actual no matchea completa
            # (evita agregar el modelo del producto siguiente que rompe el regex)
            _combined = _strip
            if not _ean_pat.match(_combined.strip()):
                for _fwd_idx in range(_ei + 1, min(_ei + 4, len(_ean_lines))):
                    _nxt = _ean_lines[_fwd_idx].strip()
                    if _ean_stop.match(_nxt):
                        break
                    _combined += ' ' + _nxt
                    if _ean_pat.match(_combined.strip()):
                        break
            _em = _ean_pat.match(_combined.strip())
            if not _em:
                continue
            _ean       = _em.group(1)
            _rest_ean  = _em.group(2).strip()
            _price_raw = _em.group(3)
            _sub_raw   = _em.group(4)

            # Extraer qty (último entero antes de UN — ya consumido por regex previo)
            # qty está al final de _rest_ean: "... 3,540 60"
            _qty_m = re.search(r'\s+(\d+)\s*$', _rest_ean)
            _qty   = int(_qty_m.group(1)) if _qty_m else 0
            _rest_ean = (_rest_ean[:_qty_m.start()].strip() if _qty_m else _rest_ean)

            # Remover M3 (decimal con coma como separador decimal: "3,540")
            _m3_m = re.search(r'\s+([\d]+,\d{3})\s*$', _rest_ean)
            if _m3_m:
                _rest_ean = _rest_ean[:_m3_m.start()].strip()

            # Extraer código interno: "176270-DESCRIPCION"
            _ic_m = re.match(r'^(\d{4,8})-(.+)$', _rest_ean)
            _int_code = _ic_m.group(1) if _ic_m else _ean
            _desc_ean = (_ic_m.group(2).strip() if _ic_m else _rest_ean.strip())

            # Líneas de continuación: absorber hasta encontrar "Entregar:" o otro EAN13
            _ei2 = _ei + 1
            while _ei2 < len(_ean_lines):
                _nl2 = _ean_lines[_ei2].strip()
                if (re.match(r'^\d{13}\s', _nl2)
                        or re.match(r'^Entregar:', _nl2, re.IGNORECASE)
                        or re.match(r'^TOTAL|^Vencimiento|^P[aá]gina', _nl2, re.IGNORECASE)):
                    break
                if _nl2 and re.search(r'[A-Za-z0-9]', _nl2) and not re.match(r'^IVA', _nl2, re.IGNORECASE):
                    _desc_ean += " " + _nl2
                _ei2 += 1

            # Buscar IVA en las líneas siguientes (después de Entregar:)
            _iva_ean = 21.0
            for _fwd in range(_ei + 1, min(_ei + 6, len(_ean_lines))):
                _fwd_ln = _ean_lines[_fwd].strip()
                _iva_m2 = re.match(r'IVA\s+([\d,.]+)\s*%', _fwd_ln, re.IGNORECASE)
                if _iva_m2:
                    try:
                        _iva_ean = float(normalize_amount(_iva_m2.group(1)))
                    except Exception:
                        pass
                    break

            _price_f = float(normalize_amount(_price_raw))
            _sub_f   = float(normalize_amount(_sub_raw))
            _desc_ean = re.sub(r'\s+', ' ', _desc_ean).strip()

            fields["lineas"].append({
                "codigo":      _int_code,
                "ean13":       _ean,        # código de barras EAN13 original
                "descripcion": _desc_ean,
                "cantidad":    float(_qty),
                "precio_unit": _price_f,
                "iva_pct":     _iva_ean,
                "subtotal":    _sub_f,
            })

    # ── Fallback: parser de texto cuando no hay tablas ───────────────────
    # Detecta líneas de producto del tipo:
    #   CODIGO  QTY  DESCRIPCION  NETO  IVA%  IT  CFINAL  SUBTOTAL
    # Maneja números fusionados (artefacto de pdfplumber): "297,004.1329,700,413.00"
    if not fields["lineas"] and text:
        _lines = text.split("\n")
        _hdr_idx = None
        for _i, _ln in enumerate(_lines):
            if re.search(r'\bC[oó]digo\b.{0,30}\bCant\b', _ln, re.IGNORECASE):
                _hdr_idx = _i
                break

        if _hdr_idx is not None:
            _stop = re.compile(
                r'^(?:Sub[-\s]?[Tt]otal|Totales|TOTALES|Sub-Totales|Observaciones|IMPORTANTE)',
                re.IGNORECASE)
            _num_re = r'\d{1,3}(?:[.,]\d{3})*[.,]\d{2}|\d+[.,]\d{2}'

            def _parse_num(s):
                s = str(s).strip()
                lc, ld = s.rfind(","), s.rfind(".")
                if lc > ld:
                    return float(s.replace(".", "").replace(",", "."))
                return float(s.replace(",", ""))

            _j = _hdr_idx + 1
            while _j < len(_lines):
                _raw = _lines[_j].strip()
                if _stop.match(_raw):
                    break
                _cm = re.match(r'^(\d{6,12})\s+(\d+)\s+(.+)$', _raw)
                if _cm:
                    _cod   = _cm.group(1)
                    _qty_s = _cm.group(2)
                    _rest  = _cm.group(3).strip()

                    # Absorber línea siguiente si es continuación de la descripción
                    if _j + 1 < len(_lines):
                        _nl = _lines[_j + 1].strip()
                        if (_nl and not re.match(r'^\d{6,12}\s', _nl)
                                and not _stop.match(_nl)
                                and re.search(r'[A-Za-z]', _nl)):
                            _rest += " " + _nl
                            _j += 1

                    # Separar números fusionados: ".13" seguido de dígito → ".13 "
                    _rest_fixed = re.sub(r'(\.\d{2})(\d)', r'\1 \2', _rest)

                    _raw_nums = re.findall(_num_re, _rest_fixed)
                    _nums = []
                    for _rn in _raw_nums:
                        try:
                            _nums.append(_parse_num(_rn))
                        except Exception:
                            pass

                    if len(_nums) >= 3:
                        _qty    = float(_qty_s)
                        # Estructura esperada (de izquierda a derecha):
                        #   Neto  [%Desc]  IVA%  IT  C.Final  Sub-Total
                        # El último siempre es Sub-Total, el primero es Neto
                        _sub    = _nums[-1]
                        _neto   = _nums[0]
                        # IVA%: buscar el valor típico 21.0 o 10.5 en las posiciones centrales
                        _iva    = 21.0
                        for _n in _nums[1:-1]:
                            if abs(_n - 21.0) < 0.6:
                                _iva = 21.0; break
                            if abs(_n - 10.5) < 0.6:
                                _iva = 10.5; break

                        # Descripción: texto antes del primer número + texto después del último
                        # Esto captura el modelo que queda al final de la línea (ej: G2110, G3110)
                        _fst_m  = re.search(_num_re, _rest_fixed)
                        _all_ms = list(re.finditer(_num_re, _rest_fixed))
                        _pre  = (_rest_fixed[:_fst_m.start()].strip()
                                 if _fst_m else _rest_fixed.strip())
                        _post = (_rest_fixed[_all_ms[-1].end():].strip()
                                 if _all_ms else "")
                        _desc = ((_pre + " " + _post).strip() if _post else _pre)
                        _desc = re.sub(r'\s+', ' ', _desc).strip()

                        fields["lineas"].append({
                            "codigo":      _cod,
                            "descripcion": _desc,
                            "cantidad":    _qty,
                            "precio_unit": round(_neto, 2),
                            "iva_pct":     _iva,
                            "subtotal":    round(_sub, 2),
                        })
                _j += 1

    # ── Fallback: La Anónima / tabular texto plano (Cod.Art.Prov.) ──────────
    # Header: "Cod.Art. Cod.Art.Prov. Descripción Marca Bto. Cont. U/M Cant. Costo % Bonif. % Iva Total"
    # Línea:  "2383809 LCANO00015 BOTELLA GL-190 CYA CANON 1 1 CU 25 17098.3 0.00 21.00 427458.00"
    # Números en formato US (punto como decimal): 17098.3  0.00  21.00  427458.00
    if not fields["lineas"] and text:
        _la_lines = text.split("\n")
        _la_hdr = None
        for _li, _ll in enumerate(_la_lines):
            if re.search(r'Cod\.Art\.Prov', _ll, re.IGNORECASE):
                _la_hdr = _li
                break
        if _la_hdr is not None:
            _la_stop = re.compile(
                r'^(?:Sub[-\s]?[Tt]otal|Total\s|Totales|Bonificaci[oó]n|Observaciones|'
                r'Sr\.?\s+Proveedor|Toda\s+Orden|RESERVAR)',
                re.IGNORECASE)
            for _ll in _la_lines[_la_hdr + 1:]:
                _lraw = _ll.strip()
                if not _lraw or _la_stop.match(_lraw):
                    break
                # INT_CODE PROV_CODE ... resto
                _lcm = re.match(r'^(\d{5,10})\s+([A-Z][A-Z0-9]{2,})\s+(.+)', _lraw)
                if not _lcm:
                    continue
                _lint_code  = _lcm.group(1)
                _lprov_code = _lcm.group(2)
                _lrest      = _lcm.group(3).strip()
                # Extraer los últimos 5 tokens numéricos (qty costo bonif iva% total)
                # La Anónima usa formato US: solo dígitos y punto decimal
                _ltoks     = _lrest.split()
                _lnum_toks = []
                for _lt in reversed(_ltoks):
                    if re.match(r'^\d+(?:\.\d+)?$', _lt) and len(_lnum_toks) < 5:
                        _lnum_toks.insert(0, _lt)
                    elif _lnum_toks:
                        break
                if len(_lnum_toks) < 4:
                    continue
                _lqty   = float(_lnum_toks[0])
                _lcosto = float(_lnum_toks[1])
                _liva   = float(_lnum_toks[3]) if len(_lnum_toks) > 3 else 21.0
                _ltotal = float(_lnum_toks[4]) if len(_lnum_toks) > 4 else _lqty * _lcosto
                # Descripción: tokens antes de los numéricos; limpiar U/M y Bto/Cont al final
                _lnum_start = len(_ltoks) - len(_lnum_toks)
                _ldesc = " ".join(_ltoks[:_lnum_start]).strip()
                _ldesc = re.sub(r'\s+\d+\s+\d+\s+[A-Z]{1,3}\s*$', '', _ldesc).strip()
                fields["lineas"].append({
                    "codigo":      _lprov_code,
                    "descripcion": _ldesc,
                    "cantidad":    _lqty,
                    "precio_unit": _lcosto,
                    "iva_pct":     _liva,
                    "subtotal":    _ltotal,
                })

    return fields, all_tables, text


def extract_excel_oc_fields(file_bytes):
    """
    Parser para Órdenes de Compra en formato Excel (ej: Fusion Bikes / Fanttik).
    Detecta automáticamente columnas: SKU, Modelo/Descripción, EAN, IVA, Precio s/IVA, Pedido (qty).
    Solo incluye filas con Pedido > 0.
    Retorna (fields_dict) con estructura compatible con oc_fields.
    Sin CUIT ni condiciones de pago (el usuario las completa a mano).
    """
    fields = {
        "cuit": "", "numero_oc": "", "fecha": "", "fecha_iso": "",
        "condiciones_pago": "", "dias_pago": None,
        "lineas": [],
        "subtotal_neto": "", "iva_21": "", "iva_105": "", "total": "",
        "fuente": "excel",
    }
    try:
        import openpyxl
        wb = openpyxl.load_workbook(BytesIO(file_bytes), data_only=True)
        ws = wb.active
    except Exception:
        return fields

    # Paso 1: encontrar la primera fila que tenga 'SKU' o 'Pedido'
    col_map = {}
    hdr_row_idx = None
    all_rows = list(ws.iter_rows(values_only=True))
    for ri, row in enumerate(all_rows[:25]):
        vals = [str(c or "").strip().lower() for c in row]
        if any(v == 'sku' for v in vals) or any(v in ('pedido', 'cantidad') for v in vals):
            hdr_row_idx = ri
            for ci, h in enumerate(vals):
                if h == 'sku':
                    col_map['sku'] = ci
                elif h in ('ean', 'ean13', 'codigo', 'código'):
                    if 'ean' not in col_map:
                        col_map['ean'] = ci
                elif h in ('modelo', 'model', 'nombre'):
                    if 'modelo' not in col_map:
                        col_map['modelo'] = ci
                elif h == 'iva':
                    col_map['iva'] = ci
                elif 'precio s/iva' in h or 'precio sin' in h or ('precio' in h and 'iva' in h):
                    if 'precio' not in col_map:
                        col_map['precio'] = ci
                elif h == 'pvp' and 'precio' not in col_map:
                    col_map['pvp'] = ci
                elif h in ('pedido', 'cantidad', 'qty'):
                    col_map['pedido'] = ci
                elif 'subtotal' in h:
                    col_map['subtotal'] = ci
                elif 'caracteristic' in h or 'descripci' in h or 'detalle' in h:
                    if 'descripcion' not in col_map:
                        col_map['descripcion'] = ci
            break

    if 'pedido' not in col_map:
        return fields

    # Fallback precio: usar PVP si no hay precio s/IVA
    precio_col = col_map.get('precio') if col_map.get('precio') is not None else col_map.get('pvp')

    # Paso 2: leer filas de datos (desde después del header)
    _header_kws = {'sku', 'modelo', 'model', 'ean', 'pvp', 'iva', 'pedido', 'cantidad', 'stock'}
    for row in all_rows[hdr_row_idx + 1:]:
        if not any(c is not None for c in row):
            continue
        def _gcell(ci):
            return row[ci] if ci is not None and ci < len(row) else None

        sku_val = str(_gcell(col_map.get('sku')) or "").strip()
        # Saltar filas que son sub-headers repetidos
        if not sku_val or sku_val.lower() in _header_kws:
            continue

        pedido_raw = _gcell(col_map.get('pedido'))
        if pedido_raw is None:
            continue
        try:
            pedido_qty = float(str(pedido_raw).replace(",", ".").strip())
        except Exception:
            continue
        if pedido_qty <= 0:
            continue

        modelo_val = str(_gcell(col_map.get('modelo')) or "").strip()
        desc_val   = str(_gcell(col_map.get('descripcion')) or "").strip()
        ean_val    = str(_gcell(col_map.get('ean')) or "").strip()

        iva_raw = _gcell(col_map.get('iva'))
        try:
            iva_f = float(str(iva_raw or "0.21").replace(",", ".").strip())
            iva_pct = round(iva_f * 100, 1) if iva_f < 1 else round(iva_f, 1)
        except Exception:
            iva_pct = 21.0

        precio_raw = _gcell(precio_col)
        try:
            precio_unit = float(str(precio_raw or "0").replace(",", ".").strip())
        except Exception:
            precio_unit = 0.0

        subtotal_raw = _gcell(col_map.get('subtotal'))
        try:
            subtotal = float(str(subtotal_raw or "0").replace(",", ".").strip())
            if subtotal <= 0:
                subtotal = precio_unit * pedido_qty
        except Exception:
            subtotal = precio_unit * pedido_qty

        desc_full = modelo_val or desc_val or sku_val

        fields["lineas"].append({
            "codigo":      sku_val,
            "descripcion": desc_full[:120],
            "ean":         ean_val,
            "cantidad":    pedido_qty,
            "precio_unit": precio_unit,
            "iva_pct":     iva_pct,
            "subtotal":    subtotal,
        })

    return fields


def classify_document(text, carpeta_id=""):
    """
    Clasifica un documento de importación.
    Prioridad: sin-texto → no-aplica → CUIT → keyword.
    Retorna dict con: tipo, label, partner_id, journal_id, doc_type,
                      no_aplica (bool), mismatch (bool), extracted (dict).
    """
    _other = {"tipo":"other","label":"Otro comprobante","partner_id":None,
              "journal_id":10,"doc_type":None,
              "no_aplica":False,"mismatch":False,"extracted":{}}

    # ── Sin texto ─────────────────────────────────────────────────────────
    if not text.strip():
        return {**_other, "label":"Sin texto — no aplica", "no_aplica":True}

    tu = text.upper()
    extracted = {}

    # ── Extracción de campos comunes ──────────────────────────────────────
    # Fecha (DD/MM/YYYY, DD-MM-YYYY, DD.MM.YYYY)
    _fm = re.search(r'(\d{2}[/\-\.]\d{2}[/\-\.]\d{4})', text)
    if _fm:
        extracted["fecha"] = parse_ar_date(_fm.group(1))

    # CUIT (con o sin guiones): captura el primero que aparece
    _cm = re.search(r'(\d{2})[-\s]?(\d{8})[-\s]?(\d)\b', text)
    if _cm:
        extracted["cuit"]      = f"{_cm.group(1)}-{_cm.group(2)}-{_cm.group(3)}"
        extracted["cuit_norm"] = _cm.group(1) + _cm.group(2) + _cm.group(3)

    # Monto total
    _am = re.search(r'TOTAL[^\d$]*([\d\.]+,[\d]{2})', tu)
    if _am:
        extracted["monto"] = normalize_amount(_am.group(1))

    # TC desde texto: "Tipo de cambio USD 1 = ARG 1.505,18000" o similar
    _tc_m = re.search(
        r'(?:TIPO\s+DE\s+CAMBIO|T\.?\s*C\.?)\s*.*?(?:USD\s*1\s*=\s*(?:ARG\s*)?)?([\d\.]+,[\d]+)',
        tu, re.DOTALL)
    if _tc_m:
        extracted["tc_pdf"] = normalize_amount(_tc_m.group(1))

    # N° comprobante — patrón argentino: letra + 4 dígitos + guion + 8 dígitos
    _nr = re.search(r'\b([A-Z]\d{4}-\d{8})\b', text)
    if _nr:
        extracted["nro_comp"] = _nr.group(1)
    else:
        _nr2 = re.search(r'N[°º]?\s*(?:COMP\.?|FACTURA|COMPROBANTE)?[:\s]+([A-Z0-9\-]{5,20})', tu)
        if _nr2:
            extracted["nro_comp"] = _nr2.group(1).strip()

    # ── No aplica ─────────────────────────────────────────────────────────
    if "VOLANTE ELECTRONICO DE PAGO" in tu or (
            re.search(r'\bVEP\b', tu) and ("PAGO" in tu or "AFIP" in tu)):
        return {**_other, "label":"VEP — no aplica", "no_aplica":True, "extracted":extracted}

    if re.search(r'\bPRESUPUESTO\b', tu) and not re.search(r'\bFACTURA\b', tu):
        return {**_other, "label":"Presupuesto — no aplica", "no_aplica":True, "extracted":extracted}

    if "BILL OF LADING" in tu or ("CONOCIMIENTO" in tu and "EMBARQUE" in tu):
        return {**_other, "label":"Bill of Lading — no aplica", "no_aplica":True, "extracted":extracted}

    # ── Mismatch de carpeta ────────────────────────────────────────────────
    mismatch = False
    if carpeta_id:
        _carp_norm = re.sub(r'[_\s]', '_', carpeta_id.strip().upper())
        _refs = re.findall(r'LUMI[_\s]?\d+[A-Z]?', tu)
        for _r in _refs:
            _r_norm = re.sub(r'[_\s]', '_', _r.strip())
            if _r_norm != _carp_norm:
                mismatch = True
                extracted["mismatch_ref"] = _r_norm
                break

    # ── Clasificación por CUIT ─────────────────────────────────────────────
    cuit_norm = extracted.get("cuit_norm", "")
    # Buscar en todo el texto (por si el PDF tiene CUITs sin guiones)
    _tu_no_sep = tu.replace("-","").replace(" ","")
    for _ck, _cfg in CUIT_TO_PARTNER.items():
        if cuit_norm == _ck or _ck in _tu_no_sep:
            return {**_cfg, "no_aplica":False, "mismatch":mismatch, "extracted":extracted}

    # ── Fallback por keyword ───────────────────────────────────────────────
    _kw = [
        ("PETDUR",                {"tipo":"petdur", "label":"Bill PETDUR (Etapa 1)",  "partner_id":49328,"journal_id":71,"doc_type":None}),
        ("217016440010",          {"tipo":"petdur", "label":"Bill PETDUR (Etapa 1)",  "partner_id":49328,"journal_id":71,"doc_type":None}),
        ("26001IC",               {"tipo":"di_afip","label":"DI AFIP (Etapa 2)",      "partner_id":9,    "journal_id":10,"doc_type":66}),
        ("DECLARACION DE IMPORT", {"tipo":"di_afip","label":"DI AFIP (Etapa 2)",      "partner_id":9,    "journal_id":10,"doc_type":66}),
        ("SUBREGIMEN",            {"tipo":"di_afip","label":"DI AFIP (Etapa 2)",      "partner_id":9,    "journal_id":10,"doc_type":66}),
        ("SENASA",                {"tipo":"nac",    "label":"Bill SENASA (Etapa 2a)", "partner_id":48827,"journal_id":10,"doc_type":None}),
        ("TRICE",                 {"tipo":"nac",    "label":"Bill TRICE Transport (Etapa 2a)", "partner_id":48825,"journal_id":10,"doc_type":None}),
        ("TERMINAL 4",            {"tipo":"nac",    "label":"Bill Terminal 4 SA (Etapa 2a)",   "partner_id":48828,"journal_id":10,"doc_type":None}),
        ("MUNDO COMEX",           {"tipo":"nac",    "label":"Bill Mundo Comex (Etapa 2a)",     "partner_id":48826,"journal_id":10,"doc_type":None}),
    ]
    for _kword, _cfg in _kw:
        if _kword in tu:
            return {**_cfg, "no_aplica":False, "mismatch":mismatch, "extracted":extracted}

    return {**_other, "mismatch":mismatch, "extracted":extracted}


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
                        url = odoo_url("account.move", move_id)
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
                _dup_url = odoo_url("account.move", _dup_id)
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
                                _dup2_url = odoo_url("account.move", _dup2_id)
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
                        url = odoo_url("account.move", move_id)
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
            # ── Parseo inteligente de Excel de pedido ─────────────────────
            with st.spinner("Leyendo Excel..."):
                oc_fields_xl = extract_excel_oc_fields(file_bytes)
            _lineas_xl = oc_fields_xl.get("lineas", [])
            if _lineas_xl:
                st.caption(f"✅ {len(_lineas_xl)} productos con pedido > 0 detectados automáticamente.")
            else:
                st.warning("No se detectaron productos con cantidad pedida. Revisá el archivo.")

            # ── Cliente: CUIT editable + auto-lookup ──────────────────────
            st.markdown("##### 🏢 Cliente")
            _cuit_key_xl = f"xl_cuit_{uf.name}"
            _pid_key_xl  = f"xl_pid_{uf.name}"
            _pnm_key_xl  = f"xl_pnm_{uf.name}"
            for _k, _dv in [(_cuit_key_xl,""), (_pid_key_xl, None), (_pnm_key_xl,"")]:
                if _k not in st.session_state:
                    st.session_state[_k] = _dv

            _xl_cuit = st.text_input(
                "CUIT del cliente",
                key=_cuit_key_xl,
                placeholder="30-12345678-9",
                help="El Excel no trae cliente. Ingresá el CUIT para buscarlo en Odoo.",
            )
            # Auto-lookup al escribir el CUIT
            _xl_pid = st.session_state[_pid_key_xl]
            _xl_pnm = st.session_state[_pnm_key_xl]
            if _xl_cuit and len(re.sub(r'[^0-9]', '', _xl_cuit)) >= 10:
                if not _xl_pid:
                    _partner_xl = search_partner_by_cuit(models_url, uid, api_key, _xl_cuit)
                    if _partner_xl:
                        st.session_state[_pid_key_xl] = _partner_xl[0]
                        st.session_state[_pnm_key_xl] = _partner_xl[1]
                        _xl_pid = _partner_xl[0]
                        _xl_pnm = _partner_xl[1]
                if _xl_pid:
                    st.success(f"✅ Cliente identificado: **{_xl_pnm}**")
                else:
                    st.warning(f"⚠️ CUIT {_xl_cuit} no encontrado en Odoo.")
                    with st.expander("➕ Crear nuevo cliente", expanded=False):
                        _xnc1, _xnc2 = st.columns(2)
                        _xnc_name   = _xnc1.text_input("Razón social *", key=f"xl_nc_name_{uf.name}")
                        _xnc_street = _xnc1.text_input("Dirección", key=f"xl_nc_st_{uf.name}")
                        _xnc_phone  = _xnc2.text_input("Teléfono", key=f"xl_nc_ph_{uf.name}")
                        _xnc_email  = _xnc2.text_input("Email", key=f"xl_nc_em_{uf.name}")
                        if st.button("Crear cliente", key=f"xl_btn_nc_{uf.name}"):
                            if _xnc_name and _xl_cuit:
                                try:
                                    _new_xl_pid = create_partner(models, uid, api_key,
                                        _xnc_name, _xl_cuit, _xnc_street, _xnc_phone, _xnc_email)
                                    st.session_state[_pid_key_xl] = _new_xl_pid
                                    st.session_state[_pnm_key_xl] = _xnc_name
                                    st.success(f"✅ Cliente creado (ID {_new_xl_pid})")
                                    st.rerun()
                                except Exception as _xe:
                                    st.error(f"❌ {_xe}")
                            else:
                                st.warning("Razón social y CUIT son obligatorios.")
            else:
                st.info("📌 Ingresá el CUIT del cliente para continuar.")

            # ── Plazo de pago (siempre visible, opciones del sistema) ─────
            st.markdown("##### 📅 Plazo de pago")
            _all_pts_xl  = get_all_payment_terms(models_url, uid, api_key)
            _pt_opts_xl  = {name: pid for pid, name in _all_pts_xl}
            _xl_pt_def   = 0
            _xl_cpt_name = ""
            if _xl_pid and _pt_opts_xl:
                _xl_cpt_id, _xl_cpt_name = get_customer_payment_terms(models_url, uid, api_key, _xl_pid)
                if _xl_cpt_name and _xl_cpt_name in _pt_opts_xl:
                    _xl_pt_def = list(_pt_opts_xl.keys()).index(_xl_cpt_name)
            _xl_pt_names = list(_pt_opts_xl.keys()) if _pt_opts_xl else ["(sin opciones)"]
            _xl_pt_sel   = st.selectbox("Plazo de pago a usar",
                options=_xl_pt_names, index=_xl_pt_def, key=f"xl_pt_{uf.name}",
                help=f"Plazo cargado en Odoo para este cliente: {_xl_cpt_name or 'no configurado'}")
            _xl_pt_id = _pt_opts_xl.get(_xl_pt_sel)

            # ── Productos ─────────────────────────────────────────────────
            st.markdown("##### 📦 Productos")
            _xl_enriched = []
            if _lineas_xl:
                for _ln in _lineas_xl:
                    _prods = search_product_by_code_or_name(
                        models_url, uid, api_key,
                        code=_ln.get("codigo",""),
                        name_keywords=_ln.get("descripcion",""),
                        limit=1)
                    _op    = _prods[0] if _prods else None
                    _cost  = float(_op["standard_price"]) if _op else 0.0
                    _price = float(_ln.get("precio_unit") or 0)
                    _margin = ((_price - _cost) / _price * 100) if _price > 0 else 0.0
                    _xl_enriched.append({**_ln, "odoo_product": _op,
                                          "cost": _cost, "margin_pct": _margin})
                _xl_df_rows = []
                for _el in _xl_enriched:
                    _xl_df_rows.append({
                        "SKU/Código":    _el.get("codigo",""),
                        "Descripción":   _el.get("descripcion",""),
                        "Cant.":         _el.get("cantidad",0),
                        "Precio s/IVA":  fmt_ars(_el.get("precio_unit",0)),
                        "IVA %":         _el.get("iva_pct",""),
                        "Subtotal":      fmt_ars(_el.get("subtotal",0)),
                        "Costo Odoo":    fmt_ars(_el.get("cost",0)),
                        "Margen %":      f"{_el.get('margin_pct',0):.1f}%",
                        "Match Odoo":    (_el["odoo_product"]["name"]
                                          if _el.get("odoo_product")
                                          else f"⚠️ [{_el.get('codigo','')}]"),
                    })
                st.dataframe(pd.DataFrame(_xl_df_rows), use_container_width=True, hide_index=True)
            else:
                st.info("No se detectaron productos con cantidad pedida.")

            # ── Resumen financiero ─────────────────────────────────────────
            _xl_neto  = sum(float(_el.get("subtotal",0)) for _el in _xl_enriched)
            _xl_iva   = sum(
                float(_el.get("subtotal",0)) * float(_el.get("iva_pct",21)) / 100
                for _el in _xl_enriched)
            _xl_total = _xl_neto + _xl_iva
            if _xl_enriched:
                st.markdown("##### 💰 Resumen financiero")
                _xlrf1, _xlrf2, _xlrf3 = st.columns(3)
                _xlrf1.metric("Neto s/IVA",    fmt_ars(_xl_neto))
                _xlrf2.metric("IVA",           fmt_ars(_xl_iva))
                _xlrf3.metric("Total c/IVA",   fmt_ars(_xl_total))

            # ── Crear pedido ───────────────────────────────────────────────
            st.markdown("---")
            _xl_btn_disabled = not bool(_xl_pid)
            if _xl_btn_disabled:
                st.caption("🔒 Identificá el cliente para habilitar la creación del pedido.")
            if st.button("⬆️ Crear pedido en Odoo", key=f"btn_xl_order_{uf.name}",
                         type="primary", disabled=_xl_btn_disabled):
                with st.spinner("Creando pedido..."):
                    try:
                        _xl_order_lines = [{
                            "product_id":  _el["odoo_product"]["id"] if _el.get("odoo_product") else None,
                            "descripcion": _el.get("descripcion",""),
                            "cantidad":    _el.get("cantidad", 1),
                            "precio_unit": _el.get("precio_unit", 0),
                        } for _el in _xl_enriched]
                        _xl_order_id = create_sale_order(
                            models, uid, api_key,
                            partner_id      = _xl_pid,
                            note            = f"Importado desde {uf.name}",
                            lines           = _xl_order_lines,
                            filename        = uf.name,
                            file_bytes      = file_bytes,
                            mimetype        = mimetype,
                            payment_term_id = _xl_pt_id or None,
                        )
                        url = odoo_url("sale.order", _xl_order_id)
                        st.success(f"✅ Pedido creado — [Abrir en Odoo]({url})")
                        st.session_state.history.append({"tipo":"Pedido cliente",
                            "archivo":uf.name,"id":_xl_order_id,"url":url,"estado":"✅"})
                    except Exception as _xe:
                        st.error(f"❌ {_xe}")
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

            # Session state para overrides manuales de producto
            _ss_overrides = f"prod_ov_{uf.name}"
            if _ss_overrides not in st.session_state:
                st.session_state[_ss_overrides] = {}

            if _lineas_oc:
                for _li, _ln in enumerate(_lineas_oc):
                    _override = st.session_state[_ss_overrides].get(_li)
                    if _override:
                        _op = _override
                    else:
                        _prods = search_product_by_code_or_name(
                            models_url, uid, api_key,
                            code=_ln.get("codigo",""),
                            name_keywords=_ln.get("descripcion",""),
                            ean13=_ln.get("ean13",""),
                            limit=1,
                        )
                        _op = _prods[0] if _prods else None
                    _cost  = float(_op["standard_price"]) if _op else 0.0
                    _price = float(_ln.get("precio_unit") or 0)
                    _margin = ((_price - _cost) / _price * 100) if _price > 0 else 0.0
                    _enriched.append({**_ln,
                        "odoo_product": _op,
                        "cost": _cost,
                        "margin_pct": _margin,
                    })

                def _fmt_cost(v):
                    """Igual que fmt_ars pero muestra $ 0,00 para costo cero."""
                    try:
                        s = "{:,.2f}".format(float(v))
                        return "$ " + s.replace(",", "X").replace(".", ",").replace("X", ".")
                    except Exception:
                        return "$ 0,00"

                _df_rows = []
                for _el in _enriched:
                    _df_rows.append({
                        "Código":        _el.get("codigo",""),
                        "Descripción":   _el.get("descripcion",""),
                        "Cant.":         _el.get("cantidad",0),
                        "Precio unit.":  fmt_ars(_el.get("precio_unit",0)),
                        "IVA %":         _el.get("iva_pct","21"),
                        "Subtotal":      fmt_ars(_el.get("subtotal",0)),
                        "Costo Odoo":    _fmt_cost(_el.get("cost",0)),
                        "Margen %":      f"{_el.get('margin_pct',0):.1f}%",
                        "Match Odoo":    (_el["odoo_product"]["name"]
                                          if _el.get("odoo_product")
                                          else f"⚠️ [{_el.get('codigo','')}]"),
                    })
                st.dataframe(pd.DataFrame(_df_rows), use_container_width=True, hide_index=True)
                st.caption("✏️ ¿Algún match incorrecto? Expandí el panel de abajo para corregirlo.")

                # ── Edición / reasignación de match para todas las líneas ───────
                _n_unmatched = sum(1 for el in _enriched if not el.get("odoo_product"))
                _exp_label = (
                    f"🔍 {_n_unmatched} producto(s) sin match — asignar manualmente"
                    if _n_unmatched > 0
                    else "✏️ Editar asignaciones de productos"
                )
                with st.expander(_exp_label, expanded=(_n_unmatched > 0)):
                    st.caption("Escribí nombre o código Odoo para reasignar cualquier línea.")
                    for _li2, _el2 in enumerate(_enriched):
                            _cur_match = (_el2.get("odoo_product") or {}).get("name","")
                            _cur_code  = (_el2.get("odoo_product") or {}).get("default_code","")
                            _match_str = f"{_cur_match} [{_cur_code}]" if _cur_match else "⚠️ Sin match"
                            st.caption(
                                f"**{_el2.get('descripcion','')}** · "
                                f"Código OC: `{_el2.get('codigo','')}` · "
                                f"Match actual: *{_match_str}*"
                            )
                            _sk_q   = f"mq_{uf.name}_{_li2}"
                            _sk_sel = f"ms_{uf.name}_{_li2}"
                            _sk_btn = f"mc_{uf.name}_{_li2}"
                            _mq = st.text_input(
                                "Reasignar — buscar en Odoo (nombre o código)",
                                key=_sk_q,
                                placeholder="Ej: G2110  o  LCANO00015",
                            )
                            if _mq and len(_mq) >= 2:
                                _res = search_product_by_code_or_name(
                                    models_url, uid, api_key,
                                    code=_mq,
                                    name_keywords=_mq,
                                    limit=8,
                                )
                                if _res:
                                    _opts_labels = [
                                        f"{r['name']}  [{r.get('default_code','')}]"
                                        for r in _res
                                    ]
                                    _chosen_lbl = st.selectbox(
                                        "Resultados", _opts_labels, key=_sk_sel
                                    )
                                    _chosen_idx = _opts_labels.index(_chosen_lbl)
                                    if st.button("✅ Confirmar asignación", key=_sk_btn):
                                        st.session_state[_ss_overrides][_li2] = _res[_chosen_idx]
                                        st.rerun()
                                else:
                                    st.caption("Sin resultados — probá con otro término.")
                            if _li2 < len(_enriched) - 1:
                                st.divider()
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

            # Cuando hay líneas enriquecidas, usar valores calculados (más exactos).
            # Solo caer en los valores del PDF si no hay líneas detectadas.
            if _enriched:
                _show_neto   = _calc_neto
                _show_iva21  = _calc_iva21
                _show_iva105 = _calc_iva105
                _show_iva    = _calc_iva
                _show_total  = _calc_neto + _calc_iva
            else:
                _show_neto   = float(oc_fields.get("subtotal_neto") or 0)
                _show_iva21  = float(oc_fields.get("iva_21")  or 0)
                _show_iva105 = float(oc_fields.get("iva_105") or 0)
                _show_iva    = _show_iva21 + _show_iva105
                _show_total  = float(oc_fields.get("total") or 0) or (_show_neto + _show_iva)

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

            _all_pts_pdf = get_all_payment_terms(models_url, uid, api_key)
            _pt_opts_pdf = {name: pid for pid, name in _all_pts_pdf}

            if _pt_id and _pt_name:
                _odoo_dias_est = parse_payment_terms(_pt_name)
                _hay_disc = (
                    _oc_dias is not None
                    and _odoo_dias_est is not None
                    and abs(_odoo_dias_est - _oc_dias) > 3
                )
                if _hay_disc:
                    st.warning(
                        f"⚠️ Discrepancia: la OC indica **{_oc_dias} días** "
                        f"({_oc_cond_str}), pero el cliente tiene **{_pt_name}** en Odoo."
                    )
                # Selectbox con todas las opciones del sistema
                _pt_def_idx = 0
                if _pt_name in _pt_opts_pdf:
                    _pt_def_idx = list(_pt_opts_pdf.keys()).index(_pt_name)
                _oc_hint = f" | OC: {_oc_cond_str}" if _oc_cond_str else (f" | OC: {_oc_dias} días" if _oc_dias else "")
                _pt_sel_pdf = st.selectbox(
                    "Plazo de pago a usar",
                    options=list(_pt_opts_pdf.keys()),
                    index=_pt_def_idx,
                    key=f"pt_sel_{uf.name}",
                    help=f"Plazo del cliente en Odoo: {_pt_name}{_oc_hint}",
                )
                _pt_choice_id = _pt_opts_pdf.get(_pt_sel_pdf)
                if not _hay_disc:
                    st.caption(f"✅ Plazo del cliente en Odoo: **{_pt_name}**"
                               + (f" — OC: {_oc_cond_str}" if _oc_cond_str else ""))
            elif _oc_dias:
                st.info(f"📅 OC indica **{_oc_dias} días** ({_oc_cond_str}) "
                        f"— cliente sin plazo configurado en Odoo.")
            elif _pt_id:
                st.info(f"📅 Plazo del cliente en Odoo: **{_pt_name}**")

            # ── SECCIÓN 6: ASIENTO ESTIMADO ───────────────────────────────
            st.markdown("##### 📒 Asiento estimado en Odoo")
            _iva_rows_md = ""
            if _show_iva105 > 0:
                _iva_rows_md += f"| IVA Débito Fiscal 10,5% | | {fmt_ars(_show_iva105)} |\n"
            if _show_iva21 > 0:
                _iva_rows_md += f"| IVA Débito Fiscal 21% | | {fmt_ars(_show_iva21)} |"
            elif _show_iva > 0 and not _iva_rows_md:
                _iva_rows_md += f"| IVA Débito Fiscal | | {fmt_ars(_show_iva)} |"
            st.markdown(
                f"| Cuenta | Debe | Haber |\n"
                f"|---|---|---|\n"
                f"| Cuentas por Cobrar (Clientes) | {fmt_ars(_show_total)} | |\n"
                f"| Ventas / Ingresos | | {fmt_ars(_show_neto)} |\n"
                + _iva_rows_md
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
                        url = odoo_url("sale.order", order_id)
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

        # ── Input carpeta ─────────────────────────────────────────
        col_ci, col_btns = st.columns([3, 2])
        carp_in = col_ci.text_input("Carpeta", value=st.session_state.carpeta_id,
            placeholder="LUMI_293", key="input_carpeta", label_visibility="collapsed")
        with col_btns:
            _b1, _b2, _b3 = st.columns(3)
            load_btn   = _b1.button("🔍 Cargar",   key="btn_load_carp",   use_container_width=True)
            reset_btn  = _b2.button("🔄 Nueva",    key="btn_reset_carp",  use_container_width=True)
            cancel_btn = _b3.button("❌ Cancelar", key="btn_cancel_carp", use_container_width=True)

        if reset_btn or cancel_btn:
            for k in ["carpeta_id", "carpeta_po", "carpeta_bills", "carpeta_lc_id"]:
                st.session_state[k] = DEFAULTS.get(k, "")
            st.session_state.etapas = {k: False for k, *_ in ETAPAS_DEF}
            if "carp_data" in st.session_state:
                del st.session_state["carp_data"]
            st.rerun()

        if carp_in != st.session_state.carpeta_id:
            st.session_state.carpeta_id = carp_in
            if "carp_data" in st.session_state:
                del st.session_state["carp_data"]

        if load_btn and st.session_state.carpeta_id:
            load_carpeta_full.clear()
            with st.spinner(f"Cargando {st.session_state.carpeta_id} desde Odoo..."):
                cdata = load_carpeta_full(models_url, uid, api_key, st.session_state.carpeta_id)
            st.session_state["carp_data"] = cdata
            if cdata.get("po"):
                st.session_state.carpeta_po  = cdata["po"]
                st.session_state.etapas["0"] = True
            if cdata.get("bills"):
                st.session_state.carpeta_bills = [b["id"] for b in cdata["bills"]]
            for k, v in cdata.get("stages", {}).items():
                if v:
                    st.session_state.etapas[k] = True
            st.rerun()

        if not st.session_state.carpeta_id:
            st.info("Ingresá el número de carpeta y presioná **🔍 Cargar** para traer los datos de Odoo.")
            st.stop()

        carp_data = st.session_state.get("carp_data")


        st.divider()

        # ── Subir comprobantes ────────────────────────────────────
        st.markdown("#### ⬆️ Subir comprobantes")
        _etapas_done = st.session_state.etapas
        _missing = []
        if not _etapas_done.get("1"):  _missing.append("Etapa 1: Bill PETDUR (USD)")
        if not _etapas_done.get("2"):  _missing.append("Etapa 2: DI AFIP")
        if not _etapas_done.get("2a"): _missing.append("Etapa 2a: TRICE / Terminal 4 / Mundo Comex / SENASA")
        if _missing:
            st.caption("⏳ Pendiente: " + "  ·  ".join(_missing))

        st.info("💡 Podés subir todos los archivos de la carpeta a la vez: "
                "seleccioná múltiples archivos con **Ctrl+A** en el explorador, "
                "o arrastrá y soltá varios archivos al mismo tiempo.")

        TIPO_OPTIONS_IMP = {
            "Bill PETDUR (Etapa 1)":           {"tipo":"petdur",  "partner_id":49328,"journal_id":71, "doc_type":None},
            "DI AFIP (Etapa 2)":               {"tipo":"di_afip", "partner_id":9,    "journal_id":10, "doc_type":66},
            "Bill TRICE Transport (Etapa 2a)": {"tipo":"nac",     "partner_id":48825,"journal_id":10, "doc_type":None},
            "Bill Terminal 4 SA (Etapa 2a)":   {"tipo":"nac",     "partner_id":48828,"journal_id":10, "doc_type":None},
            "Bill Mundo Comex (Etapa 2a)":     {"tipo":"nac",     "partner_id":48826,"journal_id":10, "doc_type":None},
            "Bill SENASA (Etapa 2a)":          {"tipo":"nac",     "partner_id":48827,"journal_id":10, "doc_type":None},
            "Otro comprobante":                {"tipo":"other",   "partner_id":None, "journal_id":10, "doc_type":None},
        }

        imp_files = st.file_uploader(
            f"Documentos de {st.session_state.carpeta_id} — seleccioná uno o todos a la vez",
            type=["pdf","jpg","jpeg","png"], accept_multiple_files=True, key="import_uploader")

        classified_docs = []
        if imp_files:
            st.markdown("**Clasificación automática — revisá y ajustá si hace falta**")
            for uf in imp_files:
                ext        = uf.name.rsplit(".", 1)[-1].lower()
                file_bytes = uf.read()
                mimetype   = MIMETYPES.get(ext, "application/octet-stream")
                raw_text   = ""
                if ext == "pdf":
                    _, raw_text = extract_pdf_fields(file_bytes)
                auto        = classify_document(raw_text, st.session_state.carpeta_id)
                default_lbl = auto["label"] if auto["label"] in TIPO_OPTIONS_IMP else "Otro comprobante"
                _ext_info   = auto.get("extracted", {})
                _icon       = "🚫" if auto.get("no_aplica") else ("⚠️" if auto.get("mismatch") else "📎")
                with st.expander(f"{_icon} {uf.name} — {auto['label']}", expanded=True):
                    # Warnings de no-aplica y mismatch
                    if auto.get("no_aplica"):
                        st.warning(f"🚫 **Este documento no aplica** ({auto['label']}) — "
                                   "no se cargará en Odoo. Podés ignorarlo.")
                    elif auto.get("mismatch"):
                        st.warning(f"⚠️ **Carpeta mismatch** — este doc referencia "
                                   f"**{_ext_info.get('mismatch_ref','')}** "
                                   f"pero la carpeta activa es **{st.session_state.carpeta_id}**. "
                                   "Verificá que sea el documento correcto.")

                    # Info extraída del PDF
                    _info_parts = []
                    if _ext_info.get("cuit"):     _info_parts.append(f"CUIT: `{_ext_info['cuit']}`")
                    if _ext_info.get("nro_comp"): _info_parts.append(f"Comprobante: `{_ext_info['nro_comp']}`")
                    if _ext_info.get("fecha"):    _info_parts.append(f"Fecha: `{_ext_info['fecha']}`")
                    if _ext_info.get("monto"):    _info_parts.append(f"Monto: `{_ext_info['monto']}`")
                    if _ext_info.get("tc_pdf"):   _info_parts.append(f"TC (PDF): `{_ext_info['tc_pdf']}`")
                    if _info_parts:
                        st.caption("  ·  ".join(_info_parts))

                    if not auto.get("no_aplica"):
                        ct1, ct2, ct3, ct4 = st.columns([3, 2, 2, 1])
                        tipo_sel  = ct1.selectbox("Tipo", list(TIPO_OPTIONS_IMP.keys()),
                            index=list(TIPO_OPTIONS_IMP.keys()).index(default_lbl), key=f"tipo_{uf.name}")
                        ref_doc   = ct2.text_input("N° comprobante",
                            value=_ext_info.get("nro_comp",""), key=f"ref_d_{uf.name}")
                        fecha_doc = ct3.text_input("Fecha (AAAA-MM-DD)",
                            value=_ext_info.get("fecha",""), key=f"fec_d_{uf.name}",
                            placeholder="2026-05-12")
                        _def_mon  = "USD" if auto.get("tipo") == "petdur" else "ARS"
                        moneda    = ct4.selectbox("Moneda", ["ARS","USD"],
                            index=["ARS","USD"].index(_def_mon), key=f"cur_{uf.name}")
                        if moneda == "USD":
                            _rd = fecha_doc or pd.Timestamp.today().strftime("%Y-%m-%d")
                            _tc_up, _dt_up = get_usd_rate_odoo(models_url, uid, api_key, _rd)
                            st.caption(f"TC Odoo para {_dt_up or _rd}: **$ {_tc_up:,.2f}**"
                                       if _tc_up else "TC no encontrado en Odoo para esa fecha")
                        classified_docs.append({
                            "filename": uf.name, "file_bytes": file_bytes, "mimetype": mimetype,
                            "tipo_cfg": TIPO_OPTIONS_IMP[tipo_sel],
                            "ref": ref_doc, "fecha": fecha_doc, "moneda": moneda,
                        })

            if classified_docs:
                # ── Vista previa antes de crear ────────────────────────────
                _prev_btn, _create_btn = st.columns([1, 2])
                _show_prev = _prev_btn.button("👁️ Vista previa", key="btn_preview_imp",
                                              use_container_width=True)
                if _show_prev or st.session_state.get("_imp_preview_open"):
                    st.session_state["_imp_preview_open"] = True
                    st.markdown("**Resumen de lo que se va a crear en Odoo:**")
                    _prev_rows = []
                    for _d in classified_docs:
                        _cfg = _d["tipo_cfg"]
                        _pid = _cfg.get("partner_id")
                        _pname = PARTNER_TO_TIPO.get(_pid, {}).get("label", "—") if _pid else "Sin asignar"
                        _tc_prev = "—"
                        if _d.get("moneda") == "USD":
                            _ref_date = _d.get("fecha") or pd.Timestamp.today().strftime("%Y-%m-%d")
                            _tv, _td = get_usd_rate_odoo(models_url, uid, api_key, _ref_date)
                            _tc_prev = f"$ {_tv:,.0f}" if _tv else "sin TC"
                        _prev_rows.append({
                            "Archivo":    _d["filename"],
                            "Tipo":       _cfg.get("label","—")[:30],
                            "Proveedor":  _pname,
                            "Ref.":       (f"{st.session_state.carpeta_id} / {_d['ref']}"
                                          if _d.get("ref") else st.session_state.carpeta_id),
                            "Fecha":      _d.get("fecha") or "—",
                            "Moneda":     _d.get("moneda","ARS"),
                            "TC ARS/USD": _tc_prev,
                        })
                    st.dataframe(pd.DataFrame(_prev_rows), use_container_width=True, hide_index=True)
                    st.caption("Revisá los datos antes de confirmar. Podés modificar cualquier campo arriba.")

                if _create_btn.button(f"⬆️ Confirmar y crear {len(classified_docs)} registro(s) en Odoo",
                             type="primary", key="btn_create_all_imp", use_container_width=True):
                    st.session_state["_imp_preview_open"] = False
                    _prog = st.progress(0)
                    _ok, _errs = 0, []
                    _carp = st.session_state.carpeta_id
                    for _i, _doc in enumerate(classified_docs):
                        try:
                            _full_ref = f"{_carp} / {_doc['ref']}" if _doc["ref"] else _carp
                            _cur_id   = None
                            if _doc.get("moneda", "ARS") == "USD":
                                _cur_id = get_currency_id(models_url, uid, api_key, "USD")
                            _move_id = create_vendor_bill(models, uid, api_key,
                                partner_id   = _doc["tipo_cfg"]["partner_id"],
                                ref          = _full_ref,
                                invoice_date = _doc["fecha"] or False,
                                filename     = _doc["filename"],
                                file_bytes   = _doc["file_bytes"],
                                mimetype     = _doc["mimetype"],
                                journal_id   = _doc["tipo_cfg"]["journal_id"],
                                doc_type_id  = _doc["tipo_cfg"]["doc_type"],
                                currency_id  = _cur_id,
                            )
                            _url = odoo_url("account.move", _move_id)
                            st.success(f"✅ {_doc['filename']} → ID {_move_id} — [Ver en Odoo]({_url})")
                            _tipo = _doc["tipo_cfg"]["tipo"]
                            if _tipo == "petdur":    st.session_state.etapas["1"]  = True
                            elif _tipo == "di_afip": st.session_state.etapas["2"]  = True
                            elif _tipo == "nac":     st.session_state.etapas["2a"] = True
                            if _move_id not in st.session_state.carpeta_bills:
                                st.session_state.carpeta_bills.append(_move_id)
                            st.session_state.history.append({
                                "tipo":   f"Importación {_carp}",
                                "archivo":_doc["filename"], "id":_move_id,
                                "url":_url, "estado":"✅"
                            })
                            _ok += 1
                        except Exception as _e:
                            _errs.append(f"❌ {_doc['filename']}: {str(_e)[:120]}")
                        _prog.progress((_i + 1) / len(classified_docs))
                    if _ok:
                        st.success(f"✅ {_ok} registro(s) creados para {_carp}.")
                        load_carpeta_full.clear()
                    for _err in _errs:
                        st.error(_err)
                    st.rerun()

        st.divider()

        st.divider()

        # ── Resumen de carpeta ────────────────────────────────────
        if carp_data:
            if carp_data.get("error"):
                st.error(f"Error al cargar desde Odoo: {carp_data['error']}")
            po       = carp_data.get("po")
            bills    = carp_data.get("bills", [])
            pickings = carp_data.get("pickings", [])
            tc_oc    = carp_data.get("tc_oc")
            if po:
                pname    = po["name"]
                ppartner = po.get("partner_id", [0, "—"])[1]
                ptotal   = po.get("amount_total", 0)
                pcur     = po.get("currency_id", [0, "USD"])[1] if po.get("currency_id") else "USD"
                po_url   = odoo_url("purchase.order", po['id'])
                ci1, ci2, ci3 = st.columns([3, 2, 1])
                ci1.success(f"📦 **{st.session_state.carpeta_id}** — OC {pname} · {ppartner}")
                ci2.info(f"💵 {pcur} {ptotal:,.2f}" + (f" · TC: **$ {tc_oc:,.0f}**" if tc_oc else ""))
                ci3.markdown(f"[🔗 Ver OC]({po_url})")
                done_picks = [p for p in pickings if p.get("state") == "done"]
                if pickings:
                    st.caption(f"📥 {len(pickings)} picking(s) · {len(done_picks)} recibido(s) en depósito")
            elif bills:
                st.warning(f"⚠️ {len(bills)} comprobante(s) encontrados pero sin OC vinculada.")
            else:
                st.info(f"**{st.session_state.carpeta_id}** no encontrada en Odoo. "
                        f"Subí los documentos para crear los registros.")

        # ── Progreso de etapas ────────────────────────────────────
        st.markdown("#### 📋 Estado de la carpeta")
        cols_et = st.columns(len(ETAPAS_DEF))
        for i, (key, label, desc) in enumerate(ETAPAS_DEF):
            done = st.session_state.etapas.get(key, False)
            icon = "✅" if done else "⏳"
            cols_et[i].markdown(f"**{icon}**  \n<small>{label}</small>",
                                unsafe_allow_html=True, help=desc)
            if not done and st.session_state.carpeta_id:
                if cols_et[i].button("✓", key=f"et_{key}", help=f"Marcar {label}"):
                    st.session_state.etapas[key] = True
                    st.rerun()
        completadas = sum(1 for v in st.session_state.etapas.values() if v)
        st.progress(completadas / len(ETAPAS_DEF),
                    text=f"{completadas}/{len(ETAPAS_DEF)} etapas completadas")

        st.divider()

        # ── Comprobantes cargados en Odoo ─────────────────────────
        if carp_data and carp_data.get("bills"):
            st.markdown("#### 📄 Comprobantes en Odoo")
            bill_rows = []
            for b in carp_data["bills"]:
                pid      = b["partner_id"][0] if b.get("partner_id") else 0
                tipo_inf = PARTNER_TO_TIPO.get(pid,
                    {"etapa": "—", "label": b["partner_id"][1] if b.get("partner_id") else "Otro"})
                cur_name = b["currency_id"][1] if b.get("currency_id") else "ARS"

                tc_disp = "—"
                if cur_name == "USD":
                    icr = b.get("invoice_currency_rate")
                    if icr and icr is not False:
                        tc_val = _parse_odoo_rate({"rate": icr, "inverse_company_rate": None})
                        if tc_val:
                            tc_disp = f"$ {tc_val:,.0f}"
                    if tc_disp == "—" and carp_data.get("tc_oc"):
                        tc_disp = f"$ {carp_data['tc_oc']:,.0f} (OC)"

                amt_orig = (f"USD {b.get('amount_total', 0):,.2f}"
                            if cur_name == "USD" else fmt_ars(b.get("amount_total", 0)))
                amt_ars = abs(float(b.get("amount_total_signed") or b.get("amount_total") or 0))
                bill_url = odoo_url("account.move", b['id'])
                estado_map = {"draft": "Borrador", "posted": "Sin pagar", "cancel": "Cancelado"}

                bill_rows.append({
                    "Etapa":       tipo_inf["etapa"],
                    "Proveedor":   tipo_inf["label"],
                    "Comprobante": b.get("name") or f"ID {b['id']}",
                    "Fecha":       b.get("invoice_date") or "—",
                    "Moneda":      cur_name,
                    "TC ARS/USD":  tc_disp,
                    "Monto orig.": amt_orig,
                    "ARS equiv.":  fmt_ars(amt_ars) if cur_name == "USD" else "—",
                    "Estado":      estado_map.get(b.get("state", ""), b.get("state", "")),
                    "_url":        bill_url,
                })

            df_bills = pd.DataFrame([{k: v for k, v in r.items() if k != "_url"}
                                     for r in bill_rows])
            st.dataframe(df_bills, use_container_width=True, hide_index=True)
            links = "  ·  ".join(f"[{r['Proveedor']}]({r['_url']})" for r in bill_rows[:8])
            st.caption(f"Ver en Odoo: {links}")

            st.divider()

        # ── Desglose de costos por producto ───────────────────────
        _po_cost    = st.session_state.get("carpeta_po") or (carp_data.get("po") if carp_data else None)
        _bills_cost = carp_data.get("bills", []) if carp_data else []
        _tc_oc      = carp_data.get("tc_oc")       if carp_data else None

        if _po_cost and _bills_cost:
            st.markdown("#### 💰 Desglose de costos por producto")

            # Determinar TC: OC primero, luego Odoo currency rate
            _tc_src = None
            if not _tc_oc:
                _petdur_b = next((b for b in _bills_cost
                                  if b.get("partner_id") and b["partner_id"][0] == 49328), None)
                _ref_date = ((_petdur_b.get("invoice_date") if _petdur_b else None)
                             or pd.Timestamp.today().strftime("%Y-%m-%d"))
                _tc_oc, _tc_dt = get_usd_rate_odoo(models_url, uid, api_key, _ref_date)
                _tc_src = f"Odoo ({_tc_dt})" if _tc_oc else None
            else:
                _tc_src = f"OC {_po_cost.get('name', '')} (campo Cotización dólar)"

            if not _tc_oc:
                st.warning("⚠️ No se encontró TC USD/ARS. Verificá el campo 'Cotización dólar' en "
                           "la OC o que esté cargado en Odoo (Contabilidad → Divisas → USD).")
            else:
                with st.spinner("Calculando costos..."):
                    _po_lns    = get_po_lines(models_url, uid, api_key, _po_cost["id"])
                    _cost_rows, _summary = _calc_cost_breakdown(_po_lns, _bills_cost, _tc_oc)

                if _cost_rows and _summary:
                    st.caption(
                        f"TC USD/ARS: **$ {_summary['tc_usd']:,.2f}** — Fuente: {_tc_src}  ·  "
                        f"Coef. landeo total: **+{_summary['coef_landeo_total']:.1f}%**")
                    c1, c2, c3, c4 = st.columns(4)
                    c1.metric("FOB total (u$s)",    fmt_usd(_summary["total_fob_usd"]))
                    c2.metric("Landeo total (u$s)",  fmt_usd(_summary["total_landeo_usd"]))
                    c3.metric("Total (u$s)",         fmt_usd(_summary["grand_total_usd"]))
                    c4.metric("Total (ARS)",         fmt_ars(_summary["grand_total_ars"]))

                    with st.expander("📊 Detalle gastos de nacionalización (ARS)", expanded=False):
                        _tot_nac = _summary["total_landeo_ars"]
                        for _lbl, _amt in _summary.get("nac_detail", {}).items():
                            _pct = _amt / _tot_nac * 100 if _tot_nac > 0 else 0
                            st.caption(f"• **{_lbl}**: {fmt_ars(_amt)}  ({_pct:.1f}%)")
                        st.caption(
                            f"Bills nac encontradas: {_summary['_nac_bils_count']}  ·  "
                            f"AFIP: {'✅' if _summary['_afip_found'] else '—'}  ·  "
                            f"PETDUR: {'✅' if _summary['_petdur_found'] else '—'}")

                    st.dataframe(pd.DataFrame(_cost_rows), use_container_width=True, hide_index=True)

                    # ── Asientos que va a generar el Landed Cost ──────────
                    with st.expander("📒 Asientos estimados del Landed Cost", expanded=False):
                        st.caption(
                            "Basado en `split_method: by_current_cost_price`. "
                            "Los montos son estimados — el LC de Odoo los calcula al validar.")
                        _tc_ae   = _summary["tc_usd"]
                        _nac_ars = _summary["total_landeo_ars"]
                        _nac_usd = _summary["total_landeo_usd"]

                        # Asiento 1: bills nac ya registradas (DR Cuenta Puente / CR Proveedor)
                        st.markdown("**Asiento 1 — Bills de nacionalización (ya creadas)**")
                        _ae1 = []
                        for _lbl, _amt in _summary.get("nac_detail", {}).items():
                            _ae1.append({"Cuenta (DR)": "3284 · Cuenta Puente Recepciones",
                                         "Cuenta (CR)": f"Proveedores · {_lbl}",
                                         "Monto (ARS)": fmt_ars(_amt)})
                        if _ae1:
                            st.dataframe(pd.DataFrame(_ae1), use_container_width=True, hide_index=True)
                        else:
                            st.caption("Sin bills de nac cargadas aún.")

                        st.markdown("**Asiento 2 — Landed Cost (al validar)**")
                        st.caption("DR: Valuación de inventario por producto · CR: 3284 Cuenta Puente Recepciones")
                        _ae2 = []
                        for _row in _cost_rows:
                            # Reconstruct amounts from the formatted strings is hard; recalculate
                            pass
                        # Recalculate LC distribution from po_lines
                        _ae2 = []
                        for _ln in _po_lns:
                            _prod_name = _ln["product_id"][1] if _ln.get("product_id") else _ln.get("name","?")
                            _qty       = float(_ln.get("product_qty") or 1)
                            _fob_line  = float(_ln.get("price_subtotal") or 0)
                            _prop      = _fob_line / _summary["total_fob_usd"] if _summary["total_fob_usd"] > 0 else 0
                            _lc_line_ars = _nac_ars * _prop
                            _lc_unit_usd = (_nac_usd * _prop / _qty) if _qty > 0 else 0
