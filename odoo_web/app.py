"""
Carga de documentos → Odoo
Luminatec / GPowerByte
"""

import streamlit as st
import xmlrpc.client
import base64
import re
from io import BytesIO

import pandas as pd

# ─── Page config ──────────────────────────────────────────────
st.set_page_config(
    page_title="Carga Odoo · Luminatec",
    page_icon="📄",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown("""
<style>
    [data-testid="stFileUploader"] { border: 2px dashed #ccc; border-radius: 10px; padding: 8px; }
</style>
""", unsafe_allow_html=True)

# ─── Configuración fija ───────────────────────────────────────
ODOO_URL = "https://gpowerbyte-luminatec.odoo.com"
ODOO_DB  = "gpowerbyte-luminatec"

MIMETYPES = {
    "pdf":  "application/pdf",
    "jpg":  "image/jpeg",
    "jpeg": "image/jpeg",
    "png":  "image/png",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "xls":  "application/vnd.ms-excel",
}

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


def attach_file(models, uid, api_key, res_model, res_id, filename, file_bytes, mimetype):
    call(models, uid, api_key, "ir.attachment", "create", [{
        "name": filename,
        "res_model": res_model,
        "res_id": res_id,
        "datas": base64.b64encode(file_bytes).decode(),
        "mimetype": mimetype,
    }])


def create_vendor_bill(models, uid, api_key, partner_id, ref, invoice_date, filename, file_bytes, mimetype):
    vals = {"move_type": "in_invoice"}
    if partner_id:
        vals["partner_id"] = partner_id
    if ref:
        vals["ref"] = ref
    if invoice_date:
        vals["invoice_date"] = invoice_date
    move_id = call(models, uid, api_key, "account.move", "create", [vals])
    attach_file(models, uid, api_key, "account.move", move_id, filename, file_bytes, mimetype)
    return move_id


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


# ─── Session state ────────────────────────────────────────────
for k, v in [("uid", None), ("models", None), ("api_key", ""), ("email", ""), ("history", [])]:
    if k not in st.session_state:
        st.session_state[k] = v


# ═══════════════════════════════════════════════════
# SIDEBAR
# ═══════════════════════════════════════════════════
with st.sidebar:
    st.markdown("### ⚙️ Conexión a Odoo")
    st.caption(f"`{ODOO_URL}`")
    st.caption(f"Base de datos: `{ODOO_DB}`")
    st.divider()

    email   = st.text_input("Email", value=st.session_state.email,   placeholder="tu@empresa.com")
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
                st.success(f"✅ Conectado")
            except Exception as e:
                st.error(str(e))
        else:
            st.warning("Completá email y API key.")

    if st.session_state.uid:
        st.success("✅ Sesión activa")
        if st.button("🔓 Desconectar", use_container_width=True):
            st.session_state.uid    = None
            st.session_state.models = None
            st.rerun()

    st.divider()
    with st.expander("¿Cómo genero la API Key?"):
        st.markdown("""
1. Abrí Odoo y hacé clic en tu **avatar** (arriba a la derecha)
2. Seleccioná **Mi perfil**
3. Pestaña **Seguridad de la cuenta**
4. Sección **Claves API** → **Nueva clave API**
5. Poné un nombre (ej. *Cowork*), copiá la clave y pegala arriba

⚠️ La clave se muestra **una sola vez**.
""")


# ═══════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════
st.title("📄 Carga de documentos → Odoo")
st.caption("Facturas de proveedores y pedidos de clientes desde PDF, imagen o Excel — todo en un lugar.")

if not st.session_state.uid:
    st.info("👈 Conectate a Odoo desde el panel lateral para empezar.")
    st.stop()

uid     = st.session_state.uid
models  = st.session_state.models
api_key = st.session_state.api_key
models_url = f"{ODOO_URL}/xmlrpc/2/object"

tab_bills, tab_orders, tab_history = st.tabs([
    "🧾 Facturas de proveedores",
    "📦 Pedidos de clientes",
    "📋 Historial de sesión",
])


# ───────────────────────────────────────────────────
# Helper: partner selector widget
# ───────────────────────────────────────────────────
def partner_selector(label, default_name, key_prefix):
    """Returns (partner_id or None, partner_name)."""
    name = st.text_input(label, value=default_name[:60] if default_name else "", key=f"{key_prefix}_name")
    partner_id = None
    if name:
        with st.spinner("Buscando en Odoo..."):
            matches = search_partners(models_url, uid, api_key, name)
        if matches:
            options = {m[1]: m[0] for m in matches}
            chosen = st.selectbox(
                "Seleccioná el registro encontrado en Odoo",
                list(options.keys()),
                key=f"{key_prefix}_sel",
            )
            partner_id = options[chosen]
        else:
            st.warning(f"'{name}' no encontrado en Odoo. Se creará la factura sin asociar proveedor.")
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

        # ── Excel: lote de facturas ──────────────────────────────
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
            col_prov  = c1.selectbox("Proveedor",   cols_opts, key=f"bp_{uf.name}")
            col_fecha = c2.selectbox("Fecha",        cols_opts, key=f"bf_{uf.name}")
            col_ref   = c3.selectbox("N° Factura",  cols_opts, key=f"br_{uf.name}")
            col_total = c4.selectbox("Total (info)", cols_opts, key=f"bt_{uf.name}")

            if st.button(f"⬆️ Cargar {len(df)} facturas en Odoo", key=f"load_bills_xls_{uf.name}"):
                bar = st.progress(0)
                ok, errs = 0, []
                for i, row in df.iterrows():
                    try:
                        prov_name = row.get(col_prov, "") if col_prov != "(ninguna)" else ""
                        fecha     = row.get(col_fecha, "") if col_fecha != "(ninguna)" else ""
                        ref       = row.get(col_ref, "")   if col_ref   != "(ninguna)" else ""
                        partner_id = False
                        if prov_name:
                            m = search_partners(models_url, uid, api_key, prov_name, limit=1)
                            partner_id = m[0][0] if m else False
                        move_id = create_vendor_bill(
                            models, uid, api_key,
                            partner_id=partner_id,
                            ref=str(ref),
                            invoice_date=str(fecha) if fecha else False,
                            filename=f"{uf.name}_fila{i+1}.pdf",
                            file_bytes=file_bytes,
                            mimetype=mimetype,
                        )
                        ok += 1
                        odoo_url = f"{ODOO_URL}/web#id={move_id}&model=account.move&view_type=form"
                        st.session_state.history.append({
                            "tipo": "Factura proveedor", "archivo": f"{uf.name} · fila {i+1}",
                            "id": move_id, "url": odoo_url, "estado": "✅",
                        })
                    except Exception as e:
                        errs.append(f"Fila {i+1}: {str(e)[:100]}")
                    bar.progress((i + 1) / len(df))
                if ok:
                    st.success(f"✅ {ok} de {len(df)} facturas creadas en Odoo.")
                for err in errs:
                    st.warning(err)

        # ── PDF / Imagen: una factura ────────────────────────────
        else:
            extracted, raw_text = {}, ""
            if ext == "pdf":
                with st.spinner("Leyendo PDF..."):
                    extracted, raw_text = extract_pdf_fields(file_bytes)
                if extracted.get("proveedor"):
                    st.caption("🤖 Datos detectados automáticamente — revisá antes de confirmar.")
                else:
                    st.caption("ℹ️ PDF sin texto extraíble. Completá los datos a mano.")
            elif ext in ("jpg", "jpeg", "png"):
                st.image(file_bytes, caption="Vista previa", width=380)
                st.caption("Completá los datos del formulario.")

            with st.form(key=f"bill_form_{uf.name}"):
                c1, c2 = st.columns(2)
                default_prov = extracted.get("proveedor", "")
                prov_input   = c1.text_input("Proveedor",    value=default_prov[:60], placeholder="Nombre exacto en Odoo")
                ref_input    = c2.text_input("N° de factura", value=extracted.get("numero", ""))
                fecha_input  = c1.text_input("Fecha (AAAA-MM-DD)", value="", placeholder="2026-05-12")
                total_input  = c2.text_input("Total (solo referencia)", value=extracted.get("total", ""), disabled=True)
                notas_input  = st.text_area("Notas internas", height=55)
                go = st.form_submit_button("⬆️ Cargar en Odoo", use_container_width=True)

            if go:
                with st.spinner("Procesando..."):
                    try:
                        partner_id = False
                        if prov_input:
                            matches = search_partners(models_url, uid, api_key, prov_input, limit=3)
                            if matches:
                                partner_id = matches[0][0]
                                st.caption(f"Proveedor asignado: **{matches[0][1]}**")
                            else:
                                st.warning(f"'{prov_input}' no encontrado — se creará sin proveedor.")
                        move_id = create_vendor_bill(
                            models, uid, api_key,
                            partner_id=partner_id,
                            ref=ref_input,
                            invoice_date=fecha_input or False,
                            filename=uf.name,
                            file_bytes=file_bytes,
                            mimetype=mimetype,
                        )
                        odoo_url = f"{ODOO_URL}/web#id={move_id}&model=account.move&view_type=form"
                        st.success(f"✅ Factura creada — [Abrir en Odoo →]({odoo_url})")
                        st.session_state.history.append({
                            "tipo": "Factura proveedor", "archivo": uf.name,
                            "id": move_id, "url": odoo_url, "estado": "✅",
                        })
                    except Exception as e:
                        st.error(f"❌ {e}")


# ═══════════════════════════════════════════════════
# TAB 2 — PEDIDOS DE CLIENTES
# ═══════════════════════════════════════════════════
with tab_orders:
    st.subheader("Pedidos de clientes")
    files_o = st.file_uploader(
        "Arrastrá o elegí archivos (PDF, JPG, PNG, XLSX)",
        type=["pdf", "jpg", "jpeg", "png", "xlsx", "xls"],
        accept_multiple_files=True,
        key="orders_upload",
    )

    if not files_o:
        st.caption("Subí uno o más archivos para empezar.")

    for uf in (files_o or []):
        st.divider()
        ext        = uf.name.rsplit(".", 1)[-1].lower()
        file_bytes = uf.read()
        mimetype   = MIMETYPES.get(ext, "application/octet-stream")
        st.markdown(f"**📎 {uf.name}**  `{ext.upper()}`  ({len(file_bytes)//1024} KB)")

        # ── Excel: lote de pedidos ───────────────────────────────
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
            col_cli   = c1.selectbox("Cliente",   cols_opts, key=f"oc_{uf.name}")
            col_prod  = c2.selectbox("Producto",  cols_opts, key=f"op_{uf.name}")
            col_qty   = c3.selectbox("Cantidad",  cols_opts, key=f"oq_{uf.name}")
            col_price = c4.selectbox("Precio",    cols_opts, key=f"opr_{uf.name}")

            if col_cli == "(ninguna)":
                st.warning("Seleccioná al menos la columna de Cliente para poder cargar.")
            elif st.button(f"⬆️ Cargar pedidos en Odoo", key=f"load_orders_xls_{uf.name}"):
                clientes = df[col_cli].unique()
                bar = st.progress(0)
                ok, errs = 0, []
                for i, cliente in enumerate(clientes):
                    try:
                        rows = df[df[col_cli] == cliente]
                        matches = search_partners(models_url, uid, api_key, str(cliente), limit=1)
                        if not matches:
                            errs.append(f"Cliente '{cliente}' no encontrado en Odoo.")
                            continue
                        partner_id = matches[0][0]
                        lines = []
                        for _, row in rows.iterrows():
                            lines.append({
                                "producto": row.get(col_prod, "") if col_prod != "(ninguna)" else "",
                                "cantidad": row.get(col_qty, 1)   if col_qty  != "(ninguna)" else 1,
                                "precio":   row.get(col_price, 0) if col_price != "(ninguna)" else 0,
                            })
                        order_id = create_sale_order(
                            models, uid, api_key,
                            partner_id=partner_id,
                            note=f"Importado desde {uf.name}",
                            lines=lines,
                            filename=uf.name,
                            file_bytes=file_bytes,
                            mimetype=mimetype,
                        )
                        ok += 1
                        odoo_url = f"{ODOO_URL}/web#id={order_id}&model=sale.order&view_type=form"
                        st.success(f"✅ Pedido de **{cliente}** creado — [Abrir en Odoo →]({odoo_url})")
                        st.session_state.history.append({
                            "tipo": "Pedido cliente", "archivo": f"{uf.name} · {cliente}",
                            "id": order_id, "url": odoo_url, "estado": "✅",
                        })
                    except Exception as e:
                        errs.append(f"Cliente '{cliente}': {str(e)[:100]}")
                    bar.progress((i + 1) / len(clientes))
                if ok:
                    st.success(f"✅ {ok} pedidos creados.")
                for err in errs:
                    st.warning(err)

        # ── PDF / Imagen: un pedido ──────────────────────────────
        else:
            extracted, _ = {}, ""
            if ext == "pdf":
                with st.spinner("Leyendo PDF..."):
                    extracted, _ = extract_pdf_fields(file_bytes)
            elif ext in ("jpg", "jpeg", "png"):
                st.image(file_bytes, caption="Vista previa", width=380)

            with st.form(key=f"order_form_{uf.name}"):
                c1, c2 = st.columns(2)
                cli_input = c1.text_input("Cliente", value=extracted.get("proveedor", "")[:60], placeholder="Nombre exacto en Odoo")
                ref_input = c2.text_input("Referencia / N° pedido", value=extracted.get("numero", ""))
                notas_input = st.text_area("Notas", height=55)

                st.caption("Líneas del pedido (opcional — podés completarlas directo en Odoo)")
                lines_text = st.text_area(
                    "Formato: Producto | Cantidad | Precio unitario",
                    height=90,
                    placeholder="Camiseta azul talle M | 10 | 2500\nPantalón negro talle L | 5 | 4000",
                )
                go = st.form_submit_button("⬆️ Crear pedido en Odoo", use_container_width=True)

            if go:
                with st.spinner("Procesando..."):
                    try:
                        matches = search_partners(models_url, uid, api_key, cli_input, limit=3)
                        if not matches:
                            st.error(f"Cliente '{cli_input}' no encontrado en Odoo. Verificá el nombre.")
                            st.stop()
                        partner_id = matches[0][0]
                        st.caption(f"Cliente asignado: **{matches[0][1]}**")

                        lines = []
                        for line in (lines_text or "").strip().split("\n"):
                            if not line.strip():
                                continue
                            parts = [p.strip() for p in line.split("|")]
                            lines.append({
                                "producto": parts[0] if len(parts) > 0 else "",
                                "cantidad": parts[1] if len(parts) > 1 else 1,
                                "precio":   parts[2] if len(parts) > 2 else 0,
                            })

                        order_id = create_sale_order(
                            models, uid, api_key,
                            partner_id=partner_id,
                            note=notas_input,
                            lines=lines,
                            filename=uf.name,
                            file_bytes=file_bytes,
                            mimetype=mimetype,
                        )
                        odoo_url = f"{ODOO_URL}/web#id={order_id}&model=sale.order&view_type=form"
                        st.success(f"✅ Pedido creado — [Abrir en Odoo →]({odoo_url})")
                        st.session_state.history.append({
                            "tipo": "Pedido cliente", "archivo": uf.name,
                            "id": order_id, "url": odoo_url, "estado": "✅",
                        })
                    except Exception as e:
                        st.error(f"❌ {e}")


# ═══════════════════════════════════════════════════
# TAB 3 — HISTORIAL
# ═══════════════════════════════════════════════════
with tab_history:
    st.subheader("Historial de esta sesión")
    history = st.session_state.history
    if history:
        for r in reversed(history):
            c1, c2, c3 = st.columns([2, 3, 1])
            c1.markdown(f"**{r['tipo']}**")
            c2.markdown(f"{r['archivo']} — [Ver en Odoo (ID {r['id']})]({r['url']})")
            c3.markdown(r["estado"])
        st.divider()
        if st.button("🗑️ Limpiar historial"):
            st.session_state.history = []
            st.rerun()
    else:
        st.info("Todavía no se realizaron cargas en esta sesión.")
