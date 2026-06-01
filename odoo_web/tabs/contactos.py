"""Tab Contactos."""
import streamlit as st
import re
from io import BytesIO
from datetime import datetime as _dt_now
import config as _cfg
from user_prefs import load_prefs as _load_prefs
from odoo_client import (
    search_partner_by_cuit_or_name,
    create_partner,
    odoo_url,
    get_all_payment_terms,
    get_referidos,
    get_ar_states,
    get_afip_resp_types,
    get_cuit_id_type,
    get_odoo_users,
    get_pricelists,
    get_ar_accounts,
    create_full_partner,
    match_ar_state,
    OdooError,
    show_odoo_error,
    validate_cuit,
    validate_email,
    check_duplicate_file,
    register_processed_file,
    _AR_TZ,
    clean_str,
)
from parsers import extract_arca_fields, parse_alta_cliente_docx


def render(models, uid, api_key, models_url, is_admin):
    st.subheader("Alta de Contactos")
    st.caption("Pre-completá los datos subiendo la constancia ARCA (PDF) o el formulario interno (.docx).")

    # ── Mensaje persistente post-acción ───────────────────────────────────
    if "ct_arca_msg" in st.session_state:
        _msg_type, _msg_text = st.session_state.pop("ct_arca_msg")
        if _msg_type == "success":
            st.success(_msg_text)
        else:
            st.warning(_msg_text)

    # ── Documento (constancia ARCA PDF o formulario interno DOCX) ────────
    _ct_file = st.file_uploader(
        "O subí la constancia ARCA (PDF) o el formulario Alta Cliente (.docx)",
        type=["pdf", "docx"], key="ct_arca_upload",
        accept_multiple_files=False,
    )

    # ── Determinar fuente de datos ─────────────────────────────────────────
    _arca = {}
    if _ct_file:
        _ct_bytes = _ct_file.read()
        _ct_file_key = f"ct_pdf_{hash(_ct_bytes)}"
        if _ct_file_key not in st.session_state:
            _ct_fname = (_ct_file.name or "").lower()
            _is_docx  = _ct_fname.endswith(".docx")
            if _is_docx:
                # Formulario interno Alta Cliente
                with st.spinner("Leyendo formulario Alta Cliente..."):
                    try:
                        _arca_docx = parse_alta_cliente_docx(_ct_bytes)
                        st.session_state["ct_arca_data"]  = _arca_docx
                        st.session_state["ct_arca_source"] = "formulario"
                        st.session_state["ct_form_ver"] = st.session_state.get("ct_form_ver", 0) + 1
                        st.session_state[_ct_file_key] = True
                        _msg_nombre = _arca_docx.get("nombre") or "?"
                        _msg_cuit   = _arca_docx.get("cuit")   or "?"
                        st.session_state["ct_arca_msg"] = ("success",
                            f"✅ Formulario leído: {_msg_nombre} · CUIT {_msg_cuit}")
                        st.rerun()
                    except Exception as _ce:
                        st.warning(f"No se pudo leer el formulario: {_ce}")
            else:
                # Constancia ARCA PDF
                with st.spinner("Leyendo constancia ARCA..."):
                    try:
                        import pdfplumber
                        with pdfplumber.open(BytesIO(_ct_bytes)) as _pdf:
                            _ct_text = "\n".join(p.extract_text() or "" for p in _pdf.pages)
                        _arca_pdf = extract_arca_fields(_ct_text)
                        st.session_state["ct_arca_data"]  = _arca_pdf
                        st.session_state["ct_arca_source"] = "pdf"
                        st.session_state["ct_form_ver"] = st.session_state.get("ct_form_ver", 0) + 1
                        st.session_state[_ct_file_key] = True
                        st.session_state["ct_arca_msg"] = ("success",
                            f"✅ {_arca_pdf.get('nombre','?')} · CUIT {_arca_pdf.get('cuit','?')}")
                        st.rerun()
                    except Exception as _ce:
                        st.warning(f"No se pudo leer el PDF: {_ce}")
        _arca = st.session_state.get("ct_arca_data", {})
    else:
        _arca = st.session_state.get("ct_arca_data", {})

    # ── Cargar catálogos Odoo ──────────────────────────────────────────────
    _ct_states   = get_ar_states(models_url, uid, api_key)
    _ct_afip     = get_afip_resp_types(models_url, uid, api_key)
    _ct_users    = get_odoo_users(models_url, uid, api_key)
    _ct_pts      = get_all_payment_terms(models_url, uid, api_key)   # [(id,name)]
    _ct_plists   = get_pricelists(models_url, uid, api_key)
    _ct_cuit_tid  = get_cuit_id_type(models_url, uid, api_key)
    _ct_referidos = get_referidos(models_url, uid, api_key)

    # helpers: map name→id
    _state_map  = {n: i for i, n in _ct_states}
    _afip_map   = {n: i for i, n in _ct_afip}
    _user_map   = {n: i for i, n in _ct_users}
    _pt_map     = {n: i for i, n in _ct_pts}
    _plist_map    = {n: i for i, n in _ct_plists}
    _referido_map = {n: i for i, n in _ct_referidos}

    # Default provincia
    _def_prov = ""
    if _arca.get("province_name"):
        _sid = match_ar_state(_arca["province_name"], _ct_states)
        if _sid:
            _def_prov = next((n for i, n in _ct_states if i == _sid), "")

    # Default tipo responsabilidad
    _afip_names = list(_afip_map.keys())
    _def_afip_idx = 0
    _tr = _arca.get("tipo_resp", "RI")
    for _ai, _an in enumerate(_afip_names):
        _an_up = _an.upper()
        if _tr == "RI" and ("RESPONSABLE INSCRIPTO" in _an_up or "IVA" in _an_up):
            _def_afip_idx = _ai; break
        if _tr == "MONO" and "MONOTRIBUTO" in _an_up:
            _def_afip_idx = _ai; break
        if _tr == "EX" and "EXENTO" in _an_up:
            _def_afip_idx = _ai; break

    # Pre-cargar cuentas contables
    _ct_accts_rec = get_ar_accounts(models_url, uid, api_key, "asset_receivable")
    _ct_accts_pay = get_ar_accounts(models_url, uid, api_key, "liability_payable")
    _ct_acct_rec_map = {n: i for i, n in _ct_accts_rec}
    _ct_acct_pay_map = {n: i for i, n in _ct_accts_pay}

    # Default términos de pago desde formulario interno (match por keyword)
    _def_pt_idx = 0
    _pt_hint = " ".join(filter(None, [
        _arca.get("forma_pago", ""), _arca.get("plazos", "")])).lower()
    if _pt_hint:
        _pt_names_list = ["— Sin plazo —"] + list(_pt_map.keys())
        for _pti, _ptn in enumerate(_pt_names_list):
            # Match numérico (ej: "30" en "30 días") o keyword (ej: "contado")
            _ptn_low = _ptn.lower()
            _nums_hint = re.findall(r"\d+", _pt_hint)
            _nums_pt   = re.findall(r"\d+", _ptn_low)
            if _nums_hint and _nums_pt and _nums_hint[0] == _nums_pt[0]:
                _def_pt_idx = _pti; break
            if any(kw in _ptn_low for kw in ["contado", "inmediato"] if kw in _pt_hint):
                _def_pt_idx = _pti; break

    # Referencia interna pre-llenada con datos del formulario interno
    _def_ref_interna = ""
    _ref_parts = []
    if _arca.get("iibb"):
        _ref_parts.append(f"IIBB: {_arca['iibb']}")
    if _arca.get("transport_name"):
        _ref_parts.append(f"Transporte: {_arca['transport_name']}")
    if _arca.get("delivery_address"):
        _ref_parts.append(f"Entrega: {_arca['delivery_address']}")
    _def_ref_interna = " | ".join(_ref_parts)

    # ── Botón limpiar ─────────────────────────────────────────────────────
    if _arca or st.session_state.get("ct_arca_data"):
        if st.button("🗑️ Limpiar / Nuevo contacto", key="ct_clear"):
            for _k in list(st.session_state.keys()):
                if any(_k.startswith(p) for p in
                       ("ct_arca", "ct_pdf_", "ct_form_ver", "ct_clear")):
                    st.session_state.pop(_k, None)
            st.rerun()

    with st.form(f"ct_form_{st.session_state.get('ct_form_ver', 0)}"):
        # ── Persona / Empresa ──────────────────────────────────────────────
        _ct_company_type = st.radio(
            "Tipo de entidad", ["🏢 Empresa", "👤 Persona"],
            horizontal=True, index=0,
        )
        _ct_is_company_val = (_ct_company_type == "🏢 Empresa")

        # ── Tipo de contacto ───────────────────────────────────────────────
        st.markdown("##### 🏷️ Rol")
        _ct_c1, _ct_c2 = st.columns(2)
        _ct_is_customer = _ct_c1.checkbox("Es cliente", value=True)
        _ct_is_supplier = _ct_c2.checkbox("Es proveedor", value=False)

        # ── Datos básicos ──────────────────────────────────────────────────
        st.markdown("##### 🏢 Datos básicos")
        _ct_b1, _ct_b2 = st.columns(2)
        _ct_name  = _ct_b1.text_input("Razón social *",
            value=clean_str(_arca.get("nombre", "")),
            placeholder="ACME S.A.")
        _ct_cuit  = _ct_b2.text_input("CUIT *",
            value=clean_str(_arca.get("cuit", "")),
            placeholder="30-12345678-9")
        _ct_phone = _ct_b1.text_input("Teléfono",
            value=clean_str(_arca.get("phone", "")),
            placeholder="+54 351 xxx-xxxx")
        _ct_email = _ct_b2.text_input("Correo electrónico",
            value=clean_str(_arca.get("email", "")),
            placeholder="contacto@empresa.com")
        _ct_web   = st.text_input("Sitio web",
            value=clean_str(_arca.get("website", "")),
            placeholder="https://www.empresa.com")

        # ── Dirección ──────────────────────────────────────────────────────
        st.markdown("##### 📍 Dirección fiscal")
        _ct_d1, _ct_d2, _ct_d3 = st.columns([3, 2, 1])
        _ct_street = _ct_d1.text_input("Calle y número",
            value=clean_str(_arca.get("street", "")))
        _ct_city   = _ct_d2.text_input("Ciudad / Localidad",
            value=clean_str(_arca.get("city", "")))
        _ct_zip    = _ct_d3.text_input("C.P.",
            value=clean_str(_arca.get("zip_code", "")))
        _ct_state_opts = ["— Seleccionar —"] + list(_state_map.keys())
        _ct_state_def  = _ct_state_opts.index(_def_prov) if _def_prov in _ct_state_opts else 0
        _ct_state_sel  = st.selectbox("Provincia", _ct_state_opts, index=_ct_state_def)

        # ── AFIP ───────────────────────────────────────────────────────────
        st.markdown("##### 🏛️ Fiscal AFIP")
        _ct_f1, _ct_f2 = st.columns(2)
        _ct_afip_sel = _ct_f1.selectbox(
            "Tipo de responsabilidad AFIP",
            _afip_names if _afip_names else ["(no disponible)"],
            index=_def_afip_idx,
        )
        _ct_ref = _ct_f2.text_input("Referencia interna",
            value=_def_ref_interna,
            placeholder="Ej: Canal Online, Zona Norte · IIBB: 924-8306909")

        # ── Ventas ─────────────────────────────────────────────────────────
        st.markdown("##### 💼 Ventas")
        _ct_v1, _ct_v2, _ct_v3 = st.columns(3)
        _ct_user_opts = ["— Sin vendedor —"] + list(_user_map.keys())
        _ct_user_sel  = _ct_v1.selectbox("Vendedor", _ct_user_opts)
        _ct_pt_opts   = ["— Sin plazo —"] + list(_pt_map.keys())
        _ct_pt_sel    = _ct_v2.selectbox("Términos de pago (ventas)", _ct_pt_opts,
            index=_def_pt_idx)
        _ct_pl_opts   = ["— Predeterminado —"] + list(_plist_map.keys())
        _ct_pl_sel    = _ct_v3.selectbox("Lista de precios", _ct_pl_opts)

        _ct_ref_opts  = ["— Sin referido —"] + list(_referido_map.keys())
        _prefs_ct = _load_prefs()
        _pref_ref_ct = _prefs_ct.get("referido_nombre", "")
        _ct_ref_def = (_ct_ref_opts.index(_pref_ref_ct)
                       if _pref_ref_ct in _ct_ref_opts else 0)
        _ct_ref_sel   = st.selectbox(
            "Referido",
            _ct_ref_opts,
            index=_ct_ref_def,
            help="Quien refirio a este contacto",
        )

        # ── Compras ────────────────────────────────────────────────────────
        st.markdown("##### 🛒 Compras")
        _ct_p1, _ct_p2 = st.columns(2)
        _ct_pt_purch_sel = _ct_p1.selectbox("Términos de pago (compras)", _ct_pt_opts,
            key="ct_pt_purch")
        _ct_ref_purch = _ct_p2.text_input("Referencia del proveedor",
            placeholder="Código que el proveedor nos asigna")

        # ── Contabilidad ───────────────────────────────────────────────────
        st.markdown("##### 📊 Contabilidad")
        _ct_acct_c1, _ct_acct_c2 = st.columns(2)
        _ct_rec_opts = ["— Predeterminada —"] + list(_ct_acct_rec_map.keys())
        _ct_pay_opts = ["— Predeterminada —"] + list(_ct_acct_pay_map.keys())
        # Default: primera cuenta de cada tipo
        _ct_rec_def = 1 if _ct_accts_rec else 0
        _ct_pay_def = 1 if _ct_accts_pay else 0
        _ct_acct_rec_sel = _ct_acct_c1.selectbox(
            "Cuenta por cobrar", _ct_rec_opts, index=_ct_rec_def,
            help="Cuenta contable para créditos por ventas")
        _ct_acct_pay_sel = _ct_acct_c2.selectbox(
            "Cuenta por pagar", _ct_pay_opts, index=_ct_pay_def,
            help="Cuenta contable para proveedores")
        _ct_credit_limit = st.number_input(
            "Límite de crédito", min_value=0.0, value=0.0,
            step=1000.0, format="%.2f",
            help="0 = sin límite")

        # ── Notas ──────────────────────────────────────────────────────────
        st.markdown("##### 📝 Notas internas")
        _ct_notes = st.text_area("Notas", height=60, label_visibility="collapsed",
            value=clean_str(_arca.get("actividad_principal", "")))

        _ct_go = st.form_submit_button("💾 Crear en Odoo", use_container_width=True, type="primary")

    if _ct_go:
        _ct_errs = []
        if not _ct_name.strip():
            _ct_errs.append("La razón social es obligatoria.")
        _cuit_ok, _cuit_clean_or_msg = validate_cuit(_ct_cuit)
        if not _cuit_ok:
            _ct_errs.append(_cuit_clean_or_msg)
        _email_ok, _email_msg = validate_email(_ct_email)
        if not _email_ok:
            _ct_errs.append(_email_msg)
        if _ct_errs:
            for _em in _ct_errs:
                st.error(_em)
        else:
            with st.spinner("Creando contacto en Odoo..."):
                try:
                    _ct_vals = {
                        "name":          _ct_name.strip(),
                        "is_company":    _ct_is_company_val,
                        "company_type":  "company" if _ct_is_company_val else "person",
                        "customer_rank": 1 if _ct_is_customer else 0,
                        "supplier_rank": 1 if _ct_is_supplier else 0,
                    }
                    # CUIT (ya validado y limpio)
                    _vat_clean = _cuit_clean_or_msg
                    _ct_vals["vat"] = _vat_clean
                    if _ct_cuit_tid:
                        _ct_vals["l10n_latam_identification_type_id"] = _ct_cuit_tid

                    # Contacto
                    if _ct_phone.strip(): _ct_vals["phone"]   = _ct_phone.strip()
                    if _ct_email.strip(): _ct_vals["email"]   = _ct_email.strip()
                    if _ct_web.strip():   _ct_vals["website"] = _ct_web.strip()

                    # Dirección
                    if _ct_street.strip(): _ct_vals["street"] = _ct_street.strip()
                    if _ct_city.strip():   _ct_vals["city"]   = _ct_city.strip()
                    if _ct_zip.strip():    _ct_vals["zip"]    = _ct_zip.strip()
                    if _ct_state_sel and _ct_state_sel != "— Seleccionar —":
                        _ct_vals["state_id"]   = _state_map[_ct_state_sel]
                        _ct_vals["country_id"] = models.execute_kw(
                            _cfg.ODOO_DB, uid, api_key, "res.country", "search",
                            [[["code", "=", "AR"]]], {"limit": 1})[0]

                    # AFIP
                    if _ct_afip_sel and _ct_afip_sel in _afip_map:
                        _ct_vals["l10n_ar_afip_responsibility_type_id"] = _afip_map[_ct_afip_sel]

                    # Referencia
                    if _ct_ref.strip(): _ct_vals["ref"] = _ct_ref.strip()

                    # Ventas
                    if _ct_user_sel != "— Sin vendedor —":
                        _ct_vals["user_id"] = _user_map[_ct_user_sel]
                    if _ct_pt_sel != "— Sin plazo —":
                        _ct_vals["property_payment_term_id"] = _pt_map[_ct_pt_sel]
                    if _ct_pl_sel != "— Predeterminado —":
                        _ct_vals["property_product_pricelist"] = _plist_map[_ct_pl_sel]
                    if _ct_ref_sel != "— Sin referido —" and _ct_ref_sel in _referido_map:
                        _ct_vals["x_studio_referido_1"] = _referido_map[_ct_ref_sel]

                    # Compras
                    if _ct_pt_purch_sel != "— Sin plazo —":
                        _ct_vals["property_supplier_payment_term_id"] = _pt_map[_ct_pt_purch_sel]
                    if _ct_ref_purch.strip():
                        _ct_vals["ref"] = _ct_ref_purch.strip()

                    # Notas
                    if _ct_notes.strip():
                        _ct_vals["comment"] = _ct_notes.strip()


                    # Cuentas contables
                    if _ct_acct_rec_sel != "— Predeterminada —" and _ct_acct_rec_sel in _ct_acct_rec_map:
                        _ct_vals["property_account_receivable_id"] = _ct_acct_rec_map[_ct_acct_rec_sel]
                    if _ct_acct_pay_sel != "— Predeterminada —" and _ct_acct_pay_sel in _ct_acct_pay_map:
                        _ct_vals["property_account_payable_id"] = _ct_acct_pay_map[_ct_acct_pay_sel]

                    # Límite de crédito
                    if _ct_credit_limit > 0:
                        _ct_vals["credit_limit"] = _ct_credit_limit

                    _new_pid = create_full_partner(models, uid, api_key, _ct_vals)
                    _new_url = odoo_url("res.partner", _new_pid)
                    # Limpiar datos de ARCA para el próximo contacto
                    for _k in list(st.session_state.keys()):
                        if _k.startswith("ct_arca") or _k.startswith("ct_pdf_") or _k == "ct_form_ver":
                            st.session_state.pop(_k, None)
                    st.toast("Contacto creado en Odoo", icon="✅")
                    st.markdown(f"🎉 **{_ct_name}** creado · [Abrir en Odoo]({_new_url})")
                    st.session_state.history.append({
                        "tipo": "Contacto",
                        "archivo": _ct_name,
                        "id": _new_pid,
                        "url": _new_url,
                        "estado": "✅",
                        "hora": _dt_now.now(_AR_TZ).strftime("%H:%M"),
                    })
                except OdooError as _cte:
                    show_odoo_error(_cte, "crear contacto")
                except Exception as _cte:
                    show_odoo_error(_cte, "crear contacto")

    st.divider()

    # ═══════════════════════════════════════════════════════════════════════
    # SECCIÓN: Búsqueda de contactos existentes en Odoo
    # ═══════════════════════════════════════════════════════════════════════
    with st.expander("🔍 Buscar contacto existente en Odoo", expanded=False):
        st.caption("Buscá un contacto por nombre, CUIT o email.")
        _ct_sq, _ct_sbtn = st.columns([4, 1])
        _ct_search_q = _ct_sq.text_input(
            "Nombre, CUIT o email",
            key="ct_search_q",
            placeholder="Ej: Juan, 30-12345678-9, juan@empresa.com",
            label_visibility="collapsed")
        _ct_search_btn = _ct_sbtn.button("🔍 Buscar", key="ct_search_btn", use_container_width=True)

        if _ct_search_btn and _ct_search_q.strip():
            with st.spinner("Buscando..."):
                _ct_results = search_partner_by_cuit_or_name(
                    models_url, uid, api_key, _ct_search_q.strip(), limit=15)
            if not _ct_results:
                st.info("No se encontraron contactos.")
            else:
                import pandas as _ct_pd
                _ct_rows = []
                for _r in _ct_results:
                    _ct_rows.append({
                        "Nombre":  _r.get("name",""),
                        "CUIT":    _r.get("vat","") or "—",
                        "Email":   _r.get("email","") or "—",
                        "Tel.":    _r.get("phone","") or _r.get("mobile","") or "—",
                        "Ciudad":  _r.get("city","") or "—",
                        "Link":    f"[Abrir]({_cfg.ODOO_URL}/odoo/contacts/{_r['id']})",
                    })
                st.dataframe(_ct_pd.DataFrame(_ct_rows), use_container_width=True, hide_index=True)
                st.caption(f"{len(_ct_results)} contacto(s) encontrado(s).")

    # ═══════════════════════════════════════════════════════════════════════
    # SECCIÓN: Carga masiva de contactos desde Excel
    # ═══════════════════════════════════════════════════════════════════════
    with st.expander("📥 Carga masiva desde Excel", expanded=False):
        st.caption(
            "Subí un Excel con columnas: **Nombre** (obligatorio), **CUIT**, Email, "
            "Teléfono, Dirección, Ciudad, CP. "
            "La app omite los que ya existan en Odoo por CUIT.")
        _bulk_file = st.file_uploader(
            "Excel de contactos", type=["xlsx","xls"],
            key="ct_bulk_upload")

        if _bulk_file:
            import pandas as _bpd
            try:
                _bdf = _bpd.read_excel(_bulk_file, dtype=str).fillna("")
            except Exception as _bxe:
                st.error(f"No se pudo leer el Excel: {_bxe}")
                _bdf = None

            if _bdf is not None and not _bdf.empty:
                # Normalizar nombres de columnas
                _bcol_map = {}
                for _col in _bdf.columns:
                    _cl = _col.lower().strip()
                    if any(k in _cl for k in ["nombre","razon","razón","name"]):
                        _bcol_map["nombre"] = _col
                    elif any(k in _cl for k in ["cuit","ruc","nit","vat"]):
                        _bcol_map["cuit"] = _col
                    elif "email" in _cl or "mail" in _cl:
                        _bcol_map["email"] = _col
                    elif any(k in _cl for k in ["tel","phone","celular","movil","móvil"]):
                        _bcol_map["telefono"] = _col
                    elif any(k in _cl for k in ["direcc","street","domicilio"]):
                        _bcol_map["direccion"] = _col
                    elif any(k in _cl for k in ["ciudad","city","localidad"]):
                        _bcol_map["ciudad"] = _col
                    elif any(k in _cl for k in ["cp","zip","postal","codigo postal"]):
                        _bcol_map["cp"] = _col

                if "nombre" not in _bcol_map:
                    st.error("No se encontró columna de Nombre/Razón Social en el Excel.")
                else:
                    # Construir lista de contactos a importar
                    _bulk_rows = []
                    for _, _br in _bdf.iterrows():
                        _bnombre = str(_br.get(_bcol_map.get("nombre",""), "")).strip()
                        if not _bnombre:
                            continue
                        _bulk_rows.append({
                            "nombre":    _bnombre,
                            "cuit":      str(_br.get(_bcol_map.get("cuit",""), "")).strip().replace("-","").replace(" ",""),
                            "email":     str(_br.get(_bcol_map.get("email",""), "")).strip(),
                            "telefono":  str(_br.get(_bcol_map.get("telefono",""), "")).strip(),
                            "direccion": str(_br.get(_bcol_map.get("direccion",""), "")).strip(),
                            "ciudad":    str(_br.get(_bcol_map.get("ciudad",""), "")).strip(),
                            "cp":        str(_br.get(_bcol_map.get("cp",""), "")).strip(),
                        })

                    st.info(f"**{len(_bulk_rows)} contacto(s)** detectados en el archivo.")

                    # Preview + estado de duplicados
                    if _bulk_rows:
                        _prev_rows = []
                        for _br in _bulk_rows:
                            _exists = ""
                            if _br["cuit"] and len(_br["cuit"]) >= 10:
                                try:
                                    _ex = models.execute_kw(
                                        _cfg.ODOO_DB, uid, api_key,
                                        "res.partner", "search_read",
                                        [[("vat","=",_br["cuit"]),("active","=",True)]],
                                        {"fields":["id","name"],"limit":1})
                                    _exists = f"✅ Ya existe: {_ex[0]['name']}" if _ex else "🆕 Nuevo"
                                except Exception:
                                    _exists = "?"
                            else:
                                _exists = "⚠️ Sin CUIT"
                            _prev_rows.append({
                                "Nombre":    _br["nombre"],
                                "CUIT":      _br["cuit"] or "—",
                                "Email":     _br["email"] or "—",
                                "Teléfono":  _br["telefono"] or "—",
                                "Ciudad":    _br["ciudad"] or "—",
                                "Estado":    _exists,
                            })

                        _bpd2 = __import__("pandas")
                        st.dataframe(_bpd2.DataFrame(_prev_rows), use_container_width=True, hide_index=True)

                        _new_count = sum(1 for r in _prev_rows if "Nuevo" in r["Estado"])
                        _skip_count = len(_prev_rows) - _new_count

                        if _new_count == 0:
                            st.warning("Todos los contactos ya existen en Odoo por CUIT.")
                        else:
                            if _skip_count > 0:
                                st.caption(f"Se omitirán {_skip_count} contacto(s) ya existente(s). Se crearán {_new_count}.")
                            if st.button(
                                f"➕ Crear {_new_count} contacto(s) en Odoo",
                                type="primary", key="ct_bulk_create"):
                                _created = 0
                                _errors  = []
                                for _br, _pr in zip(_bulk_rows, _prev_rows):
                                    if "Nuevo" not in _pr["Estado"]:
                                        continue
                                    try:
                                        _new_id = create_partner(
                                            models, uid, api_key,
                                            _br["nombre"],
                                            _br["cuit"] or "",
                                            _br["direccion"],
                                            _br["telefono"],
                                            _br["email"],
                                        )
                                        st.session_state.history.append({
                                            "tipo":   "Contacto",
                                            "archivo": _br["nombre"],
                                            "id":      _new_id,
                                            "url":     odoo_url("res.partner", _new_id),
                                            "estado":  "✅",
                                            "hora":    _dt_now.now(_AR_TZ).strftime("%H:%M"),
                                        })
                                        _created += 1
                                    except Exception as _be:
                                        _errors.append(f"{_br['nombre']}: {_be}")
                                if _created:
                                    st.toast(f"{_created} contacto(s) creados en Odoo", icon="✅")
                                if _errors:
                                    st.warning("Errores:\n" + "\n".join(_errors))

