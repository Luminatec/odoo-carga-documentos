"""
Carga de documentos → Odoo
Luminatec / GPowerByte
v4 — Refactorizado en módulos (config, odoo_client, parsers, tabs/)
"""

import streamlit as st
import os

# ── Importar módulos propios ────────────────────────────────────────────────
import config as _cfg
from odoo_client import get_models_proxy, odoo_authenticate
from tabs import facturas, pedidos, contactos, recibos, chat, historial

# ── Page config (debe ir antes de cualquier otro st.*) ──────────────────────
st.set_page_config(
    page_title="Luminatec · Odoo",
    page_icon="🛒",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── CSS / Branding ──────────────────────────────────────────────────────────
st.markdown("""
<style>
  /* ── Variables de marca Luminatec ─────────────────────────── */
  :root {
    --lumi-red:    #FD0029;
    --lumi-red-dk: #C21F34;
    --lumi-gold:   #F2C800;
    --lumi-dark:   #111111;
    --lumi-gray:   #F7F7F7;
    --lumi-border: #E0E0E0;
  }

  /* ── Sidebar ──────────────────────────────────────────────── */
  [data-testid="stSidebar"] {
    background: var(--lumi-dark) !important;
    border-right: 3px solid var(--lumi-red) !important;
  }
  [data-testid="stSidebar"] label,
  [data-testid="stSidebar"] .stMarkdown,
  [data-testid="stSidebar"] p,
  [data-testid="stSidebar"] small,
  [data-testid="stSidebar"] span,
  [data-testid="stSidebar"] .stCaption { color: #dddddd !important; }
  [data-testid="stSidebar"] h1,
  [data-testid="stSidebar"] h2,
  [data-testid="stSidebar"] h3 { color: #ffffff !important; }
  [data-testid="stSidebar"] .stTextInput input {
    background: #1e1e1e !important; color: #eee !important;
    border-color: #444 !important; border-radius: 6px !important;
  }
  [data-testid="stSidebar"] .stTextInput input:focus {
    border-color: var(--lumi-red) !important;
    box-shadow: 0 0 0 2px rgba(253,0,41,0.25) !important;
  }
  [data-testid="stSidebar"] .stButton button,
  [data-testid="stSidebar"] .stFormSubmitButton button {
    background: var(--lumi-red) !important; color: white !important;
    border: none !important; font-weight: 700 !important;
    border-radius: 6px !important; letter-spacing: 0.3px;
    transition: background 0.15s ease !important;
  }
  [data-testid="stSidebar"] .stButton button:hover,
  [data-testid="stSidebar"] .stFormSubmitButton button:hover {
    background: var(--lumi-red-dk) !important;
  }
  [data-testid="stSidebar"] hr { border-color: #333 !important; }

  /* ── Logo / branding sidebar ──────────────────────────────── */
  .lumi-sidebar-logo {
    display: flex; align-items: center; gap: 10px;
    padding: 8px 0 14px 0;
  }
  .lumi-logo-text {
    font-size: 1.6rem; font-weight: 900;
    color: var(--lumi-red) !important; letter-spacing: -1px;
  }
  .lumi-logo-dot { color: var(--lumi-gold) !important; font-size: 1.8rem; }

  /* ── Título principal ─────────────────────────────────────── */
  .main-title {
    font-size: 1.85rem; font-weight: 900;
    color: var(--lumi-red); letter-spacing: -1px;
    border-bottom: 3px solid var(--lumi-gold);
    padding-bottom: 6px; margin-bottom: 4px;
  }
  .main-title span { color: var(--lumi-gold); }

  /* ── Tabs ─────────────────────────────────────────────────── */
  .stTabs [data-baseweb="tab-list"] {
    gap: 4px;
    border-bottom: 2px solid var(--lumi-border) !important;
  }
  .stTabs [data-baseweb="tab"] {
    font-weight: 600; font-size: 0.88rem;
    border-radius: 6px 6px 0 0 !important;
    padding: 8px 16px !important;
    color: #555 !important;
  }
  .stTabs [aria-selected="true"] {
    background: var(--lumi-red) !important;
    color: white !important;
    border-bottom: 2px solid var(--lumi-red) !important;
  }

  /* ── File uploader ────────────────────────────────────────── */
  [data-testid="stFileUploader"] {
    border: 2px dashed rgba(253,0,41,0.3) !important;
    border-radius: 10px !important;
    padding: 8px !important;
    background: rgba(253,0,41,0.02) !important;
  }
  [data-testid="stFileUploader"]:hover {
    border-color: var(--lumi-red) !important;
  }

  /* ── Chips / badges ───────────────────────────────────────── */
  .admin-badge {
    display: inline-block;
    background: var(--lumi-gold); color: #111;
    font-size: 0.68rem; font-weight: 800;
    padding: 3px 10px; border-radius: 10px;
    text-transform: uppercase; letter-spacing: 0.6px;
  }
  .user-chip {
    display: inline-block;
    background: #1e1e1e; color: #eee;
    font-size: 0.78rem; padding: 4px 12px;
    border-radius: 20px; border: 1px solid #444;
    margin-bottom: 4px;
  }

  /* ── Métricas ─────────────────────────────────────────────── */
  [data-testid="stMetric"] {
    background: var(--lumi-gray);
    border-radius: 8px; padding: 10px 14px;
    border-left: 4px solid var(--lumi-red);
  }
  [data-testid="stMetricValue"] { color: var(--lumi-red) !important; font-weight: 800 !important; }

  /* ── Botones primarios (fuera del sidebar) ────────────────── */
  .stButton > button[kind="primary"],
  .stFormSubmitButton > button {
    background: var(--lumi-red) !important;
    color: white !important; font-weight: 700 !important;
    border: none !important; border-radius: 6px !important;
  }
  .stButton > button[kind="primary"]:hover,
  .stFormSubmitButton > button:hover {
    background: var(--lumi-red-dk) !important;
  }

  /* ── Expanders ────────────────────────────────────────────── */
  [data-testid="stExpander"] summary {
    font-weight: 600; color: var(--lumi-red);
  }
</style>
""", unsafe_allow_html=True)


# ── ADMIN_EMAILS (usa st.secrets — no puede estar en config.py) ────────────
_raw_admin = st.secrets.get("ADMIN_EMAILS", "")
ADMIN_EMAILS = _cfg.BASE_ADMIN_EMAILS | {
    e.strip().lower() for e in _raw_admin.split(",") if e.strip()
}

# ── SESSION STATE defaults ──────────────────────────────────────────────────
_DEFAULTS = {
    "logged_in":     False,
    "user_email":    "",
    "odoo_uid":      None,
    "odoo_password": "",
    "history":       [],
    "carpeta_id":    "",
    "carpeta_po":    None,
    "carpeta_bills": [],
    "carpeta_lc_id": None,
    "etapas":          {k: False for k, *_ in _cfg.ETAPAS_DEF},
    "processed_files": {},   # hash -> {filename, tipo, resultado, hora}
    "error_log":       [],   # entries from show_odoo_error / show_odoo_warning
}
for _k, _v in _DEFAULTS.items():
    if _k not in st.session_state:
        st.session_state[_k] = _v

if "odoo_env" not in st.session_state:
    st.session_state["odoo_env"] = "prod"

# ── Activar entorno correcto en config (se ejecuta en cada rerun) ──────────
if st.session_state["odoo_env"] == "test":
    _cfg.ODOO_URL = _cfg.TEST_ODOO_URL
    _cfg.ODOO_DB  = _cfg.TEST_ODOO_DB
else:
    _cfg.ODOO_URL = _cfg.PROD_ODOO_URL
    _cfg.ODOO_DB  = _cfg.PROD_ODOO_DB


# ═══════════════════════════════════════════════════
# SIDEBAR
# ═══════════════════════════════════════════════════
with st.sidebar:
    if os.path.exists("logo.png"):
        st.image("logo.png", width=180)
    else:
        st.markdown("""<div class="lumi-sidebar-logo">
  <span class="lumi-logo-dot">🛒</span>
  <span class="lumi-logo-text">LUMINATEC</span>
</div>""", unsafe_allow_html=True)
    st.markdown("---")

    # ── Selector de entorno (solo dev) ──────────────────────────────────────
    _dev_email     = "ivarela@luminatec.com"
    _current_email = st.session_state.get("user_email", "")
    _show_env_toggle = (
        not st.session_state.get("logged_in")
        or _current_email == _dev_email
    )
    if _show_env_toggle:
        _is_test = st.toggle(
            "🧪 Modo testing",
            value=(st.session_state.get("odoo_env") == "test"),
            help="Entorno de prueba — solo disponible para el administrador",
        )
        if _is_test and st.session_state.get("odoo_env") != "test":
            st.session_state["odoo_env"] = "test"
            st.session_state.logged_in    = False
            st.session_state.odoo_uid     = None
            st.session_state.odoo_password = ""
            st.rerun()
        elif not _is_test and st.session_state.get("odoo_env") != "prod":
            st.session_state["odoo_env"] = "prod"
            st.session_state.logged_in    = False
            st.session_state.odoo_uid     = None
            st.session_state.odoo_password = ""
            st.rerun()
        if st.session_state.get("odoo_env") == "test":
            st.warning("🧪 **Entorno de TESTING**")
    else:
        if st.session_state.get("odoo_env") == "test":
            st.session_state["odoo_env"] = "prod"
            st.session_state.logged_in    = False
            st.session_state.odoo_uid     = None
            st.session_state.odoo_password = ""
            st.rerun()
    st.markdown("---")

    if not st.session_state.logged_in:
        st.markdown("### 🔐 Iniciar sesión")
        st.caption(f"`{_cfg.ODOO_URL}`")
        with st.form("login_form"):
            email_in  = st.text_input("Email", placeholder="tu@luminatec.com")
            pass_in   = st.text_input("Contraseña", type="password", placeholder="••••••••")
            login_btn = st.form_submit_button("Ingresar", use_container_width=True)
        if login_btn:
            if email_in and pass_in:
                with st.spinner("Verificando en Odoo..."):
                    _uid, _err = odoo_authenticate(email_in, pass_in)
                if _uid:
                    st.session_state.logged_in     = True
                    st.session_state.user_email    = email_in.strip().lower()
                    st.session_state.odoo_uid      = _uid
                    st.session_state.odoo_password = pass_in
                    st.rerun()
                else:
                    st.error(_err or "Email o contraseña incorrectos.")
            else:
                st.warning("Completá email y contraseña.")
    else:
        is_admin = st.session_state.user_email in ADMIN_EMAILS
        st.success("✅ Sesión activa")
        st.markdown(
            '<span class="user-chip">👤 ' + st.session_state.user_email + '</span>',
            unsafe_allow_html=True,
        )
        if is_admin:
            st.markdown('<span class="admin-badge">🛳️ Importaciones habilitado</span>',
                        unsafe_allow_html=True)
            st.caption(f"Carpeta: **{st.session_state.carpeta_id or 'sin selección'}**")
        if st.button("🔓 Cerrar sesión", use_container_width=True):
            st.session_state.logged_in     = False
            st.session_state.user_email    = ""
            st.session_state.odoo_uid      = None
            st.session_state.odoo_password = ""
            st.session_state.history       = []
            st.session_state.carpeta_id    = ""
            st.session_state.carpeta_po    = None
            st.session_state.carpeta_bills = []
            st.session_state.carpeta_lc_id = None
            st.session_state.etapas        = {k: False for k, *_ in _cfg.ETAPAS_DEF}
            st.rerun()

    st.divider()
    _env_label = "🟢 Producción" if st.session_state.get("odoo_env", "prod") == "prod" else "🧪 Testing"
    st.caption(f"{_env_label} · `{_cfg.ODOO_DB}`")


# ═══════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════
st.markdown('<h1 class="main-title">🛒 <span>LUMINA</span>TEC · Carga Odoo</h1>',
            unsafe_allow_html=True)
st.caption("Facturas de proveedores, pedidos de clientes e importaciones — todo en un lugar.")

if not st.session_state.logged_in:
    st.info("👈 Iniciá sesión desde el panel lateral para empezar.")
    st.stop()

# ── Keepalive ────────────────────────────────────────────────────────────────
import streamlit.components.v1 as _stc
_stc.html("""
<script>
(function() {
    function ping() {
        fetch('/_stcore/health', {method: 'GET', cache: 'no-store'})
            .catch(function() {
                fetch(window.location.origin + '/', {method: 'GET',
                    mode: 'no-cors', cache: 'no-store'}).catch(function(){});
            });
    }
    ping();
    setInterval(ping, 120000);
})();
</script>
""", height=0, scrolling=False)

# ── Credenciales de sesión ───────────────────────────────────────────────────
uid        = st.session_state.odoo_uid
api_key    = st.session_state.odoo_password
models     = get_models_proxy()
models_url = f"{_cfg.ODOO_URL}/xmlrpc/2/object"
is_admin   = st.session_state.user_email in ADMIN_EMAILS

if not uid or not api_key:
    st.error("⚠️ Sesión inválida. Por favor cerrá sesión y volvé a ingresar.")
    st.stop()


# ── Preferencias del usuario (sidebar) ─────────────────────────────────────
from user_prefs import load_prefs, save_prefs
from odoo_client import get_payment_journals as _get_pj
from odoo_client import get_purchase_journals as _get_pfj
from odoo_client import get_all_payment_terms as _get_pts
from odoo_client import get_referidos as _get_refs

with st.sidebar:
    st.divider()
    if st.button("🔄 Refrescar datos de Odoo", key="sidebar_refresh_cache",
                 help="Limpia el caché y recarga productos, cuentas y datos de Odoo"):
        st.cache_data.clear()
        st.rerun()
    with st.expander("⚙️ Preferencias", expanded=False):
        _prefs    = load_prefs()
        _pj_raw   = _get_pj(models_url, uid, api_key)
        _pfj_raw  = _get_pfj(models_url, uid, api_key)
        _pfj_opts = ["— Sin preferencia —"] + [n for _, n in _pfj_raw]
        _pfj_cur  = _prefs.get("diario_facturas_nombre", "")
        _pfj_def  = _pfj_opts.index(_pfj_cur) if _pfj_cur in _pfj_opts else 0
        _ref_raw  = _get_refs(models_url, uid, api_key)
        _pt_raw   = _get_pts(models_url, uid, api_key)

        _pj_opts  = ["— Sin preferencia —"] + [n for _, n, *_ in _pj_raw]
        _ref_opts = ["— Sin preferencia —"] + [n for _, n in _ref_raw]
        _pt_opts  = ["— Sin preferencia —"] + [n for _, n in _pt_raw]

        _pj_cur   = _prefs.get("diario_cobros_nombre", "")
        _ref_cur  = _prefs.get("referido_nombre", "")
        _pt_cur   = _prefs.get("plazo_pago_nombre", "")

        _pj_def   = _pj_opts.index(_pj_cur)   if _pj_cur  in _pj_opts  else 0
        _ref_def  = _ref_opts.index(_ref_cur)  if _ref_cur in _ref_opts else 0
        _pt_def   = _pt_opts.index(_pt_cur)    if _pt_cur  in _pt_opts  else 0

        with st.form("sidebar_prefs_form"):
            st.caption("Valores pre-seleccionados en cada tab.")
            _pj_sel  = st.selectbox("Diario de cobros", _pj_opts,
                index=_pj_def, key="sb_pj",
                help="Diario usado en Recibos de Cobro")
            _pfj_sel = st.selectbox("Diario de facturas prov.", _pfj_opts,
                index=_pfj_def, key="sb_pfj",
                help="Configura una vez — aplica para todos los usuarios")
            _ref_sel = st.selectbox("Ejecutivo / Referido", _ref_opts,
                index=_ref_def, key="sb_ref",
                help="Pre-seleccionado en Pedidos y Contactos")
            _pt_sel  = st.selectbox("Plazo de pago", _pt_opts,
                index=_pt_def, key="sb_pt",
                help="Plazo pre-seleccionado en Pedidos")
            if st.form_submit_button("💾 Guardar", use_container_width=True):
                _pfj_save_id = next((jid for jid, jn in _pfj_raw if jn == _pfj_sel), 0)
                save_prefs({
                    "diario_cobros_nombre":  "" if _pj_sel  == "— Sin preferencia —" else _pj_sel,
                    "diario_facturas_nombre": "" if _pfj_sel == "— Sin preferencia —" else _pfj_sel,
                    "diario_facturas_id":    _pfj_save_id,
                    "referido_nombre":       "" if _ref_sel == "— Sin preferencia —" else _ref_sel,
                    "plazo_pago_nombre":     "" if _pt_sel  == "— Sin preferencia —" else _pt_sel,
                })
                st.toast("Preferencias guardadas", icon="⚙️")


# ═══════════════════════════════════════════════════
# TABS
# ═══════════════════════════════════════════════════
(tab_bills, tab_orders, tab_recibos, tab_contacts,
 tab_chat, tab_history) = st.tabs([
    "🧾 Facturas prov.",
    "📦 Pedidos",
    "💰 Recibos de Cobro",
    "👥 Contactos",
    "🤖 Asistente",
    "📋 Historial",
])

with tab_bills:
    facturas.render(models, uid, api_key, models_url, is_admin)

with tab_orders:
    pedidos.render(models, uid, api_key, models_url, is_admin)

with tab_recibos:
    recibos.render(models, uid, api_key, models_url, is_admin)

with tab_contacts:
    contactos.render(models, uid, api_key, models_url, is_admin)

with tab_chat:
    chat.render(models, uid, api_key, models_url, is_admin)

with tab_history:
    historial.render(models, uid, api_key, models_url, is_admin)
