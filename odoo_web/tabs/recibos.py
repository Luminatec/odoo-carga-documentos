"""Tab Recibos de Cobro."""
import streamlit as st
import re
from io import BytesIO
import pandas as pd
import config as _cfg
from user_prefs import load_prefs as _load_prefs
from odoo_client import (
    check_duplicate_cheque,
    get_all_accounts,
    normalize_amount,
    fmt_ars,
    get_all_banks,
    match_bank_id,
    search_partners_by_cuits,
    get_customer_unpaid_invoices,
    get_customer_pending_credit_notes,
    register_customer_payment,
    show_odoo_warning,
    check_duplicate_file,
    register_processed_file,
    search_registered_payments,
    get_payment_journals,
)


def render(models, uid, api_key, models_url, is_admin):
    from datetime import date as _rc_date_cls

    st.subheader("💰 Recibos de Cobro")

    # ── helpers locales ─────────────────────────────────────────────────────
    def _rc_parse_monto(val):
        """Convierte importe de home banking a float.
        Soporta formato AR (1.234,56), US/Excel (1234.56) y sin centavos (1234)."""
        s = str(val).replace("$", "").replace("\xa0", "").replace(" ", "").strip()
        if not s or s.lower() in ("nan", "none", ""):
            return 0.0
        try:
            return float(normalize_amount(s))
        except Exception:
            return 0.0

    def _rc_parse_retencion(file_bytes):
        """Parsea PDF de retención. Soporta formato AFIP estándar y formato SAP multi-página
        (Megatone y similares: dos columnas, cert. por página).
        Retorna LISTA de dicts [{cuit, nombre, importe, fecha, concepto, provincia, ...}]."""
        try:
            import pdfplumber
            results = []
            with pdfplumber.open(BytesIO(file_bytes)) as _pdf:
                for _page in _pdf.pages:
                    _txt = _page.extract_text() or ""
                    if not _txt.strip():
                        continue
                    _r = {}
                    _lines = [l.strip() for l in _txt.splitlines() if l.strip()]

                    # ── CUIT emisor ─────────────────────────────────────────
                    # SAP: primer C.U.I.T. en el texto es el del cliente
                    # AFIP: igual
                    _m = re.search(r"C\.U\.I\.T\.:\s*([\d\-\s]+)", _txt)
                    if _m:
                        _r["cuit"] = re.sub(r"[^\d]", "", _m.group(1))[:11]

                    # ── Nombre emisor ───────────────────────────────────────
                    # SAP: primera línea tiene "Empresa S.R.L Página: N" → quitar " Página: N"
                    # AFIP: primera línea es directamente el nombre
                    _raw_nombre = _lines[0] if _lines else ""
                    _r["nombre"] = re.sub(r'\s*P[áa]gina:\s*\d+.*$', '', _raw_nombre).strip()

                    # ── Fecha (DD/MM/YYYY o DD.MM.YYYY) ────────────────────
                    _m = re.search(r"Fecha:\s*(\d{2})[./](\d{2})[./](\d{4})", _txt)
                    if _m:
                        _r["fecha"]     = f"{_m.group(1)}/{_m.group(2)}/{_m.group(3)}"
                        _r["fecha_iso"] = f"{_m.group(3)}-{_m.group(2)}-{_m.group(1)}"

                    # ── Nro certificado ─────────────────────────────────────
                    # AFIP: "Nro. de certificado: XXXX"
                    _m = re.search(r"Nro\.\s*de\s*certificado:\s*(.+)", _txt)
                    if _m:
                        _r["nro_certificado"] = _m.group(1).strip()
                    else:
                        # SAP: patrón NNNN-NNNNNNNN en el bloque del header
                        _m = re.search(r"\b(\d{4}-\d{8})\b", _txt)
                        if _m:
                            _r["nro_certificado"] = _m.group(1)

                    # ── Concepto / tipo de retención ────────────────────────
                    # AFIP: "Concepto del pago: ..."
                    _m = re.search(r"Concepto del pago:\s*(.+)", _txt)
                    if _m:
                        _tipo = _m.group(1).strip()
                    else:
                        # SAP: "Este certificado de retención de TIPO es para..."
                        # Más confiable que parsear el bloque de dos columnas
                        _m = re.search(
                            r"Este certificado de retenci[oó]n de (.+?) es para", _txt)
                        _tipo = _m.group(1).strip() if _m else ""

                    # Provincia (SAP): "Provincia: NN Nombre Provincia"
                    _mp = re.search(r"Provincia:\s*\d+\s*(.+?)(?=\s*\n|\s*C\.U\.I\.T\.|$)",
                                    _txt, re.MULTILINE)
                    _prov = _mp.group(1).strip() if _mp else ""
                    _r["provincia"] = _prov

                    # Concepto final: tipo + provincia si aplica
                    _r["concepto"] = f"{_tipo} {_prov}".strip() if _prov else _tipo

                    # ── Importe retenido ────────────────────────────────────
                    # AFIP: "Importe retenido: X.XXX,XX"
                    _m = re.search(r"Importe retenido:\s*([\d.,]+)", _txt)
                    if _m:
                        _r["importe"] = _rc_parse_monto(_m.group(1))
                    else:
                        # SAP: último número antes de "ARS" (línea TOTAL del resumen)
                        # Ej: "12.381.348,45 123.813,48 ARS"
                        _ars_nums = re.findall(r"([\d\.]+,\d{2})\s+ARS", _txt)
                        if _ars_nums:
                            _r["importe"] = _rc_parse_monto(_ars_nums[-1])

                    # ── Importe sujeto (opcional) ───────────────────────────
                    _m = re.search(
                        r"Importe pagado sujeto a retenci[oó]n:\s*([\d.,]+)", _txt)
                    if _m:
                        _r["importe_sujeto"] = _rc_parse_monto(_m.group(1))

                    # ── Formato "Constancia Retención CM" (Trivisonno y similares) ──
                    # pdfplumber fusiona las dos columnas: nombre aparece en la primera línea
                    # junto con "CONSTANCIA DE RETENCION". El CUIT del agente NO está en el PDF.
                    if (not _r.get("importe")
                            and re.search(r"CONSTANCIA\s+DE\s+RETENCI[OÓ]N", _txt, re.IGNORECASE)
                            and re.search(r"ING\.?\s*BRUTOS?", _txt, re.IGNORECASE)):
                        # Nombre: primera línea "EMPRESA SRL  CONSTANCIA DE RETENCION"
                        _m_nom = re.match(r"^(.+?)\s{2,}CONSTANCIA", _lines[0]) if _lines else None
                        if _m_nom:
                            _r["nombre"] = _m_nom.group(1).strip()
                        elif not _r.get("nombre") and _lines:
                            _r["nombre"] = _lines[0].strip()
                        # Fecha: DD-MM-YYYY en header ("...CM 921-759430 12-06-2026 16:43")
                        _mf = re.search(r"\b(\d{2})-(\d{2})-(\d{4})\b", _txt)
                        if _mf and not _r.get("fecha"):
                            _r["fecha"]     = f"{_mf.group(1)}/{_mf.group(2)}/{_mf.group(3)}"
                            _r["fecha_iso"] = f"{_mf.group(3)}-{_mf.group(2)}-{_mf.group(1)}"
                        # Nro certificado: "ORIGINAL NNNNNNNN"
                        _mnro = re.search(r"ORIGINAL\s+(\d{6,10})", _txt, re.IGNORECASE)
                        if _mnro:
                            _r["nro_certificado"] = _mnro.group(1)
                        # Importe: última columna de la fila REMESA
                        # Ej: "12/06/26  REMESA: 001RPX001700008706  3266797.81  0.80  22245.05"
                        _mimp = re.search(
                            r"REMESA:\s*\S+\s+([\d.,]+)\s+([\d.,]+)\s+([\d.,]+)",
                            _txt, re.IGNORECASE)
                        if _mimp:
                            _r["importe"] = _rc_parse_monto(_mimp.group(3))
                        # CUIT del agente de retención: no figura en este formato
                        if not _r.get("cuit"):
                            _r["cuit"] = ""
                        _r.setdefault("concepto", "IIBB")
                        _r.setdefault("provincia", "")

                    if _r.get("importe"):
                        results.append(_r)
            return results
        except Exception:
            return []

    def _rc_parse_cheques(file_bytes, filename):
        fname = filename.lower()
        if fname.endswith(".csv"):
            try:
                text = file_bytes.decode("latin-1")
            except Exception:
                text = file_bytes.decode("utf-8", errors="replace")
            lines = [l for l in text.splitlines() if l.strip()]
            if len(lines) < 3:
                return []
            rows = []
            for line in lines[2:]:
                parts = [p.strip() for p in line.split(";")]
                while len(parts) < 40:
                    parts.append("")
                estado = parts[7].lower()
                # Col D (idx 3) = CUIT del que nos lo entrego (directo)
                # Col N (idx 13) = CUIT del beneficiario original (endosado)
                if "endoso" in estado:
                    cuit   = parts[13]
                    nombre = parts[12]
                else:
                    cuit   = parts[3]
                    nombre = parts[2]
                cuit = str(cuit).replace("-", "").replace(" ", "").strip()
                if not cuit or len(cuit) < 8:
                    continue
                rows.append({
                    "nro":       parts[0],
                    "nombre":    nombre.strip(),
                    "cuit":      cuit,
                    "fecha_pago": parts[4],
                    "importe":   _rc_parse_monto(parts[6]),
                    "estado":    parts[7],
                    "banco":     parts[8],
                })
            return rows
        elif fname.endswith((".xlsx", ".xls")):
            try:
                _dfx = pd.read_excel(BytesIO(file_bytes), header=1, dtype=str).fillna("")
            except Exception as _xe:
                st.error(f"No se pudo leer el Excel: {_xe}")
                return []
            rows = []
            for _, _xr in _dfx.iterrows():
                parts = list(_xr.values)
                while len(parts) < 40:
                    parts.append("")
                estado = str(parts[7]).lower()
                if "endoso" in estado:
                    cuit   = str(parts[13])
                    nombre = str(parts[12])
                else:
                    cuit   = str(parts[3])
                    nombre = str(parts[2])
                cuit = cuit.replace("-", "").replace(" ", "").strip()
                if not cuit or len(cuit) < 8:
                    continue
                # Normalizar fecha: el xlsx trae '2026-06-19 00:00:12' → tomar solo YYYY-MM-DD
                _fp_raw = str(parts[4]).strip()
                _fp = _fp_raw[:10] if len(_fp_raw) >= 10 else _fp_raw
                rows.append({
                    "nro":       str(parts[0]),
                    "nombre":    nombre.strip(),
                    "cuit":      cuit,
                    "fecha_pago": _fp,
                    "importe":   _rc_parse_monto(str(parts[6])),
                    "estado":    str(parts[7]),
                    "banco":     str(parts[8]).strip(),
                })
            return rows
        else:
            st.error("Formato no soportado. Subí un archivo CSV o XLSX del home banking.")
            return []

    # ── Diario de cobros (configurable desde Preferencias) ───────────────────────
    _prefs_rc = _load_prefs()
    _pref_jour = _prefs_rc.get("diario_cobros_nombre", "")
    _all_rc_journals = get_payment_journals(models_url, uid, api_key)
    _RC_JOURNAL_ID = next(
        (jid for jid, jname, *_ in _all_rc_journals if jname == _pref_jour),
        73,  # fallback si no hay preferencia configurada
    ) if _pref_jour else 73
    _rc_all_banks  = get_all_banks(models_url, uid, api_key)

    # ── Uploaders ────────────────────────────────────────────────────────────
    _rcu_col1, _rcu_col2 = st.columns([3, 2])
    _rc_file = _rcu_col1.file_uploader(
        "📄 Cheques (CSV o XLSX del home banking)",
        type=["csv", "xlsx", "xls"], key="rc_file_uploader",
        help="Descargalo desde tu home banking y subilo sin modificar.")
    _rc_ret_files = _rcu_col2.file_uploader(
        "🔖 Retenciones (PDF)",
        type=["pdf"], key="rc_ret_uploader",
        accept_multiple_files=True,
        help="Subí uno o más comprobantes de retención de IIBB o Ganancias.")

    # ── Parsear retenciones subidas ────────────────────────────────────────
    # Guardamos en session_state para que no se pierdan al reruns
    if "rc_retenciones" not in st.session_state:
        st.session_state["rc_retenciones"] = {}   # {cuit: [ret_dict, ...]}
    if _rc_ret_files:
        for _rf in _rc_ret_files:
            _rb = _rf.read()
            _rets = _rc_parse_retencion(_rb)
            if _rets:
                for _ret in _rets:
                    _rcuit_ret = _ret["cuit"]
                    _ret["_filename"] = _rf.name
                    _existing = st.session_state["rc_retenciones"].get(_rcuit_ret, [])
                    # Evitar duplicados: mismo concepto + mismo importe (independiente del archivo)
                    _dup_key = f"{_ret.get('concepto','')}|{_ret.get('importe',0)}"
                    if not any(
                        f"{r.get('concepto','')}|{r.get('importe',0)}" == _dup_key
                        for r in _existing
                    ):
                        _existing.append(_ret)
                        st.session_state["rc_retenciones"][_rcuit_ret] = _existing
            else:
                show_odoo_warning(f"No se pudo parsear la retención en {_rf.name}.", "parsear retención")

    # ── Retenciones sin CUIT — asignación manual ────────────────────────────
    _rets_no_cuit = st.session_state["rc_retenciones"].get("", [])
    if _rets_no_cuit:
        st.warning(
            "⚠️ Las siguientes retenciones no tienen CUIT del agente. "
            "Ingresá el CUIT del cliente para asociarlas al recibo correspondiente:")
        _reassign_map = {}
        for _nci, _nc in enumerate(_rets_no_cuit):
            _c1, _c2 = st.columns([3, 2])
            _nc_label = (
                f"{_nc.get('nombre','')} — "
                f"ARS {fmt_ars(_nc.get('importe',0))} — "
                f"Cert. {_nc.get('nro_certificado','')} — "
                f"{_nc.get('fecha','')} — {_nc.get('concepto','IIBB')}"
            )
            _c1.markdown(f"**{_nc_label}**")
            _inp = _c2.text_input(
                "CUIT cliente", key=f"rc_assign_cuit_{_nci}",
                placeholder="30-12345678-9", label_visibility="collapsed")
            _cuit_digits = re.sub(r"[^\d]", "", _inp)
            if len(_cuit_digits) == 11:
                _reassign_map[_nci] = _cuit_digits
        if _reassign_map:
            _remaining_nc = []
            for _nci, _nc in enumerate(_rets_no_cuit):
                if _nci in _reassign_map:
                    _cuit_r = _reassign_map[_nci]
                    _bucket = st.session_state["rc_retenciones"].setdefault(_cuit_r, [])
                    _dup_kr = f"{_nc.get('concepto','')}|{_nc.get('importe',0)}"
                    if not any(
                        f"{r.get('concepto','')}|{r.get('importe',0)}" == _dup_kr
                        for r in _bucket
                    ):
                        _bucket.append({**_nc, "cuit": _cuit_r})
                else:
                    _remaining_nc.append(_nc)
            st.session_state["rc_retenciones"][""] = _remaining_nc
            st.rerun()

    # Mostrar retenciones cargadas
    if st.session_state["rc_retenciones"]:
        _all_rets = [r for rl in st.session_state["rc_retenciones"].values() for r in rl]
        st.caption(
            f"✅ {len(_all_rets)} retención(es) cargada(s): "
            + ", ".join(f"{r['nombre']} — ARS {fmt_ars(r['importe'])}" for r in _all_rets)
            + "  ·  [Limpiar]" if _all_rets else "")
        if st.button("🗑️ Limpiar retenciones cargadas", key="rc_clear_rets"):
            st.session_state["rc_retenciones"] = {}
            st.rerun()

    if _rc_file is None:
        st.caption(
            "Subí el archivo del home banking para procesar los cheques recibidos "
            "y generar los recibos de cobro en Odoo.")
    else:
        _rc_bytes   = _rc_file.read()
        # ── Detección de duplicados ───────────────────────────────────
        _is_dup_rc, _dup_entry_rc = check_duplicate_file(_rc_bytes, _rc_file.name)
        if _is_dup_rc:
            st.warning(
                f"⚠️ **{_rc_file.name}** ya fue procesado en esta sesión "
                f"a las {_dup_entry_rc.get('hora','?')} "
                f"({_dup_entry_rc.get('resultado','')}). "
                "Podés continuar igual si es un reenvío o corrección.")
        _rc_cheques = _rc_parse_cheques(_rc_bytes, _rc_file.name)

        if not _rc_cheques:
            st.error(
                "No se encontraron cheques en el archivo. "
                "Verificá que sea el export correcto del home banking.")
        else:
            # Group by CUIT, preserving order of first appearance
            _rc_groups = {}
            for _ch in _rc_cheques:
                _rc_groups.setdefault(_ch["cuit"], []).append(_ch)

            _rc_total_ars = sum(c["importe"] for c in _rc_cheques)
            st.info(
                f"**{len(_rc_cheques)} cheque(s)** de **{len(_rc_groups)} cliente(s)** "
                f"— Total ARS {fmt_ars(_rc_total_ars)}")

            # ── Cargar datos de Odoo para todos los CUITs a la vez ──────────
            _rc_all_cuits = tuple(sorted(_rc_groups.keys()))
            with st.spinner("Buscando clientes en Odoo..."):
                _rc_partner_map = search_partners_by_cuits(
                    models_url, uid, api_key, _rc_all_cuits)

            _rc_pids_found = tuple(pid for pid, _ in _rc_partner_map.values())
            if _rc_pids_found:
                with st.spinner("Cargando facturas y notas de crédito pendientes..."):
                    _rc_all_inv = get_customer_unpaid_invoices(
                        models_url, uid, api_key, _rc_pids_found)
                    _rc_all_nc = get_customer_pending_credit_notes(
                        models_url, uid, api_key, _rc_pids_found)
            else:
                _rc_all_inv = []
                _rc_all_nc  = []

            # Construir mapa inverso: id_hijo → id_padre (para agrupar facturas de contactos)
            # Una factura puede estar a nombre de un contacto (hijo) del socio principal
            _rc_child_to_parent = {}
            if _rc_pids_found:
                try:
                    _all_contacts = models.execute_kw(
                        _cfg.ODOO_DB, uid, api_key, "res.partner", "search_read",
                        [[("parent_id", "in", list(_rc_pids_found))]],
                        {"fields": ["id", "parent_id"], "limit": 500})
                    for _ct in _all_contacts:
                        _parent = (_ct.get("parent_id") or [0])[0]
                        if _parent:
                            _rc_child_to_parent[_ct["id"]] = _parent
                except Exception:
                    pass

            # Index invoices by partner_id (normalizando hijos al padre)
            _rc_inv_by_pid = {}
            for _rci in _rc_all_inv:
                _rpid = (_rci.get("partner_id") or [0])[0]
                _rpid_norm = _rc_child_to_parent.get(_rpid, _rpid)
                _rc_inv_by_pid.setdefault(_rpid_norm, []).append(_rci)

            # Index notas de crédito by partner_id (mismo criterio)
            _rc_nc_by_pid = {}
            for _rnc in _rc_all_nc:
                _rpid = (_rnc.get("partner_id") or [0])[0]
                _rpid_norm = _rc_child_to_parent.get(_rpid, _rpid)
                _rc_nc_by_pid.setdefault(_rpid_norm, []).append(_rnc)

            if st.button("🔄 Actualizar desde Odoo", key="rc_refresh"):
                search_partners_by_cuits.clear()
                get_customer_unpaid_invoices.clear()
                get_customer_pending_credit_notes.clear()
                st.rerun()

            st.divider()

            # ── Un expander por cliente ──────────────────────────────────────
            for _rcuit, _rchs in _rc_groups.items():
                _rcp_data  = _rc_partner_map.get(_rcuit)
                _rctotal   = sum(c["importe"] for c in _rchs)
                _rc_tag    = "" if _rcp_data else "  ⚠️ no encontrado en Odoo"
                _rc_exp_lbl = (
                    f"👤 {_rchs[0]['nombre']} — CUIT {_rcuit} — "
                    f"{len(_rchs)} cheque(s) · ARS {fmt_ars(_rctotal)}{_rc_tag}")

                with st.expander(_rc_exp_lbl, expanded=(len(_rc_groups) == 1)):

                    # Tabla de cheques
                    _rch_rows = [
                        {"Nro": c["nro"], "Banco": c["banco"],
                         "Fecha cobro": c["fecha_pago"],
                         "Estado": c["estado"],
                         "Importe ARS": fmt_ars(c["importe"])}
                        for c in _rchs
                    ]
                    st.dataframe(
                        pd.DataFrame(_rch_rows),
                        use_container_width=True, hide_index=True)

                    if not _rcp_data:
                        show_odoo_warning(
                            f"CUIT {_rcuit} no encontrado en Odoo. "
                            "Verificá que el cliente esté registrado con ese CUIT en el campo VAT.",
                            "buscar cliente por CUIT")
                        continue

                    _rc_pid_cuit, _rc_pname_cuit = _rcp_data

                    # ── Detectar mismatch de nombre ───────────────────────────
                    _rc_nombre_excel = _rchs[0].get("nombre", "").strip().upper()
                    _rc_nombre_odoo  = _rc_pname_cuit.strip().upper()
                    # Limpiar puntuación de las keywords (ej: "LAURET," → "LAURET")
                    _rc_kws = [
                        re.sub(r"[^\w]", "", w)
                        for w in _rc_nombre_excel.split()
                        if len(re.sub(r"[^\w]", "", w)) >= 5
                    ]
                    _rc_match_ok = not _rc_kws or any(
                        kw in _rc_nombre_odoo for kw in _rc_kws)

                    if not _rc_match_ok:
                        # Hay mismatch: dejar elegir entre el partner del CUIT y buscar por nombre
                        st.warning(
                            f"⚠️ El CUIT **{_rcuit}** está asignado en Odoo a "
                            f"**{_rc_pname_cuit}** (ID {_rc_pid_cuit}), "
                            f"pero el cheque fue emitido por **{_rchs[0].get('nombre','')}**.")

                        _rc_sel_key  = f"rc_who_{_rcuit}"
                        _rc_srch_key = f"rc_srch_{_rcuit}"
                        _rc_choice = st.radio(
                            "¿A qué cliente asignar este cobro?",
                            options=["cuit", "nombre"],
                            format_func=lambda x: (
                                f"Usar {_rc_pname_cuit} (CUIT coincide en Odoo)"
                                if x == "cuit"
                                else f"Buscar {_rchs[0].get('nombre','')} por nombre"
                            ),
                            key=_rc_sel_key, horizontal=True)

                        if _rc_choice == "cuit":
                            _rc_pid   = _rc_pid_cuit
                            _rc_pname = _rc_pname_cuit
                        else:
                            # Buscar por nombre en Odoo
                            _rc_name_q = st.text_input(
                                "Nombre a buscar en Odoo",
                                value=_rchs[0].get("nombre", ""),
                                key=_rc_srch_key)
                            _rc_name_results = []
                            if _rc_name_q and len(_rc_name_q) >= 3:
                                try:
                                    _rc_name_results = models.execute_kw(
                                        _cfg.ODOO_DB, uid, api_key, "res.partner", "search_read",
                                        [[("name", "ilike", _rc_name_q),
                                          ("customer_rank", ">", 0),
                                          ("active", "=", True)]],
                                        {"fields": ["id", "name", "vat"], "limit": 10})
                                except Exception:
                                    pass
                            if not _rc_name_results:
                                st.info("Ingresá al menos 3 caracteres para buscar.")
                                continue
                            _rc_name_opts = {
                                f"{r['name']} (CUIT {r.get('vat','?')})": (r["id"], r["name"])
                                for r in _rc_name_results}
                            _rc_name_sel = st.selectbox(
                                "Seleccioná el cliente correcto",
                                list(_rc_name_opts.keys()),
                                key=f"rc_namesel_{_rcuit}")
                            _rc_pid, _rc_pname = _rc_name_opts[_rc_name_sel]
                    else:
                        _rc_pid   = _rc_pid_cuit
                        _rc_pname = _rc_pname_cuit
                        st.markdown(f"**Cliente Odoo:** {_rc_pname} (ID {_rc_pid})")

                    _rc_invs = _rc_inv_by_pid.get(_rc_pid, [])

                    # ── Selector de facturas ─────────────────────────────────
                    _rcsel_ids  = []
                    _rcsel_saldo = 0.0
                    if not _rc_invs:
                        st.info(
                            "No hay facturas pendientes para este cliente. "
                            "El cobro se registrará como pago a cuenta.")
                    else:
                        st.markdown("**Seleccioná las facturas a cobrar:**")
                        _rci_rows = []
                        for _rci in _rc_invs:
                            _rcic  = (_rci.get("currency_id") or [0, "ARS"])[1]
                            _rcres = float(_rci.get("amount_residual") or 0)
                            _rcto  = float(_rci.get("amount_total") or 0)
                            _vence_raw = str(_rci.get("invoice_date_due") or "")
                            _today_str = str(_rc_date_cls.today())
                            _vence_disp = (f"⚠️ {_vence_raw}" if _vence_raw and _vence_raw < _today_str else _vence_raw)
                            _rci_rows.append({
                                "Sel":        False,
                                "Factura":    _rci.get("name") or f"ID {_rci['id']}",
                                "Fecha":      str(_rci.get("invoice_date") or ""),
                                "Vence":      _vence_disp,
                                "Moneda":     _rcic,
                                "Total":      fmt_ars(_rcto)  if _rcic == "ARS" else f"{_rcic} {_rcto:,.2f}",
                                "Saldo":      fmt_ars(_rcres) if _rcic == "ARS" else f"{_rcic} {_rcres:,.2f}",
                                "_saldo_num": _rcres,   # columna numérica oculta para cálculos
                                "_id":        _rci["id"],
                            })
                        _rci_df  = pd.DataFrame(_rci_rows)
                        _rci_cfg = {
                            "Sel":        st.column_config.CheckboxColumn("✓", width="small"),
                            "Total":      st.column_config.TextColumn("Total"),
                            "Saldo":      st.column_config.TextColumn("Saldo"),
                            "_saldo_num": None,   # oculta
                            "_id":        None,
                        }
                        _rci_disp = ["Sel", "Factura", "Fecha", "Vence",
                                     "Moneda", "Total", "Saldo"]
                        _rci_edited = st.data_editor(
                            _rci_df[_rci_disp + ["_saldo_num", "_id"]],
                            column_config=_rci_cfg,
                            column_order=_rci_disp,
                            use_container_width=True, hide_index=True,
                            key=f"rc_inv_{_rcuit}",
                            disabled=[c for c in _rci_disp if c != "Sel"],
                        )
                        _rcsel       = _rci_edited[_rci_edited["Sel"] == True]
                        _rcsel_ids   = [int(r) for r in _rcsel["_id"].tolist()]
                        _rcsel_saldo = float(_rcsel["_saldo_num"].sum())

                    # ── Selector de notas de crédito pendientes ──────────────
                    _rc_ncs        = _rc_nc_by_pid.get(_rc_pid, [])
                    _rcncsel_ids   = []
                    _rcncsel_total = 0.0
                    if not _rc_ncs:
                        st.caption("📋 Sin notas de crédito pendientes para este cliente.")
                    if _rc_ncs:
                        st.markdown("**Notas de crédito pendientes de aplicar:**")
                        _rnc_rows = []
                        for _rnc in _rc_ncs:
                            _rncic  = (_rnc.get("currency_id") or [0, "ARS"])[1]
                            _rncres = float(_rnc.get("amount_residual") or 0)
                            _rncto  = float(_rnc.get("amount_total") or 0)
                            _rnc_rows.append({
                                "Sel":        False,
                                "Nota de Crédito": _rnc.get("name") or f"ID {_rnc['id']}",
                                "Fecha":      str(_rnc.get("invoice_date") or ""),
                                "Moneda":     _rncic,
                                "Total NC":   fmt_ars(_rncto)  if _rncic == "ARS" else f"{_rncic} {_rncto:,.2f}",
                                "Saldo NC":   fmt_ars(_rncres) if _rncic == "ARS" else f"{_rncic} {_rncres:,.2f}",
                                "_saldo_num": _rncres,
                                "_id":        _rnc["id"],
                            })
                        _rnc_df  = pd.DataFrame(_rnc_rows)
                        _rnc_cfg = {
                            "Sel":        st.column_config.CheckboxColumn("✓", width="small"),
                            "Total NC":   st.column_config.TextColumn("Total NC"),
                            "Saldo NC":   st.column_config.TextColumn("Saldo NC"),
                            "_saldo_num": None,
                            "_id":        None,
                        }
                        _rnc_disp = ["Sel", "Nota de Crédito", "Fecha", "Moneda",
                                     "Total NC", "Saldo NC"]
                        _rnc_edited = st.data_editor(
                            _rnc_df[_rnc_disp + ["_saldo_num", "_id"]],
                            column_config=_rnc_cfg,
                            column_order=_rnc_disp,
                            use_container_width=True, hide_index=True,
                            key=f"rc_nc_{_rcuit}",
                            disabled=[c for c in _rnc_disp if c != "Sel"],
                        )
                        _rncsel        = _rnc_edited[_rnc_edited["Sel"] == True]
                        _rcncsel_ids   = [int(r) for r in _rncsel["_id"].tolist()]
                        _rcncsel_total = float(_rncsel["_saldo_num"].sum())
                        if _rcncsel_total > 0:
                            st.caption(
                                f"NC seleccionadas: ARS {fmt_ars(_rcncsel_total)} "
                                "— se aplicarán contra las facturas en el recibo.")

                    # ── Formulario de pago ────────────────────────────────────
                    st.markdown("#### Datos del recibo")
                    _rc_jour_id = _RC_JOURNAL_ID
                    _rcc2, _rcc3 = st.columns([1, 1])
                    _rc_date = _rcc2.date_input(
                        "Fecha de cobro",
                        value=_rc_date_cls.today(),
                        key=f"rc_date_{_rcuit}")
                    _rc_amount = _rcc3.number_input(
                        "Importe cobrado (ARS)",
                        min_value=0.0,
                        value=float(_rctotal),
                        step=0.01, format="%.2f",
                        key=f"rc_amt_{_rcuit}",
                        help="Pre-completado con el total de cheques. "
                             "Ajustá si hay retenciones o NC.")

                    # ── Deducciones dinámicas (Retenciones / NC) ────────────
                    _ded_key     = f"rc_deds_{_rcuit}"
                    _ded_cnt_key = f"rc_deds_cnt_{_rcuit}"
                    if _ded_key     not in st.session_state: st.session_state[_ded_key]     = []
                    if _ded_cnt_key not in st.session_state: st.session_state[_ded_cnt_key] = 0

                    # ── Auto-agregar retenciones del mismo cliente ─────────
                    _cuit_rets = st.session_state.get("rc_retenciones", {}).get(_rcuit, [])
                    for _cret in _cuit_rets:
                        _cret_fname = _cret.get("_filename", "")
                        _cret_concepto_key = _cret.get("concepto", "")
                        # Deduplicar por concepto + importe (evita duplicar aunque venga de otro archivo)
                        _already = any(
                            d.get("_ret_concepto_pdf") == _cret_concepto_key and
                            abs(d.get("monto", 0) - _cret.get("importe", 0)) < 0.01
                            for d in st.session_state[_ded_key])
                        if not _already and _cret.get("importe", 0) > 0:
                            _new_uid = st.session_state[_ded_cnt_key] + 1
                            st.session_state[_ded_cnt_key] = _new_uid
                            _cret_concepto_pdf = _cret.get("concepto", "")
                            # Construir lista de cuentas igual que el widget para calcular índice
                            _rc_accts_pre = get_all_accounts(models_url, uid, api_key)
                            _rc_accts_pre_s = sorted(
                                _rc_accts_pre,
                                key=lambda x: (1 if "(copia)" in x[1].lower() else 0, x[1]))
                            _rc_acct_opts_pre = ["— Seleccionar concepto —"] + [lbl for _, lbl in _rc_accts_pre_s]
                            # Buscar cuenta por "ingresos brutos" / "iibb" / "retenc"
                            _ret_acct_id  = None
                            _ret_acct_lbl = ""
                            _ret_acct_idx = 0
                            _concepto_low = _cret_concepto_pdf.lower()
                            # Mapeo de variantes de provincia (concepto PDF → palabras clave en cuenta Odoo)
                            _prov_map = [
                                (["buenos aires", "bsas", "pba", "bs as", "bs.as"],
                                 ["buenos aires", "bsas", "pba", "bs as"]),
                                (["santa fe", "santa fé", "stfe"],
                                 ["santa fe", "santa fé", "stfe"]),
                                (["cordoba", "córdoba", "cba"],
                                 ["cordoba", "córdoba", "cba"]),
                                (["caba", "ciudad autón", "ciudad auton", "c.a.b.a"],
                                 ["caba", "ciudad"]),
                                (["mendoza", "mdz"], ["mendoza", "mdz"]),
                                (["tucuman", "tucumán"], ["tucuman", "tucumán"]),
                                (["entre rios", "entre ríos"], ["entre rios", "entre ríos"]),
                                (["salta"], ["salta"]),
                                (["misiones"], ["misiones"]),
                                (["chaco"], ["chaco"]),
                                (["neuquen", "neuquén"], ["neuquen", "neuquén"]),
                                (["rio negro", "río negro"], ["rio negro", "río negro"]),
                            ]
                            _is_iibb = "iibb" in _concepto_low or "ingresos brutos" in _concepto_low
                            _is_patronal = any(k in _concepto_low for k in
                                               ["cont.pat", "patronal", "contribucion", "contribución",
                                                "suss", "rg1784"])
                            _is_ganancias = "ganancia" in _concepto_low
                            # Detectar provincia del concepto PDF
                            _prov_acct_kws = []
                            for _prov_in, _prov_out in _prov_map:
                                if any(pk in _concepto_low for pk in _prov_in):
                                    _prov_acct_kws = _prov_out
                                    break
                            # 1er intento: IIBB + provincia específica
                            if _is_iibb and _prov_acct_kws:
                                for _aid, _albl in _rc_accts_pre_s:
                                    _albl_low = _albl.lower()
                                    if "(copia)" in _albl_low:
                                        continue
                                    _iibb_ok = "iibb" in _albl_low or "ingresos brutos" in _albl_low
                                    _prov_ok  = any(pk in _albl_low for pk in _prov_acct_kws)
                                    if _iibb_ok and _prov_ok:
                                        _ret_acct_id  = _aid
                                        _ret_acct_lbl = _albl
                                        break
                            # 2do intento: Contribuciones Patronales
                            if not _ret_acct_id and _is_patronal:
                                for _aid, _albl in _rc_accts_pre_s:
                                    _albl_low = _albl.lower()
                                    if "(copia)" in _albl_low:
                                        continue
                                    if any(k in _albl_low for k in
                                           ["patronal", "contribucion", "contribución", "suss"]):
                                        _ret_acct_id  = _aid
                                        _ret_acct_lbl = _albl
                                        break
                            # 3er intento: Ganancias
                            if not _ret_acct_id and _is_ganancias:
                                for _aid, _albl in _rc_accts_pre_s:
                                    _albl_low = _albl.lower()
                                    if "(copia)" in _albl_low:
                                        continue
                                    if "ganancia" in _albl_low:
                                        _ret_acct_id  = _aid
                                        _ret_acct_lbl = _albl
                                        break
                            # 4to intento: IIBB sin provincia (cualquier IIBB)
                            if not _ret_acct_id and _is_iibb:
                                for _aid, _albl in _rc_accts_pre_s:
                                    _albl_low = _albl.lower()
                                    if "(copia)" in _albl_low:
                                        continue
                                    if "iibb" in _albl_low or "ingresos brutos" in _albl_low:
                                        _ret_acct_id  = _aid
                                        _ret_acct_lbl = _albl
                                        break
                            # Fallback: cualquier cuenta de retención
                            if not _ret_acct_id:
                                for _aid, _albl in _rc_accts_pre_s:
                                    _albl_low = _albl.lower()
                                    if any(kw in _albl_low for kw in
                                           ["retenc", "ingresos brutos", "iibb"]) \
                                            and "(copia)" not in _albl_low:
                                        _ret_acct_id  = _aid
                                        _ret_acct_lbl = _albl
                                        break
                            if _ret_acct_lbl and _ret_acct_lbl in _rc_acct_opts_pre:
                                _ret_acct_idx = _rc_acct_opts_pre.index(_ret_acct_lbl)
                            st.session_state[_ded_key].append({
                                "uid":               _new_uid,
                                "monto":             float(_cret["importe"]),
                                "concepto_idx":      _ret_acct_idx,
                                "concepto":          _ret_acct_lbl,
                                "account_id":        _ret_acct_id,
                                "_ret_filename":     _cret_fname,
                                "_ret_concepto_pdf": _cret_concepto_key,
                                "_ret_auto":         True,
                            })
                            st.toast(
                                f"Retención de {_cret.get('nombre','')} "
                                f"(ARS {fmt_ars(_cret['importe'])}) agregada automáticamente.",
                                icon="🔖")

                    # Cargar cuentas para conceptos (reutiliza cache de facturas)
                    _rc_accts_raw = get_all_accounts(models_url, uid, api_key)
                    # Poner cuentas "(copia)" al final para no confundir con las originales
                    _rc_accts = sorted(
                        _rc_accts_raw,
                        key=lambda x: (1 if "(copia)" in x[1].lower() else 0, x[1]))
                    _rc_acct_opts = ["— Seleccionar concepto —"] + [lbl for _, lbl in _rc_accts]

                    _deds = st.session_state[_ded_key]
                    if _deds:
                        st.markdown("**Deducciones / Retenciones / NC:**")
                    _to_remove = None
                    for _ded in _deds:
                        _uid = _ded["uid"]
                        _dc1, _dc2, _dc3 = st.columns([5, 2, 1])
                        _cpt_key = f"rc_ded_cpt_{_rcuit}_{_uid}"
                        _mnt_key = f"rc_ded_mnt_{_rcuit}_{_uid}"
                        _cur_idx = _ded.get("concepto_idx", 0)
                        _new_cpt = _dc1.selectbox(
                            "", options=_rc_acct_opts, index=_cur_idx,
                            key=_cpt_key, label_visibility="collapsed")
                        _new_mnt = _dc2.number_input(
                            "", value=float(_ded.get("monto", 0.0)),
                            min_value=0.0, step=0.01, format="%.2f",
                            key=_mnt_key, label_visibility="collapsed")
                        if _dc3.button("✕", key=f"rc_rm_{_rcuit}_{_uid}"):
                            _to_remove = _uid
                        _cpt_idx = _rc_acct_opts.index(_new_cpt) if _new_cpt in _rc_acct_opts else 0
                        _acct_id = next((aid for aid, albl in _rc_accts if albl == _new_cpt), None)
                        _ded.update({"concepto": _new_cpt, "concepto_idx": _cpt_idx,
                                     "account_id": _acct_id, "monto": _new_mnt})

                    if _to_remove is not None:
                        st.session_state[_ded_key] = [d for d in _deds if d["uid"] != _to_remove]
                        st.rerun()

                    if st.button("➕ Agregar retención / NC", key=f"rc_add_ded_{_rcuit}"):
                        _new_uid = st.session_state[_ded_cnt_key] + 1
                        st.session_state[_ded_cnt_key] = _new_uid
                        st.session_state[_ded_key].append(
                            {"uid": _new_uid, "monto": 0.0, "concepto_idx": 0,
                             "concepto": "", "account_id": None})
                        st.rerun()

                    _rc_ajuste_total = sum(
                        d.get("monto", 0.0) for d in st.session_state[_ded_key])

                    _rc_memo = st.text_input(
                        "Referencia / Memo",
                        value=f"Recibo cheques — {_rchs[0]['nombre']}",
                        key=f"rc_memo_{_rcuit}")

                    _rc_neto = _rc_amount - _rc_ajuste_total
                    _rc_info = f"**Importe neto:** ARS {fmt_ars(_rc_neto)}"
                    if _rc_ajuste_total > 0:
                        _rc_info += f"  ·  Deducciones: ARS {fmt_ars(_rc_ajuste_total)}"
                    if _rcsel_ids:
                        _rc_info += (
                            f"  ·  {len(_rcsel_ids)} factura(s) seleccionada(s) "
                            f"(saldo ARS {fmt_ars(_rcsel_saldo)})")
                    else:
                        _rc_info += "  ·  Sin facturas → se registra como pago a cuenta"
                    st.info(_rc_info)

                    # ── Diferencia de redondeo ─────────────────────────────────────────
                    _rc_writeoff_account_id = None
                    _rc_writeoff_label = "Diferencia de redondeo"
                    if _rcsel_ids and _rcsel_saldo > 0:
                        _rc_diff = _rcsel_saldo - _rc_neto
                        _rc_diff_abs = abs(_rc_diff)
                        if 0 < _rc_diff_abs <= 1.0:
                            st.warning(
                                f"⚖️ Diferencia de **ARS {fmt_ars(_rc_diff_abs)}** entre "
                                f"el importe cobrado (ARS {fmt_ars(_rc_neto)}) y el saldo "
                                f"de las facturas seleccionadas (ARS {fmt_ars(_rcsel_saldo)}).")
                            _wo_chk_key = f"rc_writeoff_chk_{_rcuit}"
                            _wo_checked = st.checkbox(
                                f"Registrar ARS {fmt_ars(_rc_diff_abs)} como diferencia de "
                                "redondeo y marcar facturas como pagadas en su totalidad",
                                key=_wo_chk_key)
                            if _wo_checked:
                                # Pre-seleccionar cuentas con "redon" en el nombre
                                _rc_wo_accts_filt = [
                                    (aid, albl) for aid, albl in _rc_accts
                                    if "redon" in albl.lower()
                                ]
                                _rc_wo_pool = _rc_wo_accts_filt if _rc_wo_accts_filt else _rc_accts
                                _rc_wo_opts = ["— Seleccionar cuenta —"] + [lbl for _, lbl in _rc_wo_pool]
                                _wo_acct_col, _wo_lbl_col = st.columns([3, 2])
                                _rc_wo_acct_sel = _wo_acct_col.selectbox(
                                    "Cuenta para la diferencia",
                                    options=_rc_wo_opts,
                                    index=(1 if _rc_wo_accts_filt else 0),
                                    key=f"rc_wo_acct_{_rcuit}")
                                _rc_wo_lbl_input = _wo_lbl_col.text_input(
                                    "Etiqueta del asiento",
                                    value="Diferencia de redondeo",
                                    key=f"rc_wo_lbl_{_rcuit}")
                                if _rc_wo_acct_sel != "— Seleccionar cuenta —":
                                    _rc_writeoff_account_id = next(
                                        (aid for aid, albl in _rc_wo_pool
                                         if albl == _rc_wo_acct_sel), None)
                                    _rc_writeoff_label = _rc_wo_lbl_input or "Diferencia de redondeo"

                    _dup_pending = st.session_state.get(f"rc_confirm_dup_{_rcuit}", False)
                    if _dup_pending:
                        st.button("↩️ Cancelar", key=f"rc_cancel_dup_{_rcuit}",
                                  on_click=lambda: st.session_state.pop(
                                      f"rc_confirm_dup_{_rcuit}", None))
                        _rc_reg_btn = st.button(
                            "⚠️ Registrar igual (puede ser duplicado)",
                            type="secondary", key=f"rc_btn_{_rcuit}")
                    else:
                        _rc_reg_btn = st.button(
                            f"💵 Registrar Recibo en Odoo",
                            type="primary", key=f"rc_btn_{_rcuit}")

                    if _rc_reg_btn:
                        if _rc_neto <= 0:
                            st.error("El importe neto debe ser mayor a cero.")
                        else:
                            _rc_date_str = _rc_date.strftime("%Y-%m-%d")
                            # Obtener currency_id: primero buscar ARS dinámicamente
                            _rc_cur_id = None
                            try:
                                _ars_cur = models.execute_kw(_cfg.ODOO_DB, uid, api_key,
                                    "res.currency", "search_read",
                                    [[("name", "=", "ARS"), ("active", "in", [True, False])]],
                                    {"fields": ["id"], "limit": 1})
                                if _ars_cur:
                                    _rc_cur_id = _ars_cur[0]["id"]
                            except Exception:
                                pass
                            if not _rc_cur_id:
                                _rc_cur_id = 1  # fallback
                            if _rcsel_ids and _rc_invs:
                                _rci_first = next(
                                    (i for i in _rc_invs
                                     if i["id"] == _rcsel_ids[0]), None)
                                if _rci_first:
                                    _rcurr = _rci_first.get("currency_id")
                                    if _rcurr and isinstance(_rcurr, (list, tuple)):
                                        _rc_cur_id = _rcurr[0]

                            # ── Validación de duplicados ──────────────────
                            # Busca pagos ya registrados con mismo cliente,
                            # monto, fecha y journal en estado confirmado.
                            _dup_key = f"rc_confirm_dup_{_rcuit}"
                            _dup_existing = []
                            try:
                                _dup_existing = models.execute_kw(
                                    _cfg.ODOO_DB, uid, api_key,
                                    "account.payment", "search_read",
                                    [[
                                        ("partner_id",   "=",  _rc_pid),
                                        ("amount",       "=",  _rc_neto),
                                        ("date",         "=",  _rc_date_str),
                                        ("journal_id",   "=",  _rc_jour_id),
                                        ("payment_type", "=",  "inbound"),
                                        ("state",        "in", ["posted", "reconciled"]),
                                    ]],
                                    {"fields": ["id", "name", "amount", "date"], "limit": 3})
                            except Exception:
                                pass

                            if _dup_existing and not st.session_state.get(_dup_key):
                                _dup_names = ", ".join(
                                    d.get("name","?") for d in _dup_existing)
                                st.warning(
                                    f"⚠️ Ya existe un cobro registrado con el mismo cliente, "
                                    f"monto y fecha: **{_dup_names}**. "
                                    f"Si es un cobro diferente, confirmá para continuar.")
                                st.session_state[_dup_key] = True
                                st.rerun()
                            else:
                                # Limpiar flag de confirmación para próxima vez
                                st.session_state.pop(_dup_key, None)
                                _rc_cheque_vals = []
                                for _rch in _rchs:
                                    _bk = match_bank_id(_rch.get("banco",""),_rc_all_banks)
                                    _dt = _rch.get("fecha_pago","") or ""
                                    try:
                                        if "/" in _dt:
                                            _p = _dt.split("/")
                                            if len(_p)==3:
                                                _dt = f"{_p[2]}-{_p[1].zfill(2)}-{_p[0].zfill(2)}"
                                    except Exception:
                                        _dt = _rc_date_str
                                    if not _dt or len(_dt)<8: _dt = _rc_date_str
                                    _rc_cheque_vals.append({
                                        "nro":          _rch.get("nro",""),
                                        "bank_id":      _bk,
                                        "issuer_vat":   _rch.get("cuit",""),
                                        "payment_date": _dt,
                                        "amount":       float(_rch.get("importe") or 0),
                                    })
                                # ── Validar cheques duplicados ────────────────────────────────
                                _chq_dups = []
                                for _rch_v in _rc_cheque_vals:
                                    _is_dup_chq, _dup_chq_name, _ = check_duplicate_cheque(
                                        models_url, uid, api_key,
                                        _rch_v.get("nro",""), _rch_v.get("issuer_vat",""))
                                    if _is_dup_chq:
                                        _chq_dups.append(
                                            f"Cheque nro {_rch_v.get('nro','')} "
                                            f"(CUIT {_rch_v.get('issuer_vat','')}): "
                                            f"ya registrado como {_dup_chq_name}")
                                if _chq_dups:
                                    st.error(
                                        "❌ Los siguientes cheques ya están registrados en Odoo:\n"
                                        + "\n".join(f"• {d}" for d in _chq_dups))
                                    st.session_state.pop(_dup_key, None)
                                if not _chq_dups:
                                    # Preparar datos antes del spinner
                                    _rc_withholdings = [
                                        d for d in st.session_state.get(_ded_key, [])
                                        if float(d.get("monto", 0)) > 0
                                    ] if _rc_cheque_vals else None
                                    _rc_all_move_ids = (
                                        (_rcsel_ids or []) + (_rcncsel_ids or [])
                                    ) or None
                                    with st.status("Registrando cobro en Odoo...", expanded=True) as _rc_status:
                                        st.write(f"💳 Grupo de pago · ARS {fmt_ars(_rc_neto)}")
                                        if _rc_cheque_vals:
                                            st.write(f"🏦 {len(_rc_cheque_vals)} cheque(s) por acreditar")
                                        if _rc_withholdings:
                                            st.write(f"📋 {len(_rc_withholdings)} retención(es) a registrar")
                                        if _rc_all_move_ids:
                                            st.write(f"🔗 Imputando {len(_rc_all_move_ids)} comprobante(s)")
                                        if _rc_writeoff_account_id:
                                            _diff_disp = abs(_rcsel_saldo - _rc_neto)
                                            st.write(f"⚖️ Saldando diferencia de redondeo ARS {fmt_ars(_diff_disp)}")
                                        _rc_ok, _rc_res = register_customer_payment(
                                            models, uid, api_key,
                                            _rc_pid, _rc_neto, _rc_cur_id,
                                            _rc_date_str, _rc_jour_id,
                                            move_ids=_rc_all_move_ids,
                                            memo=_rc_memo,
                                            cheques=_rc_cheque_vals if _rc_cheque_vals else None,
                                            withholdings=_rc_withholdings,
                                            writeoff_account_id=_rc_writeoff_account_id,
                                            writeoff_label=_rc_writeoff_label)
                                        if _rc_ok:
                                            _rc_status.update(
                                                label="✅ Cobro registrado",
                                                state="complete", expanded=False)
                                        else:
                                            _rc_status.update(
                                                label="❌ Error al registrar",
                                                state="error", expanded=True)
                                    if _rc_ok:
                                        _wh_warn = isinstance(_rc_res, str) and _rc_res.startswith("__WH_WARN__")
                                        st.toast(
                                            f"Recibo registrado para {_rc_pname} — ARS {fmt_ars(_rc_neto)}", icon="✅")
                                        register_processed_file(
                                            _rc_bytes, _rc_file.name, "Recibo de cobro",
                                            f"{_rc_pname} ARS {fmt_ars(_rc_neto)}")
                                        search_partners_by_cuits.clear()
                                        get_customer_unpaid_invoices.clear()
                                        get_customer_pending_credit_notes.clear()
                                        if _wh_warn:
                                            _wh_detail = _rc_res.replace("__WH_WARN__", "")
                                            show_odoo_warning(
                                                f"Recibo registrado, pero no se pudo crear el pago de retención "
                                                f"en Odoo. Registralo manualmente. Detalle: {_wh_detail}",
                                                "registrar retención")
                                        else:
                                            st.info(
                                                "Presioná 🔄 Actualizar para ver "
                                                "el estado actualizado.")
                                    else:
                                        st.error(f"Error al registrar en Odoo: {_rc_res}")

    st.divider()
    st.caption(
        f"Para emitir facturas de venta o gestionar cobros manualmente, "
        f"usá [Odoo Ventas]({_cfg.ODOO_URL}/odoo/accounting/customers/invoices) directamente.")


    # ═══════════════════════════════════════════════════════════════════════
    # SECCIÓN: Cobro manual / transferencia (sin archivo de home banking)
    # ═══════════════════════════════════════════════════════════════════════
    with st.expander("💸 Cobro manual / transferencia", expanded=False):
        st.caption(
            "Registrá cobros por transferencia bancaria, efectivo u otro medio "
            "sin necesidad de cargar un archivo del home banking.")

        # ── Búsqueda de cliente ───────────────────────────────────────────
        _man_sq, _man_sbtn = st.columns([4, 1])
        _man_q = _man_sq.text_input(
            "Buscar cliente por nombre",
            key="rc_man_q",
            placeholder="Ej: Acosta, Distribuidora Norte…",
            label_visibility="collapsed")
        _man_buscar = _man_sbtn.button(
            "🔍 Buscar", key="rc_man_buscar", use_container_width=True)

        if _man_buscar:
            if len(_man_q.strip()) < 3:
                st.warning("Ingresá al menos 3 caracteres.")
                st.session_state.pop("rc_man_partners", None)
            else:
                with st.spinner("Buscando…"):
                    try:
                        _man_found = models.execute_kw(
                            _cfg.ODOO_DB, uid, api_key, "res.partner", "search_read",
                            [[("name", "ilike", _man_q.strip()),
                              ("customer_rank", ">", 0),
                              ("active", "=", True)]],
                            {"fields": ["id", "name", "vat"], "limit": 10})
                        st.session_state["rc_man_partners"] = _man_found
                    except Exception as _me:
                        st.error(f"Error al buscar: {_me}")
                        st.session_state["rc_man_partners"] = []

        _man_partners = st.session_state.get("rc_man_partners")

        if _man_partners is None:
            st.caption("Ingresá un nombre y presioná Buscar.")
        elif not _man_partners:
            st.warning("No se encontraron clientes con ese nombre.")
        else:
            _man_opts = {
                f"{r['name']}  ({r.get('vat') or 'sin CUIT'})": (r["id"], r["name"])
                for r in _man_partners
            }
            _man_sel_lbl = st.selectbox(
                "Cliente", list(_man_opts.keys()),
                key="rc_man_client_sel")
            _man_pid, _man_pname = _man_opts[_man_sel_lbl]
            st.markdown(f"**Cliente Odoo:** {_man_pname} (ID {_man_pid})")

            if st.button("🔄 Actualizar facturas", key="rc_man_refresh"):
                get_customer_unpaid_invoices.clear()
                get_customer_pending_credit_notes.clear()
                st.rerun()

            with st.spinner("Cargando facturas…"):
                _man_invs = get_customer_unpaid_invoices(
                    models_url, uid, api_key, (_man_pid,))
                _man_ncs  = get_customer_pending_credit_notes(
                    models_url, uid, api_key, (_man_pid,))

            # ── Selector de facturas ──────────────────────────────────────
            _mansel_ids   = []
            _mansel_saldo = 0.0
            if not _man_invs:
                st.info(
                    "No hay facturas pendientes. El cobro se registrará "
                    "como pago a cuenta.")
            else:
                st.markdown("**Seleccioná las facturas a cobrar:**")
                _man_inv_rows = []
                for _mi in _man_invs:
                    _mic  = (_mi.get("currency_id") or [0, "ARS"])[1]
                    _mres = float(_mi.get("amount_residual") or 0)
                    _mto  = float(_mi.get("amount_total") or 0)
                    _mv   = str(_mi.get("invoice_date_due") or "")
                    _mvd  = (f"⚠️ {_mv}" if _mv and _mv < str(_rc_date_cls.today()) else _mv)
                    _man_inv_rows.append({
                        "Sel":        False,
                        "Factura":    _mi.get("name") or f"ID {_mi['id']}",
                        "Fecha":      str(_mi.get("invoice_date") or ""),
                        "Vence":      _mvd,
                        "Moneda":     _mic,
                        "Total":      fmt_ars(_mto)  if _mic == "ARS" else f"{_mic} {_mto:,.2f}",
                        "Saldo":      fmt_ars(_mres) if _mic == "ARS" else f"{_mic} {_mres:,.2f}",
                        "_saldo_num": _mres,
                        "_id":        _mi["id"],
                    })
                _man_inv_df = pd.DataFrame(_man_inv_rows)
                _man_inv_cfg = {
                    "Sel":        st.column_config.CheckboxColumn("✓", width="small"),
                    "Total":      st.column_config.TextColumn("Total"),
                    "Saldo":      st.column_config.TextColumn("Saldo"),
                    "_saldo_num": None,
                    "_id":        None,
                }
                _man_inv_disp = ["Sel", "Factura", "Fecha", "Vence", "Moneda", "Total", "Saldo"]
                _man_inv_ed = st.data_editor(
                    _man_inv_df[_man_inv_disp + ["_saldo_num", "_id"]],
                    column_config=_man_inv_cfg,
                    column_order=_man_inv_disp,
                    use_container_width=True, hide_index=True,
                    key="rc_man_inv_editor",
                    disabled=[c for c in _man_inv_disp if c != "Sel"],
                )
                _mansel       = _man_inv_ed[_man_inv_ed["Sel"] == True]
                _mansel_ids   = [int(r) for r in _mansel["_id"].tolist()]
                _mansel_saldo = float(_mansel["_saldo_num"].sum())

            # ── Selector de notas de crédito ──────────────────────────────
            _manncsel_ids   = []
            _manncsel_total = 0.0
            if _man_ncs:
                st.markdown("**Notas de crédito pendientes:**")
                _man_nc_rows = []
                for _mnc in _man_ncs:
                    _mncic  = (_mnc.get("currency_id") or [0, "ARS"])[1]
                    _mncres = float(_mnc.get("amount_residual") or 0)
                    _mncto  = float(_mnc.get("amount_total") or 0)
                    _man_nc_rows.append({
                        "Sel":        False,
                        "NC":         _mnc.get("name") or f"ID {_mnc['id']}",
                        "Fecha":      str(_mnc.get("invoice_date") or ""),
                        "Moneda":     _mncic,
                        "Saldo NC":   fmt_ars(_mncres) if _mncic == "ARS" else f"{_mncic} {_mncres:,.2f}",
                        "_saldo_num": _mncres,
                        "_id":        _mnc["id"],
                    })
                _man_nc_df = pd.DataFrame(_man_nc_rows)
                _man_nc_cfg = {
                    "Sel":        st.column_config.CheckboxColumn("✓", width="small"),
                    "Saldo NC":   st.column_config.TextColumn("Saldo NC"),
                    "_saldo_num": None,
                    "_id":        None,
                }
                _man_nc_disp = ["Sel", "NC", "Fecha", "Moneda", "Saldo NC"]
                _man_nc_ed = st.data_editor(
                    _man_nc_df[_man_nc_disp + ["_saldo_num", "_id"]],
                    column_config=_man_nc_cfg,
                    column_order=_man_nc_disp,
                    use_container_width=True, hide_index=True,
                    key="rc_man_nc_editor",
                    disabled=[c for c in _man_nc_disp if c != "Sel"],
                )
                _manncsel       = _man_nc_ed[_man_nc_ed["Sel"] == True]
                _manncsel_ids   = [int(r) for r in _manncsel["_id"].tolist()]
                _manncsel_total = float(_manncsel["_saldo_num"].sum())
                if _manncsel_total > 0:
                    st.caption(f"NC seleccionadas: ARS {fmt_ars(_manncsel_total)}")

            # ── Formulario de pago ────────────────────────────────────────
            st.markdown("#### Datos del cobro")
            _man_fc1, _man_fc2 = st.columns([1, 1])
            _man_date = _man_fc1.date_input(
                "Fecha de cobro",
                value=_rc_date_cls.today(),
                key="rc_man_date")
            _man_amount = _man_fc2.number_input(
                "Importe (ARS)",
                min_value=0.0,
                value=float(_mansel_saldo) if _mansel_saldo > 0 else 0.0,
                step=0.01, format="%.2f",
                key="rc_man_amount",
                help="Pre-completado con el saldo de las facturas seleccionadas.")

            # Diario: todos los diarios disponibles, pre-seleccionado según preferencia
            _man_jour_list    = [(jid, jname) for jid, jname, *_ in _all_rc_journals]
            _man_jour_names   = [jname for _, jname in _man_jour_list]
            _man_jour_default = next(
                (i for i, (_, jn) in enumerate(_man_jour_list) if jn == _pref_jour),
                0) if _pref_jour else 0
            _man_jour_sel = st.selectbox(
                "Diario / Banco",
                _man_jour_names,
                index=_man_jour_default,
                key="rc_man_jour")
            _man_jour_id = next(
                (jid for jid, jn in _man_jour_list if jn == _man_jour_sel),
                _RC_JOURNAL_ID)

            _man_memo = st.text_input(
                "Referencia / Memo",
                value=f"Cobro — {_man_pname}",
                key="rc_man_memo")

            _man_info = f"**Importe:** ARS {fmt_ars(_man_amount)}"
            if _mansel_ids:
                _man_info += (
                    f"  ·  {len(_mansel_ids)} factura(s) "
                    f"(saldo ARS {fmt_ars(_mansel_saldo)})")
            else:
                _man_info += "  ·  Sin facturas → pago a cuenta"
            st.info(_man_info)

            # ── Diferencia de redondeo ────────────────────────────────────
            _man_writeoff_id  = None
            _man_writeoff_lbl = "Diferencia de redondeo"
            if _mansel_ids and _mansel_saldo > 0:
                _man_diff     = _mansel_saldo - _man_amount
                _man_diff_abs = abs(_man_diff)
                if 0 < _man_diff_abs <= 1.0:
                    st.warning(
                        f"⚖️ Diferencia de **ARS {fmt_ars(_man_diff_abs)}** entre "
                        f"el importe y el saldo de las facturas.")
                    _man_wo_chk = st.checkbox(
                        f"Registrar ARS {fmt_ars(_man_diff_abs)} como diferencia de redondeo",
                        key="rc_man_wo_chk")
                    if _man_wo_chk:
                        _man_accts_raw = get_all_accounts(models_url, uid, api_key)
                        _man_accts = sorted(
                            _man_accts_raw,
                            key=lambda x: (1 if "(copia)" in x[1].lower() else 0, x[1]))
                        _man_wo_filt = [(aid, albl) for aid, albl in _man_accts
                                        if "redon" in albl.lower()]
                        _man_wo_pool = _man_wo_filt if _man_wo_filt else _man_accts
                        _man_wo_opts = ["— Seleccionar cuenta —"] + [lbl for _, lbl in _man_wo_pool]
                        _mwa_col, _mwl_col = st.columns([3, 2])
                        _man_wo_acct = _mwa_col.selectbox(
                            "Cuenta para la diferencia", _man_wo_opts,
                            index=(1 if _man_wo_filt else 0),
                            key="rc_man_wo_acct")
                        _man_wo_lbl_inp = _mwl_col.text_input(
                            "Etiqueta", value="Diferencia de redondeo",
                            key="rc_man_wo_lbl")
                        if _man_wo_acct != "— Seleccionar cuenta —":
                            _man_writeoff_id = next(
                                (aid for aid, albl in _man_wo_pool
                                 if albl == _man_wo_acct), None)
                            _man_writeoff_lbl = _man_wo_lbl_inp or "Diferencia de redondeo"

            # ── Botón registro ────────────────────────────────────────────
            if st.button("💵 Registrar cobro en Odoo",
                         type="primary", key="rc_man_reg_btn"):
                if _man_amount <= 0:
                    st.error("El importe debe ser mayor a cero.")
                else:
                    _man_all_moves = (_mansel_ids + _manncsel_ids) or None
                    _man_cur_id    = None
                    try:
                        _man_ars = models.execute_kw(
                            _cfg.ODOO_DB, uid, api_key, "res.currency", "search_read",
                            [[("name", "=", "ARS"), ("active", "in", [True, False])]],
                            {"fields": ["id"], "limit": 1})
                        if _man_ars:
                            _man_cur_id = _man_ars[0]["id"]
                    except Exception:
                        pass
                    _man_cur_id = _man_cur_id or 1
                    with st.status("Registrando cobro…", expanded=True) as _man_st:
                        st.write(f"💳 Cobro · ARS {fmt_ars(_man_amount)}")
                        if _man_all_moves:
                            st.write(f"🔗 Imputando {len(_man_all_moves)} comprobante(s)")
                        if _man_writeoff_id:
                            st.write(f"⚖️ Saldando diferencia ARS "
                                     f"{fmt_ars(abs(_mansel_saldo - _man_amount))}")
                        _man_ok, _man_res_val = register_customer_payment(
                            models, uid, api_key,
                            _man_pid, _man_amount, _man_cur_id,
                            _man_date.strftime("%Y-%m-%d"), _man_jour_id,
                            move_ids=_man_all_moves,
                            memo=_man_memo,
                            cheques=None,
                            withholdings=None,
                            writeoff_account_id=_man_writeoff_id,
                            writeoff_label=_man_writeoff_lbl)
                        if _man_ok:
                            _man_st.update(
                                label="✅ Cobro registrado",
                                state="complete", expanded=False)
                        else:
                            _man_st.update(
                                label="❌ Error al registrar",
                                state="error", expanded=True)
                    if _man_ok:
                        st.toast(
                            f"Cobro registrado para {_man_pname} "
                            f"— ARS {fmt_ars(_man_amount)}", icon="✅")
                        get_customer_unpaid_invoices.clear()
                        get_customer_pending_credit_notes.clear()
                        st.session_state.pop("rc_man_partners", None)
                        st.rerun()
                    else:
                        st.error(f"Error al registrar en Odoo: {_man_res_val}")

    # ═══════════════════════════════════════════════════════════════════════
    # SECCIÓN: Consulta de recibos ya registrados en Odoo
    # ═══════════════════════════════════════════════════════════════════════
    with st.expander("🔍 Consultar recibos ya registrados en Odoo", expanded=False):
        st.caption("Buscá pagos confirmados en Odoo por cliente, rango de fechas o ambos.")
        _srch_c1, _srch_c2, _srch_c3 = st.columns([2, 1, 1])
        _srch_partner_name = _srch_c1.text_input(
            "Nombre del cliente (parcial)", key="srch_rc_partner")
        _srch_from = _srch_c2.date_input(
            "Desde", value=None, key="srch_rc_from")
        _srch_to   = _srch_c3.date_input(
            "Hasta", value=None, key="srch_rc_to")

        if st.button("🔍 Buscar", key="srch_rc_btn", type="primary"):
            _srch_pid = None
            if _srch_partner_name.strip():
                with st.spinner("Buscando cliente..."):
                    try:
                        import xmlrpc.client as _xmlrc
                        _srch_m = _xmlrc.ServerProxy(models_url, allow_none=True)
                        _srch_res = _srch_m.execute_kw(
                            _cfg.ODOO_DB, uid, api_key,
                            "res.partner", "search_read",
                            [[("name", "ilike", _srch_partner_name.strip())]],
                            {"fields": ["id", "name"], "limit": 1})
                        if _srch_res:
                            _srch_pid = _srch_res[0]["id"]
                        else:
                            st.warning(f"No se encontró cliente con nombre '{_srch_partner_name}'.")
                    except Exception as _se:
                        st.error(f"Error al buscar cliente: {_se}")

            with st.spinner("Consultando pagos en Odoo..."):
                _srch_payments = search_registered_payments(
                    models_url, uid, api_key,
                    partner_id=_srch_pid,
                    date_from=_srch_from,
                    date_to=_srch_to,
                )

            if not _srch_payments:
                st.info("No se encontraron recibos con esos criterios.")
            else:
                import pandas as _pd_srch
                _srch_df = _pd_srch.DataFrame(_srch_payments)
                _srch_df["importe"] = _srch_df["importe"].apply(fmt_ars)
                _srch_df["url"] = _srch_df["url"].apply(
                    lambda u: f"[Abrir]({u})" if u else "")
                _srch_disp = _srch_df[["fecha","partner","name","importe","estado","url"]].copy()
                _srch_disp.columns = ["Fecha","Cliente","Referencia","Importe","Estado","Link"]
                st.dataframe(_srch_disp, use_container_width=True, hide_index=True)
                st.caption(f"{len(_srch_payments)} recibo(s) encontrado(s).")


    pass  # end render
