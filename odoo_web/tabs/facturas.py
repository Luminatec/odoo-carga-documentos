"""Tab Facturas de Proveedores."""
import streamlit as st
import pandas as pd
import re
from io import BytesIO
from datetime import datetime as _dt_now
import config as _cfg
from odoo_client import (
    check_duplicate_vendor_bill,
    get_purchase_journals,
    search_partners,
    get_all_accounts,
    get_partner_default_account,
    get_expense_products,
    get_partner_default_product,
    get_analytic_accounts,
    create_vendor_bill,
    odoo_url,
    safe_float,
    fmt_ars,
    search_partner_by_cuit,
    check_invoice_exists,
    create_vendor_partner,
    OdooError,
    show_odoo_error,
    validate_cuit,
    validate_email,
    check_duplicate_file,
    register_processed_file,
    _AR_TZ,
    clean_str,
)
from user_prefs import (load_prefs as _load_prefs_fac, save_prefs as _save_prefs_fac,
                         load_vendor_account_pref, save_vendor_account_pref,
                         append_persistent_history)
from parsers import (extract_pdf_fields, parse_ar_date, extract_image_fields,
                     extract_excel_oc_fields, extract_afip_xml_fields,
                     extract_afip_qr_from_pdf_text)


def render(models, uid, api_key, models_url, is_admin):
    st.subheader("Facturas de proveedores")
    files = st.file_uploader("Arrastrá o elegí archivos (PDF, JPG, PNG, XLSX)",
        type=["pdf","jpg","jpeg","png","xlsx","xls"], accept_multiple_files=True, key="bills_upload")
    if not files:
        st.caption("Subí uno o más archivos para empezar.")
    _total_upfiles = len(files) if files else 0

    # ── Vista previa batch (solo con 2+ archivos) ─────────────────────────
    _show_loop = True
    if _total_upfiles > 1:
        _batch_key = f"batchok_f_{'|'.join(f.name + str(f.size) for f in files)}"
        if not st.session_state.get(_batch_key):
            st.markdown(f"#### 📋 Revisá los {_total_upfiles} archivos antes de procesar")
            _bprev = []
            for _pf in files:
                _pe  = _pf.name.rsplit(".", 1)[-1].lower()
                _tip = "Excel" if _pe in ("xlsx","xls") else ("PDF" if _pe == "pdf" else "Imagen")
                _dup = next(
                    (True for v in st.session_state.get("processed_files", {}).values()
                     if v.get("filename") == _pf.name), False)
                _bprev.append({
                    "Archivo":      _pf.name,
                    "Tamaño":       f"{(_pf.size or 0)//1024} KB",
                    "Tipo":         _tip,
                    "¿Ya procesado?": "⚠️ Sí" if _dup else "✅ No",
                })
            import pandas as _bpd
            st.dataframe(_bpd.DataFrame(_bprev), use_container_width=True, hide_index=True)
            if st.button(f"⬆️ Procesar los {_total_upfiles} archivos",
                         type="primary", key="batch_confirm_facturas"):
                st.session_state[_batch_key] = True
                st.rerun()
            _show_loop = False
        else:
            st.caption(f"📂 {_total_upfiles} archivo(s) — procesando uno por uno.")

    if _show_loop:
     for _uf_idx, uf in enumerate(files or []):
        st.divider()
        ext        = uf.name.rsplit(".", 1)[-1].lower()
        # getvalue() siempre retorna bytes completos independientemente del puntero
        try:
            file_bytes = uf.getvalue()
        except Exception:
            uf.seek(0)
            file_bytes = uf.read()
        if not file_bytes:
            _saved_k = f"_saved_bytes_{uf.name}_{uf.size}"
            file_bytes = st.session_state.pop(_saved_k, b"")
        mimetype   = _cfg.MIMETYPES.get(ext, "application/octet-stream")
        _file_lbl = f"({_uf_idx + 1}/{_total_upfiles}) " if _total_upfiles > 1 else ""
        st.markdown(f"**📎 {_file_lbl}{uf.name}**  `{ext.upper()}`  ({len(file_bytes)//1024} KB)")
        # ── Detección de duplicados ───────────────────────────────────
        _is_dup, _dup_entry = check_duplicate_file(file_bytes, uf.name)
        if _is_dup:
            # Verificar que el registro aún existe en Odoo antes de bloquear
            _dup_res = _dup_entry.get("resultado", "")
            _dup_odoo_id = None
            import re as _re_dup
            _m_id = _re_dup.search(r"\bID\s+(\d+)", _dup_res)
            if _m_id:
                _dup_odoo_id = int(_m_id.group(1))
            _odoo_still_exists = False
            if _dup_odoo_id:
                try:
                    _chk = models.execute_kw(_cfg.ODOO_DB, uid, api_key,
                        "account.move", "search_read",
                        [[("id", "=", _dup_odoo_id), ("state", "!=", "cancel")]],
                        {"fields": ["id", "name"], "limit": 1})
                    _odoo_still_exists = bool(_chk)
                except Exception:
                    pass
            if _odoo_still_exists:
                _existing_name = _chk[0]["name"] if _chk else _dup_res
                _existing_url  = odoo_url("account.move", _dup_odoo_id)
                st.error(
                    f"❌ **{uf.name}** ya fue procesado en esta sesión "
                    f"({_dup_entry.get('hora','?')}) y existe en Odoo como "
                    f"**{_existing_name}**. [Ver en Odoo]({_existing_url})")
                continue
            else:
                st.warning(
                    f"⚠️ **{uf.name}** fue procesado antes en esta sesión "
                    f"({_dup_entry.get('hora','?')}) pero el registro fue eliminado de Odoo. "
                    "Podés cargarlo de nuevo.")
        if ext in ("xlsx", "xls"):
            # ── Detección temprana: ¿es un Excel de pedido de cliente? ───────
            _oc_check = extract_excel_oc_fields(file_bytes)
            if _oc_check.get("lineas"):
                st.warning(
                    f"⚠️ **Este Excel parece un pedido de cliente** — se detectaron "
                    f"{len(_oc_check['lineas'])} producto(s) con columna de cantidad/pedido.\n\n"
                    "Este archivo **no debe cargarse como factura de proveedor**. "
                    "Por favor subilo en la pestaña **📦 Pedidos** para crear el pedido en Ventas."
                )
                continue
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
                            invoice_date=parse_ar_date(str(fecha)) if fecha else False,
                            filename=f"{uf.name}_fila{i+1}.pdf",
                            file_bytes=file_bytes, mimetype=mimetype)
                        ok += 1
                        url = odoo_url("account.move", move_id)
                        st.session_state.history.append({"tipo":"Factura proveedor",
                            "archivo":f"{uf.name}·fila{i+1}","id":move_id,"url":url,"estado":"✅","hora":_dt_now.now(_AR_TZ).strftime("%H:%M")})
                    except (OdooError, Exception) as e:
                        errs.append(f"Fila {i+1}: {str(e)[:120]}")
                    bar.progress((i+1)/len(df))
                if ok: st.toast(f"{ok} de {len(df)} facturas creadas en Odoo.", icon="✅")
                for err in errs: st.warning(err)
        else:
            extracted, raw_text = {}, ""
            if ext == "xml":
                with st.spinner(f"Leyendo XML AFIP... {_file_lbl}"):
                    extracted = extract_afip_xml_fields(file_bytes)
                if extracted.get("numero") or extracted.get("cuit"):
                    st.caption("📄 Datos extraídos del XML AFIP — revisá antes de confirmar.")
                else:
                    st.caption("⚠️ No se reconoció el formato XML. Completá los datos a mano.")
            elif ext == "pdf":
                with st.spinner(f"Analizando PDF con IA... {_file_lbl}"):
                    extracted, raw_text = extract_pdf_fields(file_bytes)
                # Enriquecer con datos del QR AFIP si hay campos faltantes
                if raw_text:
                    _qr_data = extract_afip_qr_from_pdf_text(raw_text)
                    if _qr_data:
                        for _qk, _qv in _qr_data.items():
                            if _qk.startswith("_"): continue
                            if not extracted.get(_qk) and _qv:
                                extracted[_qk] = _qv
                        if _qr_data.get("numero") and not extracted.get("numero"):
                            extracted["numero"] = _qr_data["numero"]
                _src_tag = "📄 XML QR" if extracted.get("_from_qr") else (
                    "✨ IA" if extracted.get("_source") == "ai" else "🔣 Regex")
                if extracted.get("proveedor") or extracted.get("numero"):
                    st.caption(f"🤖 Datos detectados [{_src_tag}] — revisá antes de confirmar.")
                else:
                    st.caption("ℹ️ PDF sin texto extraíble. Completá los datos a mano.")
            elif ext in ("jpg","jpeg","png"):
                st.image(file_bytes, caption="Vista previa", width=420)
                with st.spinner(f"Leyendo imagen con OCR... {_file_lbl}"):
                    extracted, raw_text = extract_image_fields(file_bytes)
                st.caption("🤖 Datos detectados por OCR — revisá antes de confirmar." if extracted.get("proveedor")
                           else "ℹ️ OCR no detectó datos. Completá los campos a mano.")

            # Cargar cuentas contables (cacheado)
            _bill_accounts = get_all_accounts(models_url, uid, api_key)
            _acct_labels   = ["— Sin cuenta —"] + [lbl for _, lbl in _bill_accounts]

            # ── CUIT editable en tiempo real (fuera del form) ────────────────────
            _cuit_raw    = extracted.get("cuit", "")
            if _cuit_raw:
                _cuit_mod11_ok, _cuit_mod11_msg = validate_cuit(_cuit_raw)
                if not _cuit_mod11_ok:
                    st.warning(f"⚠️ CUIT extraído **{_cuit_raw}** no pasa el módulo 11: {_cuit_mod11_msg}")
            _cond_venta  = extracted.get("condiciones_venta", "")
            _dias_pago   = extracted.get("dias_pago")
            _vto_auto    = extracted.get("fecha_vencimiento", "")

            _cuit_edit_key = f"bill_cuit_edit_{uf.name}"
            if _cuit_edit_key not in st.session_state:
                st.session_state[_cuit_edit_key] = _cuit_raw

            _cuit_effective = st.text_input(
                "CUIT del proveedor",
                key=_cuit_edit_key,
                placeholder="30-12345678-9",
                help="Detectado automáticamente del PDF. Corregilo si es necesario.",
            )
            _cuit_raw  = _cuit_effective.strip() if _cuit_effective else ""
            _cuit_norm = _cuit_raw.replace("-","").replace(" ","").strip()
            _ss_vendor_key = f"vendor_created_{_cuit_norm}"

            # Buscar primero en session state (recién creado), luego en Odoo
            _partner_preloaded = st.session_state.get(_ss_vendor_key)
            if not _partner_preloaded and _cuit_raw:
                _partner_preloaded = search_partner_by_cuit(models_url, uid, api_key, _cuit_raw)

            # Pre-selección de cuenta contable:
            # Prioridad 1: cache local (última usada para este proveedor)
            # Prioridad 2: cuenta por defecto configurada en Odoo
            _default_acct_idx = 0
            _acct_pref_source  = ""    # "cache", "odoo" o ""
            if _partner_preloaded:
                _vcache = load_vendor_account_pref(_partner_preloaded[0])
                if _vcache.get("account_label") and _vcache["account_label"] in _acct_labels:
                    _default_acct_idx = _acct_labels.index(_vcache["account_label"])
                    _acct_pref_source  = "cache"
                else:
                    _def_acct = get_partner_default_account(models_url, uid, api_key, _partner_preloaded[0])
                    if _def_acct:
                        _def_acct_label = _def_acct[1]
                        for _i, _lbl in enumerate(_acct_labels):
                            if _lbl == _def_acct_label:
                                _default_acct_idx = _i
                                _acct_pref_source  = "odoo"
                                break

            # Cargar productos de gasto y calcular default del proveedor
            _expense_products = get_expense_products(models_url, uid, api_key)
            _prod_labels = ["— Sin producto —"] + [lbl for _, lbl in _expense_products]
            _default_prod_idx = 0
            if _partner_preloaded:
                # Prioridad 1: cache local
                if _vcache.get("product_label") and _vcache["product_label"] in _prod_labels:
                    _default_prod_idx = _prod_labels.index(_vcache["product_label"])
                else:
                    # Prioridad 2: default configurado en Odoo
                    _def_prod = get_partner_default_product(models_url, uid, api_key, _partner_preloaded[0])
                    if _def_prod:
                        _def_prod_label = _def_prod[1]
                        for _pi, _plbl in enumerate(_prod_labels):
                            if _plbl == _def_prod_label:
                                _default_prod_idx = _pi
                                break

            # Cargar cuentas analíticas (Centros de Costo)
            _analytic_accounts = get_analytic_accounts(models_url, uid, api_key)
            _analytic_labels   = ["— Sin centro de costo —"] + [lbl for _, lbl in _analytic_accounts]
            _default_analytic_idx = 0
            if _partner_preloaded and _vcache.get("analytic_label"):
                if _vcache["analytic_label"] in _analytic_labels:
                    _default_analytic_idx = _analytic_labels.index(_vcache["analytic_label"])

            # ── Estado del proveedor + opción de crear nuevo ──────────────────────
            _create_new_vend_key = f"bill_create_new_vend_{uf.name}"
            # Inicializar siempre en False para evitar que quede marcado de sesiones anteriores
            if _create_new_vend_key not in st.session_state:
                st.session_state[_create_new_vend_key] = False

            if _partner_preloaded:
                st.info(f"🏢 Proveedor detectado por CUIT: **{_partner_preloaded[1]}**")
            elif _cuit_raw:
                # CUIT ingresado pero no encontrado → ofrecer crear
                st.warning(f"⚠️ CUIT **{_cuit_raw}** no encontrado en Odoo.")
                st.checkbox("➕ Crear nuevo proveedor en Odoo", key=_create_new_vend_key)
            else:
                # Sin CUIT → permitir buscar por nombre o CUIT, o crear nuevo proveedor
                _search_query = st.text_input(
                    "🔍 Buscar proveedor por nombre o CUIT",
                    placeholder="Ej: Acme SA  ó  30-12345678-9",
                    key=f"vend_search_{uf.name}",
                    help="Buscá en Odoo por nombre o CUIT del proveedor",
                )
                if _search_query and _search_query.strip():
                    _srch_results = search_partners(
                        models_url, uid, api_key, _search_query.strip(), limit=6)
                    if _srch_results:
                        _srch_opts = {f"{r[1]} ({r[2]})": r[0] for r in _srch_results}
                        _chosen_lbl = st.selectbox(
                            "Proveedor encontrado",
                            options=list(_srch_opts.keys()),
                            key=f"vend_sel_{uf.name}",
                        )
                        if _chosen_lbl:
                            st.session_state[f"partner_override_{uf.name}"] = _srch_opts[_chosen_lbl]
                            st.session_state[f"partner_name_{uf.name}"]     = _chosen_lbl
                    else:
                        st.info("No se encontraron proveedores. Podés crear uno nuevo.")
                st.checkbox("➕ Crear nuevo proveedor en Odoo", key=_create_new_vend_key)

            if st.session_state.get(_create_new_vend_key):
                with st.expander("📝 Datos del nuevo proveedor", expanded=True):
                    with st.form(key=f"new_vendor_form_{uf.name}"):
                        st.markdown("Completá los datos mínimos para dar de alta el proveedor:")
                        _nv_c1, _nv_c2 = st.columns(2)
                        _nv_name  = _nv_c1.text_input("Razón social *",
                            value=clean_str(extracted.get("proveedor",""))[:80],
                            placeholder="Nombre en Odoo")
                        _nv_cuit  = _nv_c2.text_input("CUIT *",
                            value=_cuit_raw,
                            placeholder="30-12345678-9")
                        _nv_street = _nv_c1.text_input("Dirección", placeholder="Av. Corrientes 1234")
                        _nv_phone  = _nv_c2.text_input("Teléfono", placeholder="+54 11 4xxx-xxxx")
                        _nv_email  = st.text_input("E-mail", placeholder="proveedor@empresa.com")
                        _nv_go = st.form_submit_button("Crear proveedor en Odoo", use_container_width=True)
                    if _nv_go:
                        _nv_errs = []
                        if not _nv_name.strip():
                            _nv_errs.append("La razón social es obligatoria.")
                        _nv_cuit_ok, _nv_cuit_clean = validate_cuit(_nv_cuit)
                        if not _nv_cuit_ok:
                            _nv_errs.append(_nv_cuit_clean)
                        _nv_email_ok, _nv_email_msg = validate_email(_nv_email)
                        if not _nv_email_ok:
                            _nv_errs.append(_nv_email_msg)
                        if _nv_errs:
                            for _em in _nv_errs: st.error(_em)
                        else:
                            try:
                                _nv_pid = create_vendor_partner(
                                    models, uid, api_key,
                                    name=_nv_name.strip(),
                                    vat=_nv_cuit_clean,
                                    street=_nv_street.strip(),
                                    phone=_nv_phone.strip(),
                                    email_addr=_nv_email.strip())
                                _cuit_for_key = _nv_cuit_clean
                                st.session_state[f"vendor_created_{_cuit_for_key}"] = (_nv_pid, _nv_name.strip())
                                st.session_state[_create_new_vend_key] = False
                                st.toast(f"Proveedor {_nv_name} creado en Odoo (ID {_nv_pid})", icon="✅")
                                st.rerun()
                            except Exception as _nv_e:
                                st.error(f"Error al crear proveedor: {_nv_e}")

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

            # Resolver diario de facturas — ID directo (independiente del idioma/empresa)
            _fac_prefs    = _load_prefs_fac()
            _fac_jour_id  = None
            _fac_pref_id  = int(_fac_prefs.get("diario_facturas_id") or 0)
            _all_purch_j  = get_purchase_journals(models_url, uid, api_key)
            _purch_ids    = [jid for jid, _ in _all_purch_j]

            # 1. ID guardado en preferencias
            if _fac_pref_id and _fac_pref_id in _purch_ids:
                _fac_jour_id = _fac_pref_id

            # 2. Nombre exacto o insensible
            if not _fac_jour_id:
                _fac_pref_jour = _fac_prefs.get("diario_facturas_nombre", "")
                if _fac_pref_jour:
                    _fac_jour_id = (
                        next((jid for jid, jn in _all_purch_j if jn == _fac_pref_jour), None)
                        or next((jid for jid, jn in _all_purch_j
                                  if jn.lower() == _fac_pref_jour.lower()), None)
                    )

            # 3. Heurístico: busca "bill" o "proveedor" evitando "electr" e "importa"
            #    Si hay varios matches, prefiere el de ID menor (journal principal)
            if not _fac_jour_id and _all_purch_j:
                _candidates = [
                    jid for jid, jn in sorted(_all_purch_j, key=lambda x: x[0])
                    if (("proveedor" in jn.lower() and "factura" in jn.lower())
                        or ("vendor" in jn.lower() and "bill" in jn.lower()))
                    and "electr" not in jn.lower()
                    and "importa" not in jn.lower()
                ]
                if _candidates:
                    _fac_jour_id = _candidates[0]
                    _jn_exact = next((jn for jid, jn in _all_purch_j if jid == _fac_jour_id), "")
                    if _jn_exact:
                        _save_prefs_fac({**_fac_prefs,
                                         "diario_facturas_nombre": _jn_exact,
                                         "diario_facturas_id":     _fac_jour_id})

            with st.form(key=f"bill_form_{uf.name}"):
                # CUIT ya está fuera del form para lookup en tiempo real
                cuit_i = _cuit_raw
                # ── Tipo de comprobante ───────────────────────────────────────
                _is_nc = st.checkbox(
                    "📋 Es una Nota de Crédito de proveedor",
                    key=f"bill_is_nc_{uf.name}",
                    help="Marcá si el documento es una NC. Se registrará como "
                         "in_refund en Odoo y reducirá el saldo del proveedor.")
                _move_type = "in_refund" if _is_nc else "in_invoice"
                c1, c2 = st.columns(2)
                ref_i   = c2.text_input("N° de factura",
                            value=extracted.get("numero",""))
                fecha_i = c1.text_input("Fecha emisión (AAAA-MM-DD)",
                            value=extracted.get("fecha_iso",""),
                            placeholder="2026-05-12")
                fecha_vto_i = c2.text_input("Fecha vencimiento (AAAA-MM-DD)",
                            value=_vto_auto,
                            placeholder="2026-05-20",
                            help="Se calcula automáticamente si se detectan días en las condiciones de venta")

                concepto_i = st.text_input(
                    "📋 Concepto / Descripción",
                    value=extracted.get("concepto", ""),
                    placeholder="Descripción del servicio o producto",
                    help="Se usa como descripción de la línea en Odoo",
                )

                # Proveedor por nombre como fallback si no hay CUIT
                prov_i = st.text_input("Nombre del proveedor (fallback si no hay CUIT)",
                            value=(_partner_preloaded[1] if _partner_preloaded else extracted.get("proveedor","")[:60]),
                            placeholder="Nombre exacto en Odoo",
                            help="Se usa solo si el CUIT no resuelve a ningún proveedor")

                # Monto a cargar (editable; por defecto: neto gravado / total si exenta)
                _ca, _cb = st.columns([2, 1])
                _total_ref = extracted.get("total") or ""
                _neto_ref  = extracted.get("neto")  or ""
                _iva_ref   = extracted.get("iva")   or ""
                # Default: neto (base imponible); Odoo aplica impuestos encima.
                # Si no hay neto usa total (caso exenta: neto == total).
                _default_amount = safe_float(_neto_ref) or safe_float(_total_ref)
                amount_i = _ca.number_input(
                    "💰 Importe neto (base) *",
                    min_value=0.0,
                    value=_default_amount,
                    step=0.01,
                    format="%.2f",
                    key=f"amount_i_{uf.name}",
                    help="Base imponible (sin IVA). Odoo calcula los impuestos encima. "
                         "Para facturas exentas, ingresá el total.",
                )
                _ca.caption(
                    f"Extraído → Neto: {fmt_ars(_neto_ref)}  |  "
                    f"IVA: {fmt_ars(_iva_ref)}  |  "
                    f"Total: {fmt_ars(_total_ref)}"
                )
                # Exenta: sin IVA extraído → pre-marcar
                _exenta_default = not bool(safe_float(_iva_ref))
                exenta_i = st.checkbox(
                    "🔒 Factura exenta / Monotributo (sin impuestos)",
                    value=_exenta_default,
                    key=f"exenta_{uf.name}",
                    help="Si está marcado, se crea la línea sin ningún impuesto en Odoo.",
                )

                st.text_area("Notas internas", height=55, key=f"notas_{uf.name}")

                # Producto, Cuenta contable y Centro de Costo
                st.markdown("##### 📦 Producto / Servicio y Contabilidad")
                prod_sel = st.selectbox(
                    "Producto / Servicio",
                    options=_prod_labels,
                    index=_default_prod_idx,
                    key=f"prod_g_{uf.name}",
                    help="Producto de Odoo que se asigna a la línea de factura. "
                         "Se pre-selecciona según el proveedor.",
                )
                # Cuenta contable: auto-detectada en background (no se muestra en UI)
                # cuenta_sel sigue disponible para el asiento estimado y fallback
                cuenta_sel = _acct_labels[_default_acct_idx] if _acct_labels and _default_acct_idx < len(_acct_labels) else None
                analytic_sel = st.selectbox(
                    "Centro de Costo",
                    options=_analytic_labels,
                    index=_default_analytic_idx if _partner_preloaded else 0,
                    key=f"cc_g_{uf.name}",
                    help="Centro de costo (cuenta analítica) que absorbe el gasto. Opcional.",
                )

                _btn_label = "📋 Cargar NC en Odoo" if _is_nc else "⬆️ Cargar en Odoo"
                if _dup_exists:
                    _btn_label = "⚠️ Ya existe — Cargar igual"
                go = st.form_submit_button(_btn_label, use_container_width=True,
                                           type="secondary" if _is_nc else "primary")

            # Asiento estimado — visible siempre que haya montos
            _neto_f = extracted.get("neto","")
            _iva_f  = extracted.get("iva","")
            _tot_f  = extracted.get("total","")
            if _neto_f or _tot_f:
                # Hint sobre origen de la pre-selección
                if _default_acct_idx > 0 and _acct_pref_source:
                    _src_label = "💾 Pre-seleccionada por historial" if _acct_pref_source == "cache" else "🔗 Pre-seleccionada desde Odoo"
                    st.caption(_src_label)
                _cuenta_disp = (cuenta_sel if (cuenta_sel and cuenta_sel != "— Sin cuenta —")
                                else "*(cuenta de gasto — seleccioná arriba)*")
                st.markdown("**📒 Asiento estimado en Odoo:**")
                _percep_f   = float(extracted.get("percepcion_iibb", 0) or 0)
                _percep_det = extracted.get("percepcion_iibb_detalle", [])
                _piva_f     = float(extracted.get("percepcion_iva", 0) or 0)
                _piva_det   = extracted.get("percepcion_iva_detalle", [])
                _total_perc = _percep_f + _piva_f
                if _total_perc > 0:
                    _perc_labels = []
                    if _percep_f > 0:
                        _perc_labels.append(f"IIBB ARS {fmt_ars(_percep_f)}")
                    if _piva_f > 0:
                        _perc_labels.append(f"IVA ARS {fmt_ars(_piva_f)}")
                    st.info(f"📋 Percepciones: {' + '.join(_perc_labels)} — se registran como líneas en Odoo.")
                # Filas IIBB
                _percep_rows = ""
                if _percep_det:
                    for _pd in _percep_det:
                        _percep_rows += f"| Percepción IIBB {_pd['provincia']} | {fmt_ars(_pd['importe'])} | |\n"
                elif _percep_f > 0:
                    _percep_rows = f"| Percepciones IIBB | {fmt_ars(_percep_f)} | |\n"
                # Filas percepción IVA
                _piva_rows = ""
                if _piva_det:
                    for _pd in _piva_det:
                        _piva_rows += f"| {_pd['label']} | {fmt_ars(_pd['importe'])} | |\n"
                elif _piva_f > 0:
                    _piva_rows = f"| Percepción IVA | {fmt_ars(_piva_f)} | |\n"
                _iva27_f = float(extracted.get("iva_27", 0) or 0)
                _iva_f_num = float(_iva_f) if _iva_f else 0.0
                _iva21_explicit = float(extracted.get("iva_21", 0) or 0)
                if _iva27_f > 0:
                    # Usar iva_21 explícito si el parser lo detectó; sino estimar
                    _iva21_f = _iva21_explicit if _iva21_explicit > 0 else max(0.0, _iva_f_num - _iva27_f)
                else:
                    _iva21_f = _iva_f_num
                _iva_rows_str = ""
                if _iva27_f > 0:
                    _iva_rows_str = (
                        f"| IVA Crédito Fiscal 21% | {fmt_ars(_iva21_f)} | |\n"
                        f"| IVA Crédito Fiscal 27% | {fmt_ars(_iva27_f)} | |\n"
                    )
                else:
                    _iva_rows_str = f"| IVA Crédito Fiscal (si aplica) | {fmt_ars(_iva_f_num)} | |\n"
                st.markdown(
                    f"| Cuenta | Debe | Haber |\n"
                    f"|---|---|---|\n"
                    f"| {_cuenta_disp} | {fmt_ars(_neto_f)} | |\n"
                    + _iva_rows_str
                    + _percep_rows + _piva_rows +
                    f"| Proveedor (por pagar) | | {fmt_ars(_tot_f)} |"
                )

            if go:
                with st.spinner("Procesando..."):
                    try:
                        partner_id = False
                        # 0. Proveedor seleccionado vía búsqueda por nombre/CUIT (sin-CUIT branch)
                        _ov_id = st.session_state.get(f"partner_override_{uf.name}")
                        if _ov_id:
                            partner_id = _ov_id
                            st.caption(f"Proveedor seleccionado: {st.session_state.get(f'partner_name_{uf.name}','')}")
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
                                continue

                        # 4. Resolver cuenta seleccionada
                        account_id_sel = None
                        if cuenta_sel and cuenta_sel != "— Sin cuenta —":
                            for _aid, _albl in _bill_accounts:
                                if _albl == cuenta_sel:
                                    account_id_sel = _aid
                                    break

                        # 5. Resolver Centro de Costo
                        analytic_id_sel = None
                        if analytic_sel and analytic_sel != "— Sin centro de costo —":
                            for _anid, _anlbl in _analytic_accounts:
                                if _anlbl == analytic_sel:
                                    analytic_id_sel = _anid
                                    break

                        # 6. Resolver Producto seleccionado
                        product_id_sel = None
                        if prod_sel and prod_sel != "— Sin producto —":
                            for _epid, _eplbl in _expense_products:
                                if _eplbl == prod_sel:
                                    product_id_sel = _epid
                                    break

                        # 7. l10n_latam_document_number (número sin prefijo de letra)
                        _latam_num = (ref_i or "").strip()
                        # Si el número extraído tiene prefijo de letra, quitarlo
                        if _latam_num and re.match(r"^[A-Za-z]\d", _latam_num):
                            _latam_num = _latam_num[1:]

                        # ── Validar duplicado antes de crear ─────────────────────
                        _dup_bill, _dup_bill_name, _dup_bill_id = check_duplicate_vendor_bill(
                            models_url, uid, api_key,
                            partner_id, _latam_num or ref_i,
                            move_type=_move_type)
                        if _dup_bill:
                            _dup_url = odoo_url("account.move", _dup_bill_id) if _dup_bill_id else ""
                            st.error(
                                f"❌ Ya existe la factura **{_dup_bill_name}** con ese número "
                                f"para este proveedor en Odoo. "
                                + (f"[Abrir en Odoo]({_dup_url})" if _dup_url else ""))
                        else:
                            # Normalizar fechas: acepta DD/MM/YYYY, DD/M/YYYY o YYYY-MM-DD
                            _fecha_i_iso   = parse_ar_date(fecha_i)   if fecha_i   else ""
                            _fecha_vto_iso = parse_ar_date(fecha_vto_i) if fecha_vto_i else ""
                            # ── Resolver cuentas de percepción por provincia ──────────
                            _perc_lines = []
                            if _percep_det:
                                _all_accts_perc = get_all_accounts(models_url, uid, api_key)
                                for _pd in _percep_det:
                                    _prov_low = (_pd.get("provincia") or "").lower()
                                    # Expandir abreviaturas de provincia para el match de cuenta
                                    _prov_aliases = {
                                        "bs as": ["buenos aires", "arba", "bonaerense"],
                                        "bsas":  ["buenos aires", "arba", "bonaerense"],
                                        "buenosaires": ["buenos aires", "arba", "bonaerense"],
                                        "buenos aires": ["buenos aires", "arba", "bonaerense"],
                                        "caba":  ["caba", "ciudad"],
                                        "santa fe": ["santa fe", "santa fé"],
                                        "cordoba": ["cordoba", "córdoba"],
                                        "córdoba": ["cordoba", "córdoba"],
                                    }
                                    _prov_kws = (
                                        _prov_aliases.get(_prov_low)
                                        or [kw for kw in _prov_low.split() if len(kw) >= 4]
                                        or [_prov_low]
                                    )
                                    # Buscar cuenta que contenga "percep" + keyword de provincia
                                    _pac = next((
                                        aid for aid, albl in _all_accts_perc
                                        if "percep" in albl.lower()
                                        and any(kw in albl.lower() for kw in _prov_kws)
                                        and "(copia)" not in albl.lower()
                                    ), None)
                                    if not _pac:
                                        # Fallback: cualquier cuenta de percepción
                                        _pac = next((
                                            aid for aid, albl in _all_accts_perc
                                            if "percep" in albl.lower()
                                            and "iibb" in albl.lower()
                                            and "(copia)" not in albl.lower()
                                        ), None)
                                    _perc_lines.append({
                                        "provincia":  _pd.get("provincia", ""),
                                        "importe":    _pd.get("importe", 0),
                                        "account_id": _pac,
                                        "tax_ids":    [],
                                    })

                            # Agregar percepciones IVA al bloque de líneas
                            _piva_det_form = extracted.get("percepcion_iva_detalle", [])
                            if _piva_det_form:
                                _all_accts_piva = get_all_accounts(models_url, uid, api_key)
                                for _pvd in _piva_det_form:
                                    _piva_amt = float(_pvd.get("importe", 0))
                                    if _piva_amt <= 0:
                                        continue
                                    # Buscar cuenta percepción IVA/VAT (puede estar en español o inglés)
                                    _pvac = next((
                                        aid for aid, albl in _all_accts_piva
                                        if "percep" in albl.lower()
                                        and ("iva" in albl.lower() or "vat" in albl.lower())
                                        and "(copia)" not in albl.lower()
                                    ), None)
                                    _perc_lines.append({
                                        "label":      _pvd.get("label", "Percepción IVA"),
                                        "importe":    _piva_amt,
                                        "account_id": _pvac,
                                        "tax_ids":    [],
                                    })

                            # Agregar IVA 27% si se detectó en el PDF
                            _iva27_form = float(extracted.get("iva_27", 0) or 0)
                            if _iva27_form > 0:
                                # Buscar cuenta IVA crédito fiscal (con o sin tilde)
                                _iva27_acct = next((
                                    aid for aid, albl in get_all_accounts(models_url, uid, api_key)
                                    if "iva" in albl.lower()
                                    and ("cred" in albl.lower() or "créd" in albl.lower()
                                         or "credit" in albl.lower())
                                    and "percep" not in albl.lower()
                                    and "(copia)" not in albl.lower()
                                ), None)
                                if _iva27_acct:
                                    _perc_lines.append({
                                        "label":      "IVA Crédito Fiscal 27%",
                                        "importe":    _iva27_form,
                                        "account_id": _iva27_acct,
                                        "tax_ids":    [],
                                    })

                            # Validar que hay línea principal antes de crear
                            _has_main_line = bool(account_id_sel or product_id_sel) and bool(amount_i)
                            if not _has_main_line:
                                st.error("⚠️ Seleccioná una **cuenta de gasto** o un **producto** e ingresá el importe antes de cargar.")
                            else:
                              move_id = create_vendor_bill(models, uid, api_key,
                            partner_id=partner_id, ref=concepto_i.strip() or ref_i,
                            move_type=_move_type,
                            invoice_date=_fecha_i_iso or False,
                            invoice_date_due=_fecha_vto_iso or None,
                            journal_id=_fac_jour_id or None,
                            filename=uf.name, file_bytes=file_bytes, mimetype=mimetype,
                            account_id=account_id_sel,
                            amount_neto=amount_i if amount_i else None,
                            analytic_account_id=analytic_id_sel,
                            product_id=product_id_sel,
                            l10n_latam_document_number=_latam_num or None,
                            clear_taxes=exenta_i,
                            line_name=concepto_i.strip() or None,
                              percepcion_lines=_perc_lines if _perc_lines else None)
                            url = odoo_url("account.move", move_id)
                            _doc_lbl = "NC" if _move_type == "in_refund" else "Factura"
                            st.toast(f"{_doc_lbl} creada en Odoo", icon="✅")
                            # Aprender el diario usado para futuras cargas
                            try:
                                _inv_data = models.execute_kw(
                                    _cfg.ODOO_DB, uid, api_key,
                                    "account.move", "read",
                                    [[move_id]], {"fields": ["journal_id"]})
                                if _inv_data and _inv_data[0].get("journal_id"):
                                    _jname_used = _inv_data[0]["journal_id"][1]
                                    # Solo aprender si NO es diario electrónico ni de importaciones
                                    _skip_learn = ("electr" in _jname_used.lower()
                                                   or "importa" in _jname_used.lower())
                                    _cur_pref = _load_prefs_fac()
                                    if (not _skip_learn
                                            and _cur_pref.get("diario_facturas_nombre") != _jname_used):
                                        _save_prefs_fac({**_cur_pref, "diario_facturas_nombre": _jname_used})
                            except Exception:
                                pass
                            # Recordar la cuenta usada para este proveedor
                            if partner_id and account_id_sel:
                                _used_lbl = next((l for a, l in _bill_accounts if a == account_id_sel), "")
                                _used_prod_lbl = next((l for _, l in _expense_products if
                                    next((i for i, n in _expense_products if n == l), None) == product_id_sel), "")
                                _used_prod_id  = product_id_sel
                                _used_prod_lbl = next((l for i, l in _expense_products if i == product_id_sel), "") if product_id_sel else ""
                                _used_an_id    = analytic_id_sel
                                _used_an_lbl   = next((l for i, l in _analytic_accounts if i == analytic_id_sel), "") if analytic_id_sel else ""
                                save_vendor_account_pref(
                                    partner_id, account_id_sel, _used_lbl,
                                    product_id=_used_prod_id, product_label=_used_prod_lbl,
                                    analytic_id=_used_an_id, analytic_label=_used_an_lbl)
                            # Persistir en historial entre sesiones
                            if move_id:
                                from datetime import date as _date_today
                                append_persistent_history({
                                    "fecha": str(_date_today.today()),
                                    "hora": _dt_now.now(_AR_TZ).strftime("%H:%M"),
                                    "tipo": "NC proveedor" if _move_type == "in_refund" else "Factura proveedor",
                                    "archivo": uf.name,
                                    "id": move_id,
                                    "url": url,
                                    "estado": "✅",
                                })
                            st.markdown(f"📎 [Abrir en Odoo]({url})")
                            register_processed_file(file_bytes, uf.name, "Factura proveedor", f"ID {move_id}")
                            st.session_state.history.append({"tipo":"Factura proveedor",
                                "archivo":uf.name,"id":move_id,"url":url,"estado":"✅","hora":_dt_now.now(_AR_TZ).strftime("%H:%M")})
                    except OdooError as e:
                        show_odoo_error(e, "crear factura")


    pass  # end render
