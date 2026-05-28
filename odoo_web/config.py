"""
Luminatec · Odoo — Configuración y constantes
Importar con: import config as _cfg
NO uses 'from config import ODOO_DB' — el valor se muta en app.py en cada rerun.
"""

# ── URLs y bases de datos ───────────────────────────────────────────────────
PROD_ODOO_URL = "https://gpowerbyte-luminatec.odoo.com"
PROD_ODOO_DB  = "gpowerbyte-luminatec-master-22753148"
TEST_ODOO_URL = "https://gpowerbyte-luminatec-test-31645353.dev.odoo.com"
TEST_ODOO_DB  = "gpowerbyte-luminatec-test-31645353"

# Valores activos — app.py los muta al inicio de cada rerun según el entorno
ODOO_URL = PROD_ODOO_URL
ODOO_DB  = PROD_ODOO_DB

# ── Usuarios admin ─────────────────────────────────────────────────────────
# ADMIN_EMAILS se construye en app.py (requiere st.secrets).
BASE_ADMIN_EMAILS = {"ivarela@luminatec.com", "dario@luminatec.com", "dalonso@luminatec.com"}

# ── MIME types ──────────────────────────────────────────────────────────────
MIMETYPES = {
    "pdf":  "application/pdf",
    "jpg":  "image/jpeg",
    "jpeg": "image/jpeg",
    "png":  "image/png",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "xls":  "application/vnd.ms-excel",
}

# ── Productos de Landed Cost ────────────────────────────────────────────────
LC_PRODUCTS = {
    20241: "CMV - Agente de carga",
    20242: "CMV - Honorarios despachante",
    20243: "CMV - Otros",
    20244: "CMV - Terminal portuaria",
    20245: "CMV - Tasas y Derechos",
    20276: "Despacho Cta Transitoria 21%",
    20277: "Despacho Cta Transitoria 10,5%",
}

# ── Etapas de importación ───────────────────────────────────────────────────
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
