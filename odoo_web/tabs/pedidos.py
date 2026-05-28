"""Tab Pedidos de Clientes."""
import streamlit as st
import pandas as pd
import re
from datetime import datetime as _dt_now
import config as _cfg
from odoo_client import (
    create_sale_order,
    odoo_url,
    safe_float,
    fmt_ars,
    parse_payment_terms,
    search_partner_by_cuit,
    search_partner_by_cuit_or_name,
    get_all_payment_terms,
    get_customer_payment_terms,
    search_product_by_code_or_name,
    get_ejecutivo_field,
    get_referidos,
    create_partner,
    OdooError,
    show_odoo_error,
    show_odoo_warning,
    check_duplicate_file,
    register_processed_file,
)
from parsers import extract_image_oc_fields, extract_oc_fields, extract_excel_oc_fields


def render(models, uid, api_key, models_url, is_admin):
    st.subheader("Pedidos de clientes")
    files_o = st.file_uploader("Arrastrá o elegí archivos (PDF, JPG, PNG, XLSX)",
        type=["pdf","jpg","jpeg","png","xlsx","xls"], accept_multiple_files=True, key="orders_upload")
    if not files_o:
        st.caption("Subí uno o más archivos para empezar.")
    for uf in (files_o or []):
        st.divider()
        ext        = uf.name.rsplit(".", 1)[-1].lower()
        file_bytes = uf.read()
        mimetype   = _cfg.MIMETYPES.get(ext, "application/octet-stream")
        st.markdown(f"**📎 {uf.name}**  `{ext.upper()}`  ({len(file_bytes)//1024} KB)")
        # ── Detección de duplicados ───────────────────────────────────
        _is_dup, _dup_entry = check_duplicate_file(file_bytes, uf.name)
        if _is_dup:
            st.warning(
                f"⚠️ **{uf.name}** ya fue procesado en esta sesión "
                f"({_dup_entry.get('hora','?')} · {_dup_entry.get('resultado','')}). "
                "Subiste el mismo archivo dos veces.")
            continue
        if ext in ("xlsx","xls"):
            # ── Parseo inteligente de Excel de pedido ─────────────────────
            with st.spinner("Leyendo Excel..."):
                oc_fields_xl = extract_excel_oc_fields(file_bytes, filename=uf.name)
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
            # Pre-llenar CUIT si lo extrajo el parser
            if not st.session_state[_cuit_key_xl] and oc_fields_xl.get("cuit"):
                st.session_state[_cuit_key_xl] = oc_fields_xl["cuit"]

            # ── Búsqueda de cliente por CUIT o razón social ───────────────
            _xl_pid = st.session_state[_pid_key_xl]
            _xl_pnm = st.session_state[_pnm_key_xl]

            # Si ya hay cliente asignado mostrar con opción de cambiar
            if _xl_pid:
                _cc1, _cc2 = st.columns([5, 1])
                _cc1.success(f"✅ Cliente: **{_xl_pnm}**")
                if _cc2.button("✏️ Cambiar", key=f"xl_change_{uf.name}"):
                    st.session_state[_pid_key_xl] = None
                    st.session_state[_pnm_key_xl] = ""
                    st.session_state[_cuit_key_xl] = ""
                    st.rerun()
            else:
                _xl_q = st.text_input(
                    "Buscar cliente por CUIT o razón social",
                    key=_cuit_key_xl,
                    placeholder="ej: 30-12345678-9  o  Brant  o  Fanttik",
                    help="Escribí el CUIT completo o parte de la razón social y presioná Enter",
                )
                _xl_q_val = (_xl_q or "").strip()
                if _xl_q_val and len(_xl_q_val) >= 3:
                    _xl_cands = search_partner_by_cuit_or_name(
                        models_url, uid, api_key, _xl_q_val, limit=8)
                    if len(_xl_cands) == 1:
                        # Match único: asignar directo
                        st.session_state[_pid_key_xl] = _xl_cands[0]["id"]
                        st.session_state[_pnm_key_xl] = _xl_cands[0]["name"]
                        st.rerun()
                    elif len(_xl_cands) > 1:
                        # Múltiples resultados: selectbox de elección
                        _xl_opt_map = {
                            f"{r['name']}  [{r.get('vat') or '—'}]": r
                            for r in _xl_cands}
                        _xc1, _xc2 = st.columns([4, 1])
                        with _xc1:
                            _xl_sel_lbl = st.selectbox(
                                "Resultados", list(_xl_opt_map.keys()),
                                key=f"xl_sel_{uf.name}",
                                label_visibility="collapsed")
                        with _xc2:
                            st.write("")
                            if st.button("✅ Usar", key=f"xl_use_{uf.name}"):
                                _ch = _xl_opt_map[_xl_sel_lbl]
                                st.session_state[_pid_key_xl] = _ch["id"]
                                st.session_state[_pnm_key_xl] = _ch["name"]
                                st.rerun()
                    else:
                        st.warning(f"⚠️ Sin resultados para «{_xl_q_val}».")
                        with st.expander("➕ Crear nuevo cliente", expanded=False):
                            _xnc1, _xnc2 = st.columns(2)
                            _xnc_name   = _xnc1.text_input("Razón social *", key=f"xl_nc_name_{uf.name}")
                            _xnc_cuit_v = _xnc2.text_input("CUIT *", key=f"xl_nc_cuit_{uf.name}",
                                value=_xl_q_val if re.sub(r"[^\d]","",_xl_q_val).__len__() >= 10 else "",
                                placeholder="30-12345678-9")
                            _xnc_street = _xnc1.text_input("Dirección", key=f"xl_nc_st_{uf.name}")
                            _xnc_phone  = _xnc2.text_input("Teléfono",  key=f"xl_nc_ph_{uf.name}")
                            _xnc_email  = st.text_input("Email", key=f"xl_nc_em_{uf.name}")
                            if st.button("Crear cliente", key=f"xl_btn_nc_{uf.name}"):
                                if _xnc_name and _xnc_cuit_v:
                                    try:
                                        _new_xl_pid = create_partner(models, uid, api_key,
                                            _xnc_name, _xnc_cuit_v,
                                            _xnc_street, _xnc_phone, _xnc_email)
                                        st.session_state[_pid_key_xl] = _new_xl_pid
                                        st.session_state[_pnm_key_xl] = _xnc_name
                                        st.rerun()
                                    except Exception as _xe:
                                        show_odoo_error(_xe, "crear cliente")
                                else:
                                    st.warning("Razón social y CUIT son obligatorios.")
                elif _xl_q_val:
                    st.caption("Escribí al menos 3 caracteres para buscar.")
                else:
                    st.info("📌 Ingresá el CUIT o razón social del cliente para continuar.")

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
                for _i_ln, _ln in enumerate(_lineas_xl):
                    _ov_key    = f"prod_ov_{uf.name}_{_i_ln}"
                    _cache_key = f"prod_cache_{uf.name}_{_i_ln}"
                    if _cache_key not in st.session_state:
                        _prods = search_product_by_code_or_name(
                            models_url, uid, api_key,
                            code=_ln.get("modelo","") or _ln.get("codigo",""),
                            name_keywords=(_ln.get("descripcion","") or _ln.get("modelo","")).strip(),
                            limit=1)
                        if _prods:
                            st.session_state[_cache_key] = _prods[0]
                    _op_auto = st.session_state.get(_cache_key)
                    _op      = st.session_state[_ov_key] if _ov_key in st.session_state else _op_auto
                    _cost    = float(_op["standard_price"]) if _op else 0.0
                    _price   = safe_float(_ln.get("precio_unit", 0))
                    _margin  = ((_price - _cost) / _price * 100) if _price > 0 else 0.0
                    _xl_enriched.append({**_ln, "odoo_product": _op,
                                         "cost": _cost, "margin_pct": _margin,
                                         "_ov_key": _ov_key})

                # ── Tabla de productos (Producto Odoo editable inline) ──────
                _tbl_rows = []
                for _i_el, _el in enumerate(_xl_enriched):
                    _op = _el.get("odoo_product")
                    _tbl_rows.append({
                        "":            "✅" if _op else "⚠️",
                        "Modelo":      _el.get("modelo") or "",
                        "Descripción": (_el.get("descripcion") or "")[:50],
                        "Cant.":       int(_el.get("cantidad", 0)),
                        "P. Unit.":    float(_el.get("precio_unit", 0)),
                        "IVA %":       int(_el.get("iva_pct", 21)),
                        "Costo":       float(_el.get("cost", 0)),
                        "Margen %":    round(float(_el.get("margin_pct", 0)), 1),
                        "Producto Odoo": _op["name"] if _op else "",
                    })
                _tbl_cfg = {
                    "":              st.column_config.TextColumn("", width="small"),
                    "Cant.":         st.column_config.NumberColumn("Cant.", format="%d"),
                    "P. Unit.":      st.column_config.NumberColumn("P. Unit.", format="$ %.2f"),
                    "IVA %":         st.column_config.NumberColumn("IVA %", format="%d%%"),
                    "Costo":         st.column_config.NumberColumn("Costo", format="$ %.2f"),
                    "Margen %":      st.column_config.NumberColumn("Margen %", format="%.1f%%"),
                    "Producto Odoo": st.column_config.TextColumn(
                        "Producto Odoo",
                        help="✏️ Escribí nombre o código (parcial) y presioná Enter para buscar"),
                }
                _tbl_disabled_cols = ["", "Modelo", "Descripción", "Cant.",
                                      "P. Unit.", "IVA %", "Costo", "Margen %"]
                # Versión del key: cambia tras cada match exitoso para limpiar el estado del editor
                _tbl_ver   = st.session_state.get(f"_tbl_ver_{uf.name}", 0)
                _tbl_edited = st.data_editor(
                    pd.DataFrame(_tbl_rows), column_config=_tbl_cfg,
                    disabled=_tbl_disabled_cols,
                    use_container_width=True, hide_index=True,
                    key=f"tbl_ped_{uf.name}_{_tbl_ver}")

                # ── Procesar ediciones inline de Producto Odoo ─────────────
                _tbl_updated  = False
                _tbl_no_match = []
                for _i_ed, _row_ed in _tbl_edited.iterrows():
                    _new_q   = (_row_ed.get("Producto Odoo") or "").strip()
                    _old_op  = _xl_enriched[_i_ed].get("odoo_product")
                    _old_name = _old_op["name"] if _old_op else ""
                    if not _new_q or _new_q == _old_name:
                        continue  # sin cambio
                    _ck_ed = _xl_enriched[_i_ed].get("_ov_key")
                    _res = search_product_by_code_or_name(
                        models_url, uid, api_key,
                        code=_new_q, name_keywords=_new_q, limit=5)
                    if len(_res) == 1:
                        # Match único: asignar directo
                        if _ck_ed:
                            st.session_state[_ck_ed] = _res[0]
                        _xl_enriched[_i_ed]["odoo_product"] = _res[0]
                        _tbl_updated = True
                    elif len(_res) > 1:
                        # Múltiples resultados: guardar para mostrar selectbox
                        st.session_state[f"_dis_{uf.name}_{_i_ed}"] = (
                            _new_q, _res, _ck_ed, _i_ed)
                    else:
                        _tbl_no_match.append(
                            f"⚠️ Sin resultados para «{_new_q}» (línea {_i_ed+1})")

                if _tbl_updated:
                    st.session_state[f"_tbl_ver_{uf.name}"] = _tbl_ver + 1
                    st.rerun()

                # ── Desambiguación: mostrar opciones cuando hay varios resultados ──
                for _i_ed in range(len(_xl_enriched)):
                    _dis_key = f"_dis_{uf.name}_{_i_ed}"
                    if _dis_key not in st.session_state:
                        continue
                    _dq, _dres, _dck, _di = st.session_state[_dis_key]
                    _opts_map = {
                        f"{r['name']}  [{r.get('default_code') or '—'}]": r
                        for r in _dres}
                    _dc1, _dc2 = st.columns([4, 1])
                    with _dc1:
                        _mod_lbl = (_xl_enriched[_di].get("modelo")
                                    or _xl_enriched[_di].get("descripcion")
                                    or f"Línea {_di+1}")
                        _dis_sel = st.selectbox(
                            f"**{_mod_lbl}** — elegí el producto:",
                            list(_opts_map.keys()),
                            key=f"dis_sel_{uf.name}_{_i_ed}")
                    with _dc2:
                        st.write("")
                        st.write("")
                        if st.button("✅ Usar", key=f"dis_use_{uf.name}_{_i_ed}"):
                            _chosen = _opts_map[_dis_sel]
                            if _dck:
                                st.session_state[_dck] = _chosen
                            _xl_enriched[_di]["odoo_product"] = _chosen
                            del st.session_state[_dis_key]
                            st.session_state[f"_tbl_ver_{uf.name}"] = (
                                st.session_state.get(f"_tbl_ver_{uf.name}", 0) + 1)
                            st.rerun()

                for _nm_msg in _tbl_no_match:
                    st.caption(_nm_msg)
            else:
                st.info("No se detectaron productos con cantidad pedida.")

            # ── Resumen financiero ─────────────────────────────────────────
            _xl_neto  = sum(safe_float(_el.get("subtotal",0)) for _el in _xl_enriched)
            _xl_iva   = sum(
                safe_float(_el.get("subtotal",0)) * safe_float(_el.get("iva_pct",21)) / 100
                for _el in _xl_enriched)
            _xl_total = _xl_neto + _xl_iva
            if _xl_enriched:
                st.markdown("##### 💰 Resumen financiero")
                def _fin_card_xl(lbl, val):
                    return (f'<div style="border-left:3px solid #e63946;padding:5px 10px;'
                            f'background:#fff8f8;border-radius:4px">'
                            f'<div style="font-size:11px;color:#888;margin-bottom:2px">{lbl}</div>'
                            f'<div style="font-size:15px;font-weight:700;color:#e63946">{fmt_ars(val)}</div></div>')
                _xlrf1, _xlrf2, _xlrf3 = st.columns(3)
                _xlrf1.markdown(_fin_card_xl("Neto s/IVA",  _xl_neto), unsafe_allow_html=True)
                _xlrf2.markdown(_fin_card_xl("IVA",         _xl_iva),  unsafe_allow_html=True)
                _xlrf3.markdown(_fin_card_xl("Total c/IVA", _xl_total),unsafe_allow_html=True)

            # ── Crear pedido ───────────────────────────────────────────────
            st.markdown("---")

            # Campo Referido / Ejecutivo de cuenta (igual que en ruta PDF)
            _xl_ejecutivo_field, _xl_ejecutivo_relation = get_ejecutivo_field(models_url, uid, api_key)
            _xl_referidos   = get_referidos(models_url, uid, api_key)
            _xl_ref_map     = {n: i for i, n in _xl_referidos}
            _xl_ref_opts    = ["— Sin referido —"] + list(_xl_ref_map.keys())
            _xl_ref_default = 0
            if _xl_pid and _xl_ref_map:
                try:
                    _xl_pdata = models.execute_kw(_cfg.ODOO_DB, uid, api_key,
                        "res.partner", "read", [[_xl_pid]],
                        {"fields": ["x_studio_referido_1"]})[0]
                    _xl_existing = _xl_pdata.get("x_studio_referido_1")
                    if _xl_existing and isinstance(_xl_existing, (list, tuple)):
                        _rxlname = _xl_existing[1]
                        if _rxlname in _xl_ref_opts:
                            _xl_ref_default = _xl_ref_opts.index(_rxlname)
                except Exception:
                    pass
            _xl_ref_sel = st.selectbox(
                "Referido", _xl_ref_opts, index=_xl_ref_default,
                key=f"xl_ref_{uf.name}",
                help="Quién refirió a este cliente",
            )

            _xl_btn_disabled = not bool(_xl_pid)
            if _xl_btn_disabled:
                st.caption("🔒 Identificá el cliente para habilitar la creación del pedido.")
            if st.button("⬆️ Crear pedido en Odoo", key=f"btn_xl_order_{uf.name}",
                         type="primary", disabled=_xl_btn_disabled):
                with st.spinner("Creando pedido..."):
                    try:
                        # Escribir referido al partner
                        if _xl_ref_sel != "— Sin referido —" and _xl_ref_sel in _xl_ref_map:
                            try:
                                models.execute_kw(_cfg.ODOO_DB, uid, api_key,
                                    "res.partner", "write",
                                    [[_xl_pid],
                                     {"x_studio_referido_1": _xl_ref_map[_xl_ref_sel]}])
                            except Exception:
                                pass
                        _xl_ref_partner_id = _xl_ref_map.get(_xl_ref_sel) if _xl_ref_sel != "— Sin referido —" else None
                        _xl_ref_id = _xl_ref_partner_id
                        if _xl_ref_partner_id and _xl_ejecutivo_relation == "res.users":
                            try:
                                _xl_usr = models.execute_kw(_cfg.ODOO_DB, uid, api_key,
                                    "res.users", "search_read",
                                    [[("partner_id", "=", _xl_ref_partner_id)]],
                                    {"fields": ["id"], "limit": 1})
                                if _xl_usr:
                                    _xl_ref_id = _xl_usr[0]["id"]
                            except Exception:
                                pass
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
                            ejecutivo_field = _xl_ejecutivo_field,
                            ejecutivo_id    = _xl_ref_id,
                        )
                        url = odoo_url("sale.order", _xl_order_id)
                        st.toast("Presupuesto creado en Odoo — pendiente de confirmación", icon="✅")
                        st.markdown(f"📎 [Revisar y confirmar en Odoo]({url})")
                        register_processed_file(file_bytes, uf.name, "Pedido cliente", f"ID {_xl_order_id}")
                        st.session_state.history.append({"tipo":"Pedido cliente",
                            "archivo":uf.name,"id":_xl_order_id,"url":url,"estado":"✅","hora":_dt_now.now().strftime("%H:%M")})
                    except Exception as _xe:
                        show_odoo_error(_xe, "crear pedido Excel")
        else:
            # ── Parseo del PDF de OC ──────────────────────────────────────
            oc_fields, _oc_tables, _oc_raw = {}, [], ""
            if ext == "pdf":
                with st.spinner("Leyendo OC..."):
                    oc_fields, _oc_tables, _oc_raw = extract_oc_fields(file_bytes)
            elif ext in ("jpg","jpeg","png"):
                st.image(file_bytes, caption="Vista previa", width=420)
                with st.spinner("Leyendo imagen con OCR..."):
                    oc_fields, _oc_tables, _oc_raw = extract_image_oc_fields(file_bytes)
                _n_lineas = len(oc_fields.get("lineas", []))
                if not _oc_raw:
                    st.error("❌ OCR falló — Tesseract puede no estar instalado aún. Intentá en 2 minutos o subí un PDF.")
                else:
                    if _n_lineas == 0:
                        show_odoo_warning("OCR leyó el texto pero no detectó líneas de productos.", "parsear OC por imagen")
                    else:
                        st.caption(f"🤖 OCR detectó {_n_lineas} línea(s). Revisá antes de confirmar.")
                    with st.expander("🔍 Ver texto bruto detectado por OCR"):
                        st.code(_oc_raw if _oc_raw else "(vacío)")

            # ── Session state para partner de esta OC ─────────────────────
            _ss_pid   = f"oc_pid_{uf.name}"
            _ss_pname = f"oc_pname_{uf.name}"
            if _ss_pid not in st.session_state:
                st.session_state[_ss_pid] = None
            if _ss_pname not in st.session_state:
                st.session_state[_ss_pname] = ""

            # ── SECCIÓN 1: CLIENTE ────────────────────────────────────────
            st.markdown("##### 🏢 Cliente")

            # CUIT editable en tiempo real (fuera del form), igual que en Facturas
            _oc_cuit_key = f"oc_cuit_edit_{uf.name}"
            _oc_cuit_detected = oc_fields.get("cuit", "")
            _oc_name_detected = oc_fields.get("cliente_nombre", "")
            if _oc_cuit_key not in st.session_state:
                # Pre-llenar con nombre si no hay CUIT
                st.session_state[_oc_cuit_key] = _oc_cuit_detected or _oc_name_detected
            _oc_cuit = st.text_input(
                "CUIT o razon social del cliente",
                key=_oc_cuit_key,
                placeholder="30-12345678-9  o  COPPEL S.A.",
                help="Detectado del documento. Podés buscar por CUIT o nombre.",
            ).strip()
            _oc_cuit_norm = _oc_cuit.replace("-","").replace(" ","")

            # Lookup por CUIT o nombre si todavía no tenemos partner resuelto
            if _oc_cuit and not st.session_state[_ss_pid]:
                # Intentar por CUIT primero
                _partner_oc = search_partner_by_cuit(models_url, uid, api_key, _oc_cuit) if len(_oc_cuit_norm) >= 10 else None
                if _partner_oc:
                    st.session_state[_ss_pid]   = _partner_oc[0]
                    st.session_state[_ss_pname] = _partner_oc[1]
                elif len(_oc_cuit) >= 3:
                    # Fallback: buscar por nombre
                    _oc_cands = search_partner_by_cuit_or_name(models_url, uid, api_key, _oc_cuit, limit=6)
                    if len(_oc_cands) == 1:
                        st.session_state[_ss_pid]   = _oc_cands[0]["id"]
                        st.session_state[_ss_pname] = _oc_cands[0]["name"]
                    elif len(_oc_cands) > 1:
                        _oc_opt_map = {f"{r['name']}  [{r.get('vat') or '—'}]": r for r in _oc_cands}
                        _occ1, _occ2 = st.columns([4, 1])
                        with _occ1:
                            _oc_sel_lbl = st.selectbox("Resultados", list(_oc_opt_map.keys()),
                                key=f"oc_sel_{uf.name}", label_visibility="collapsed")
                        with _occ2:
                            st.write("")
                            if st.button("Usar", key=f"oc_use_{uf.name}"):
                                _ch = _oc_opt_map[_oc_sel_lbl]
                                st.session_state[_ss_pid]   = _ch["id"]
                                st.session_state[_ss_pname] = _ch["name"]
                                st.rerun()

            _partner_id_oc   = st.session_state[_ss_pid]
            _partner_name_oc = st.session_state[_ss_pname]

            # Checkbox crear nuevo (solo aparece cuando CUIT ingresado y no encontrado)
            _oc_create_key = f"oc_create_new_{uf.name}"
            if _oc_create_key not in st.session_state:
                st.session_state[_oc_create_key] = False

            if _partner_id_oc:
                st.success(f"✅ Cliente: **{_partner_name_oc}** (CUIT {_oc_cuit})")
                _pt_id, _pt_name = get_customer_payment_terms(models_url, uid, api_key, _partner_id_oc)
            elif _oc_cuit:
                show_odoo_warning(f"CUIT {_oc_cuit} no encontrado en Odoo.", "buscar cliente por CUIT")
                st.checkbox("➕ Crear nuevo cliente en Odoo", key=_oc_create_key)
                _pt_id, _pt_name = None, None
            else:
                st.info("ℹ️ Ingresá el CUIT del cliente para buscarlo en Odoo.")
                _pt_id, _pt_name = None, None

            if st.session_state.get(_oc_create_key):
                with st.expander("📝 Datos del nuevo cliente", expanded=True):
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
                                st.session_state[_oc_create_key] = False
                                st.success(f"✅ Cliente **{nc_name}** creado en Odoo (ID {_new_pid})")
                                st.rerun()
                            except Exception as _e:
                                show_odoo_error(_e, "crear cliente")
                        else:
                            st.warning("Razón social y CUIT son obligatorios.")

            # ── SECCIÓN 2: DATOS DE LA OC ─────────────────────────────────
            # Forzar sesión con datos nuevos si antes estaba vacío
            for _oc_fk, _oc_fv in [
                (f"ocnum_{uf.name}",  oc_fields.get("numero_oc", "")),
                (f"ocfec_{uf.name}",  oc_fields.get("fecha_iso", "") or oc_fields.get("fecha", "")),
                (f"occond_{uf.name}", oc_fields.get("condiciones_pago", "")),
            ]:
                if _oc_fv and not st.session_state.get(_oc_fk):
                    st.session_state[_oc_fk] = _oc_fv
            st.markdown("##### 📋 Datos de la Orden de Compra")
            _oc1, _oc2, _oc3 = st.columns(3)
            _oc_num_i  = _oc1.text_input("N° OC",   value=oc_fields.get("numero_oc",""),
                                          key=f"ocnum_{uf.name}")
            _oc_fec_i  = _oc2.text_input("Fecha",   value=oc_fields.get("fecha_iso","") or oc_fields.get("fecha",""),
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
                # Build enriched list from current overrides/cache
                for _li, _ln in enumerate(_lineas_oc):
                    _cands_key = f"oc_cands_{uf.name}_{_li}"
                    if _cands_key not in st.session_state:
                        _cands = search_product_by_code_or_name(
                            models_url, uid, api_key,
                            code=_ln.get("codigo",""),
                            name_keywords=(
                                (_ln.get("descripcion","") + " " +
                                 _ln.get("marca","") + " " +
                                 _ln.get("modelo","")).strip()
                            ),
                            ean13=_ln.get("ean13",""),
                            limit=1,
                        )
                        st.session_state[_cands_key] = _cands
                    _cands = st.session_state.get(_cands_key) or []
                    _override = st.session_state[_ss_overrides].get(_li)
                    _op = _override if _override is not None else (_cands[0] if _cands else None)
                    _cost  = float(_op["standard_price"]) if _op else 0.0
                    _price = safe_float(_ln.get("precio_unit", 0))
                    _margin = ((_price - _cost) / _price * 100) if _price > 0 else 0.0
                    _enriched.append({**_ln, "odoo_product": _op, "cost": _cost, "margin_pct": _margin})

                # Table data
                _df_rows = []
                for _el in _enriched:
                    _desc_full = " · ".join(x for x in [
                        _el.get("descripcion",""), _el.get("marca",""), _el.get("modelo","")
                    ] if x)
                    _match_txt = (_el["odoo_product"]["name"] if _el.get("odoo_product") else "")
                    _df_rows.append({
                        "Código":       _el.get("codigo",""),
                        "Descripción":  _desc_full,
                        "Cant.":        int(_el.get("cantidad",0)),
                        "Precio unit.": fmt_ars(_el.get("precio_unit",0)),
                        "Subtotal":     fmt_ars(_el.get("subtotal",0)),
                        "Margen %":     f"{_el.get('margin_pct',0):.1f}%",
                        "Match Odoo":   _match_txt,
                    })
                _prev_matches = [r["Match Odoo"] for r in _df_rows]

                _edited_df = st.data_editor(
                    pd.DataFrame(_df_rows),
                    column_config={
                        "Código":       st.column_config.TextColumn(disabled=True, width="small"),
                        "Descripción":  st.column_config.TextColumn(disabled=True),
                        "Cant.":        st.column_config.NumberColumn(disabled=True, width="small"),
                        "Precio unit.": st.column_config.TextColumn(disabled=True, width="medium"),
                        "Subtotal":     st.column_config.TextColumn(disabled=True, width="medium"),
                        "Margen %":     st.column_config.TextColumn(disabled=True, width="small"),
                        "Match Odoo":   st.column_config.TextColumn(
                            "Match Odoo ✏️",
                            help="Escribí nombre o código Odoo para reasignar",
                            width="large",
                        ),
                    },
                    use_container_width=True,
                    hide_index=True,
                    key=f"oc_editor_{uf.name}",
                )

                # Detect Match Odoo changes → search & update overrides
                _needs_rerun = False
                for _idx, _erow in _edited_df.iterrows():
                    _new_txt = str(_erow.get("Match Odoo","") or "").strip()
                    _old_txt = _prev_matches[_idx] if _idx < len(_prev_matches) else ""
                    if _new_txt == _old_txt:
                        continue
                    if not _new_txt:
                        st.session_state[_ss_overrides][_idx] = None
                        st.session_state.pop(f"oc_cands_{uf.name}_{_idx}", None)
                        _needs_rerun = True
                    else:
                        _sr = search_product_by_code_or_name(
                            models_url, uid, api_key,
                            code=_new_txt, name_keywords=_new_txt, limit=6)
                        if len(_sr) == 1:
                            st.session_state[_ss_overrides][_idx] = _sr[0]
                            st.session_state[f"oc_cands_{uf.name}_{_idx}"] = _sr
                            _needs_rerun = True
                        elif len(_sr) > 1:
                            _res_opts = {
                                f"{r['name']}  [{r.get('default_code','')}]": r for r in _sr}
                            st.warning(f"Fila {_idx+1} — múltiples resultados para «{_new_txt}»:")
                            _chosen_lbl2 = st.selectbox(
                                "Seleccioná el producto correcto",
                                list(_res_opts.keys()),
                                key=f"oc_res_{uf.name}_{_idx}",
                                label_visibility="collapsed",
                            )
                            if st.button("✅ Confirmar", key=f"oc_res_btn_{uf.name}_{_idx}"):
                                st.session_state[_ss_overrides][_idx] = _res_opts[_chosen_lbl2]
                                st.session_state[f"oc_cands_{uf.name}_{_idx}"] = _sr
                                _needs_rerun = True
                        else:
                            st.warning(f"Fila {_idx+1}: sin resultados para «{_new_txt}» — intentá otro término.")
                if _needs_rerun:
                    st.rerun()
            else:
                st.info("No se detectaron líneas de productos automáticamente.")

            # Cálculo de totales desde líneas enriquecidas
            _calc_neto = sum(safe_float(_el.get("subtotal",0)) for _el in _enriched)
            _calc_iva21 = sum(
                safe_float(_el.get("subtotal",0)) * 0.21
                for _el in _enriched if abs(safe_float(_el.get("iva_pct",21)) - 21) < 1)
            _calc_iva105 = sum(
                safe_float(_el.get("subtotal",0)) * 0.105
                for _el in _enriched if abs(safe_float(_el.get("iva_pct",21)) - 10.5) < 1)
            _calc_iva   = _calc_iva21 + _calc_iva105
            _calc_total = _calc_neto + _calc_iva
            _calc_costo = sum(safe_float(_el.get("cost",0)) * safe_float(_el.get("cantidad",1))
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
            def _fin_card(lbl, val, pct=False):
                color = "#e63946"
                fval = f"{val:.1f}%" if pct else fmt_ars(val)
                return (f'<div style="border-left:3px solid {color};padding:5px 10px;'
                        f'background:#fff8f8;border-radius:4px">'
                        f'<div style="font-size:11px;color:#888;margin-bottom:2px">{lbl}</div>'
                        f'<div style="font-size:15px;font-weight:700;color:{color}">{fval}</div></div>')
            _rf1, _rf2, _rf3, _rf4 = st.columns(4)
            _rf1.markdown(_fin_card("Total Neto",  _show_neto), unsafe_allow_html=True)
            _rf2.markdown(_fin_card("IVA",         _show_iva),  unsafe_allow_html=True)
            _rf3.markdown(_fin_card("Total c/IVA", _show_total),unsafe_allow_html=True)
            _rf4.markdown(_fin_card("Margen total",_margin_total, pct=True), unsafe_allow_html=True)

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
                    show_odoo_warning(
                        f"Discrepancia: la OC indica {_oc_dias} días ({_oc_cond_str}), "
                        f"pero el cliente tiene {_pt_name} en Odoo.",
                        "verificar condición de pago"
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

            # Campo Referido / Ejecutivo de cuenta
            _oc_ejecutivo_field, _oc_ejecutivo_relation = get_ejecutivo_field(models_url, uid, api_key)
            _oc_referidos  = get_referidos(models_url, uid, api_key)
            _oc_ref_map    = {n: i for i, n in _oc_referidos}
            _oc_ref_opts   = ["— Sin referido —"] + list(_oc_ref_map.keys())
            _oc_ref_default = 0
            if _partner_id_oc and _oc_ref_map:
                try:
                    _oc_pdata = models.execute_kw(_cfg.ODOO_DB, uid, api_key,
                        "res.partner", "read", [[_partner_id_oc]],
                        {"fields": ["x_studio_referido_1"]})[0]
                    _oc_existing = _oc_pdata.get("x_studio_referido_1")
                    if _oc_existing and isinstance(_oc_existing, (list, tuple)):
                        _rname = _oc_existing[1]
                        if _rname in _oc_ref_opts:
                            _oc_ref_default = _oc_ref_opts.index(_rname)
                except Exception:
                    pass
            _oc_ref_sel = st.selectbox(
                "Referido", _oc_ref_opts, index=_oc_ref_default,
                key=f"oc_ref_{uf.name}",
                help="Quién refirió a este cliente",
            )

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
                        if _oc_ref_sel != "— Sin referido —" and _oc_ref_sel in _oc_ref_map:
                            try:
                                models.execute_kw(_cfg.ODOO_DB, uid, api_key,
                                    "res.partner", "write",
                                    [[_partner_id_oc],
                                     {"x_studio_referido_1": _oc_ref_map[_oc_ref_sel]}])
                            except Exception:
                                pass
                        _ref_partner_id = _oc_ref_map.get(_oc_ref_sel) if _oc_ref_sel != "— Sin referido —" else None
                        # Si el campo espera res.users, buscar el usuario cuyo partner coincide
                        _ref_id_oc = _ref_partner_id
                        if _ref_partner_id and _oc_ejecutivo_relation == "res.users":
                            try:
                                _usr = models.execute_kw(_cfg.ODOO_DB, uid, api_key,
                                    "res.users", "search_read",
                                    [[("partner_id", "=", _ref_partner_id)]],
                                    {"fields": ["id"], "limit": 1})
                                if _usr:
                                    _ref_id_oc = _usr[0]["id"]
                            except Exception:
                                pass
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
                            ejecutivo_field  = _oc_ejecutivo_field,
                            ejecutivo_id     = _ref_id_oc,
                        )
                        url = odoo_url("sale.order", order_id)
                        st.toast("Presupuesto creado en Odoo — pendiente de confirmación", icon="✅")
                        st.markdown(f"📎 [Revisar y confirmar en Odoo]({url})")
                        register_processed_file(file_bytes, uf.name, "Pedido cliente", f"ID {order_id}")
                        st.session_state.history.append({"tipo":"Pedido cliente",
                            "archivo":uf.name,"id":order_id,"url":url,"estado":"✅","hora":_dt_now.now().strftime("%H:%M")})
                    except Exception as _e:
                        show_odoo_error(_e, "crear pedido OC")


# ═══════════════════════════════════════════════════
# TAB 3 — IMPORTACIONES (ADMIN)
# ═══════════════════════════════════════════════════

    pass  # end render
