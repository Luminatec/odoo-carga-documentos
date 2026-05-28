"""Tab Asistente."""
import streamlit as st
import config as _cfg



def render(models, uid, api_key, models_url, is_admin):
    import json   as _jc
    import base64 as _b64c
    import io     as _ioc
    import datetime as _dtc

    st.subheader("Asistente Luminatec")
    st.caption("Pregunta sobre facturas, saldos, socios. Pedi PDFs o exportes Excel. Adjunta archivos para analizarlos.")

    _ant_key = st.secrets.get("ANTHROPIC_API_KEY", "")
    if not _ant_key:
        st.warning("Falta ANTHROPIC_API_KEY en los Secrets de Streamlit Cloud.")
        st.stop()

    # Session state
    if "chat_msgs" not in st.session_state: st.session_state.chat_msgs = []
    if "chat_dl"   not in st.session_state: st.session_state.chat_dl   = []
    if "chat_qr"   not in st.session_state: st.session_state.chat_qr   = []  # quick replies

    def _blk_to_dict(b):
        if isinstance(b, dict): return b
        t = getattr(b, "type", None)
        if t == "text":        return {"type": "text", "text": b.text}
        if t == "tool_use":    return {"type": "tool_use", "id": b.id, "name": b.name, "input": b.input}
        return {"type": "text", "text": str(b)}

    def _odoo_pdf(doc_id, report="account.report_invoice_with_payments"):
        import base64 as _b64p
        # Metodo 1: buscar adjunto PDF ya generado en Odoo (ir.attachment)
        try:
            _atts = models.execute_kw(_cfg.ODOO_DB, uid, api_key,
                "ir.attachment", "search_read",
                [[["res_model", "=", "account.move"],
                  ["res_id", "=", doc_id],
                  ["mimetype", "=", "application/pdf"]]],
                {"fields": ["id", "name", "datas"], "limit": 1,
                 "order": "id desc"})
            if _atts and _atts[0].get("datas"):
                return _b64p.b64decode(_atts[0]["datas"]), None
        except Exception:
            pass
        # Metodo 2: sesion HTTP (funciona si el usuario ingreso con password real)
        try:
            import requests as _rq
            _s = _rq.Session()
            _auth = _s.post(
                f"{_cfg.ODOO_URL}/web/session/authenticate",
                json={"jsonrpc": "2.0", "method": "call", "id": 1,
                      "params": {"db": _cfg.ODOO_DB,
                                 "login": st.session_state.get("user_email", ""),
                                 "password": api_key}},
                timeout=15)
            _uid_r = (_auth.json().get("result") or {}).get("uid")
            if _uid_r:
                _r = _s.get(f"{_cfg.ODOO_URL}/report/pdf/{report}/{doc_id}", timeout=90)
                if _r.status_code == 200 and _r.content[:4] == b"%PDF":
                    return _r.content, None
        except Exception:
            pass
        # Metodo 3: devolver link directo a Odoo para que el usuario lo descargue
        _odoo_link = f"{_cfg.ODOO_URL}/odoo/accounting/customer-invoices/{doc_id}"
        return None, f"PDF_LINK:{_odoo_link}"

    def _odoo_xlsx(model, domain, fields, filename="export.xlsx"):
        try:
            import pandas as _pd
            recs = models.execute_kw(_cfg.ODOO_DB, uid, api_key, model, "search_read",
                [domain], {"fields": fields, "limit": 1000, "order": "id desc"})
            df = _pd.DataFrame(recs)
            buf = _ioc.BytesIO()
            with _pd.ExcelWriter(buf, engine="openpyxl") as _w:
                df.to_excel(_w, index=False)
            return buf.getvalue(), filename, None
        except Exception as _e:
            return None, filename, str(_e)

    _tools = [
        {
            "name": "odoo_search",
            "description": (
                "Busca registros en Odoo. Modelos disponibles:\n"
                "- account.move: facturas (campos: id, name, partner_id, invoice_date, amount_total, "
                "state, move_type, payment_state, invoice_origin, ref). "
                "move_type: out_invoice=FC venta, in_invoice=FC compra, out_refund=NC venta, in_refund=NC compra. "
                "state=posted para confirmadas. payment_state: not_paid, partial, paid, in_payment.\n"
                "- account.move.line: lineas de factura (campos: id, move_id, name, product_id, "
                "quantity, price_unit, price_subtotal, account_id). "
                "Usar para buscar por producto dentro de facturas.\n"
                "- res.partner: socios/clientes/proveedores (campos: id, name, vat, email, phone, "
                "customer_rank, supplier_rank, is_company, category_id).\n"
                "- account.payment: pagos (campos: id, name, partner_id, amount, date, "
                "payment_type, state, journal_id).\n"
                "- product.product: variantes de producto (campos: id, name, default_code, "
                "list_price, standard_price, categ_id, active).\n"
                "- product.template: plantillas de producto (campos: id, name, default_code, "
                "list_price, type, categ_id, active).\n"
                "- purchase.order: ordenes de compra (campos: id, name, partner_id, date_order, "
                "amount_total, state).\n"
                "- stock.move: movimientos de stock.\n"
                "Para saldo pendiente: account.move con payment_state in ['not_paid','partial']."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "model":  {"type": "string"},
                    "domain": {"type": "array", "description": "Condiciones Odoo, ej: [['move_type','=','out_invoice'],['state','=','posted']]"},
                    "fields": {"type": "array", "items": {"type": "string"}},
                    "limit":  {"type": "integer", "default": 10},
                    "order":  {"type": "string", "default": ""}
                },
                "required": ["model", "domain", "fields"]
            }
        },
        {
            "name": "odoo_get_pdf",
            "description": "Genera el PDF de una factura de Odoo y lo deja disponible para descargar.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "doc_id":   {"type": "integer", "description": "ID del documento en Odoo"},
                    "doc_name": {"type": "string",  "description": "Nombre para el archivo, ej: FCE-A_00011-00000779"}
                },
                "required": ["doc_id"]
            }
        },
        {
            "name": "odoo_export_xlsx",
            "description": "Exporta registros de Odoo a Excel descargable.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "model":    {"type": "string"},
                    "domain":   {"type": "array"},
                    "fields":   {"type": "array", "items": {"type": "string"}},
                    "filename": {"type": "string"}
                },
                "required": ["model", "domain", "fields", "filename"]
            }
        },
        {
            "name": "odoo_aggregate",
            "description": (
                "Agrega datos de Odoo con SUM/COUNT/AVG directamente en el servidor (read_group). "
                "Ideal para totales: cuanto se facturo en un periodo, cantidad de facturas, "
                "suma de pagos, etc. Devuelve un solo registro con los totales calculados. "
                "Ejemplo: para total facturado en Mayo usar model='account.move', "
                "domain=[['move_type','=','out_invoice'],['state','=','posted'],"
                "['invoice_date','>=','2026-05-01'],['invoice_date','<=','2026-05-31']], "
                "aggregate_fields=['amount_total:sum','id:count']."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "model":            {"type": "string"},
                    "domain":           {"type": "array"},
                    "aggregate_fields": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Campos con funcion: 'amount_total:sum', 'id:count', 'amount_total:avg'"
                    },
                    "group_by": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Opcional: agrupar por campo, ej: ['partner_id'] o ['invoice_date:month']"
                    }
                },
                "required": ["model", "domain", "aggregate_fields"]
            }
        }
    ]

    def _exec_tool(name, inp):
        if name == "odoo_search":
            try:
                recs = models.execute_kw(_cfg.ODOO_DB, uid, api_key,
                    inp["model"], "search_read",
                    [inp["domain"]],
                    {"fields": inp["fields"], "limit": inp.get("limit", 10), "order": inp.get("order", "")})
                return _jc.dumps(recs, ensure_ascii=False, default=str)
            except Exception as _e:
                return f"Error busqueda: {_e}"
        elif name == "odoo_get_pdf":
            _did   = inp["doc_id"]
            _dname = inp.get("doc_name", f"documento_{_did}").replace("/", "-").replace(" ", "_")
            _pdf, _err = _odoo_pdf(_did)
            if _pdf:
                _fname = _dname if _dname.endswith(".pdf") else f"{_dname}.pdf"
                st.session_state.chat_dl.append({"name": _fname, "data": _pdf, "mime": "application/pdf"})
                return f"PDF '{_fname}' generado ({len(_pdf):,} bytes). Disponible para descargar."
            if _err and _err.startswith("PDF_LINK:"):
                _link = _err.replace("PDF_LINK:", "")
                return f"No pude generar el PDF automaticamente (requiere password real, no API key). Podés descargarlo directamente desde Odoo: {_link}"
            return f"No se pudo generar el PDF: {_err}"
        elif name == "odoo_export_xlsx":
            _xb, _fn, _err = _odoo_xlsx(inp["model"], inp["domain"], inp["fields"],
                                         filename=inp.get("filename", "export.xlsx"))
            if _xb:
                st.session_state.chat_dl.append({
                    "name": _fn, "data": _xb,
                    "mime": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"})
                return f"Excel '{_fn}' generado. Disponible para descargar."
            return f"No se pudo generar el Excel: {_err}"
        elif name == "odoo_aggregate":
            try:
                _agg_fields = inp.get("aggregate_fields", [])
                _group_by   = inp.get("group_by", [])
                # Parsear campos de agregacion: "amount_total:sum" → ("amount_total", "sum")
                _spec_fields = []
                _spec_agg    = {}
                for _af in _agg_fields:
                    if ":" in _af:
                        _fn, _func = _af.rsplit(":", 1)
                        _spec_fields.append(_fn)
                        _spec_agg[_fn] = _func
                    else:
                        _spec_fields.append(_af)
                _all_fields = list(set(_spec_fields + _group_by))
                _rg = models.execute_kw(_cfg.ODOO_DB, uid, api_key,
                    inp["model"], "read_group",
                    [inp["domain"], _all_fields, _group_by or []],
                    {"lazy": False})
                # Simplificar resultado
                _out_rows = []
                for _row in _rg:
                    _out_row = {}
                    for _fn in _spec_fields:
                        _out_row[_fn] = _row.get(_fn)
                        if _fn + "_count" in _row:
                            _out_row[_fn + "_count"] = _row[_fn + "_count"]
                    for _gb in _group_by:
                        _gb_base = _gb.split(":")[0]
                        if _gb_base in _row:
                            _out_row[_gb_base] = _row[_gb_base]
                    _out_row["__count"] = _row.get("__count", 0)
                    _out_rows.append(_out_row)
                return _jc.dumps(_out_rows, ensure_ascii=False, default=str)
            except Exception as _ae:
                return f"Error agregacion: {_ae}"
        return "Herramienta desconocida"

    def _run_agent(user_text, file_blocks=None):
        try:
         _run_agent_inner(user_text, file_blocks)
        except Exception as _outer_err:
            st.session_state.chat_msgs.append({
                "role": "assistant",
                "content": [{"type": "text", "text": f"⚠️ Error inesperado: {_outer_err}"}]
            })

    def _run_agent_inner(user_text, file_blocks=None):
        import anthropic as _ac2
        _client = _ac2.Anthropic(api_key=_ant_key)
        _today = _dtc.date.today().isoformat()
        _system = "\n".join([
            "Sos el asistente inteligente de Luminatec, conectado a Odoo en tiempo real.",
            "Podés buscar facturas, socios, pagos, productos y cualquier registro.",
            "Podés generar PDFs de facturas y exportar listas a Excel.",
            "",
            "== ESTRATEGIA DE BUSQUEDA — SIEMPRE SEGUILA ==",
            "",
            "BUSQUEDA POR CLIENTE/PROVEEDOR:",
            "1. Buscá en res.partner: domain [['name','ilike','NOMBRE']], fields ['id','name','customer_rank','vat'], order='customer_rank desc', limit 10.",
            "2. Si hay UNO, usalo directamente. Si hay VARIOS, listalos con formato:",
            "   - NOMBRE EMPRESA (ID XXXXX) — uno por línea. Pedí que confirmen.",
            "3. Si no encontrás nada, probá variantes: truncá el nombre, sacá palabras cortas,",
            "   o buscá por CUIT si lo tenés.",
            "4. Con el partner_id confirmado, buscá en account.move con",
            "   [['partner_id','=',ID],['state','=','posted']].",
            "",
            "BUSQUEDA POR PRODUCTO (código o nombre como LFANT, PERUG, etc.):",
            "1. Primero buscá el producto: product.product domain [['default_code','ilike','COD']]",
            "   o [['name','ilike','NOMBRE']], fields ['id','name','default_code'], limit 10.",
            "2. Si no encontrás por default_code, probá buscar en account.move.line:",
            "   domain [['name','ilike','COD'],['move_id.state','=','posted']],",
            "   fields ['id','move_id','name','product_id','quantity','price_subtotal'], limit 20.",
            "3. También podés buscar: [['product_id.default_code','ilike','COD']]",
            "   o [['product_id.name','ilike','NOMBRE']].",
            "4. Una vez que tenés los move_id de las líneas, podés buscar las facturas completas.",
            "",
            "BUSQUEDA POR NUMERO DE FACTURA:",
            "1. Buscá en account.move: domain [['name','ilike','NUMERO'],['state','=','posted']],",
            "   fields ['id','name','partner_id','invoice_date','amount_total','payment_state'].",
            "",
            "REGLAS GENERALES:",
            "- Para preguntas de TOTALES o CANTIDADES (cuanto se facturo, cuantas facturas, etc.)",
            "  SIEMPRE usa odoo_aggregate con read_group en lugar de odoo_search. Es mas rapido y exacto.",
            "- NUNCA te rindas en el primer intento fallido. Probá al menos 2 estrategias distintas.",
            "- Si una búsqueda da vacío, ajustá el domain y reintentá automáticamente.",
            "- Usá ilike para búsquedas parciales, nunca '=' para nombres.",
            "- Para la última factura: order='invoice_date desc', limit 1.",
            "- Para facturas pendientes: payment_state in ['not_paid','partial'].",
            "- Cuando mostrás facturas, incluí: número, fecha, monto total, estado de pago.",
            "- Si el usuario pide el PDF de una factura específica, usá odoo_get_pdf con su ID.",
            "- Si pide exportar a Excel, usá odoo_export_xlsx.",
            "- Respondé siempre en castellano. Sé conciso pero completo.",
            "",
            "== CUANDO RECIBES UN DOCUMENTO ADJUNTO (PDF o imagen) ==",
            "1. Analizalo INMEDIATAMENTE. Identificá qué tipo es:",
            "   pedido de compra, factura, remito, nota de crédito, presupuesto, etc.",
            "2. Extraé TODOS los datos clave:",
            "   - Encabezado: empresa emisora, destinatario, fecha, número de doc, condición de pago.",
            "   - Si tiene productos: código, descripción, marca, modelo, cantidad, precio unit, total.",
            "   - Totales: subtotal, IVA, total general.",
            "3. Presentá los datos en una tabla markdown clara.",
            "4. ACCIÓN AUTOMÁTICA: buscá en Odoo los datos relevantes sin esperar que te lo pidan.",
            "   - Si es un pedido de un cliente: buscá ese cliente en res.partner.",
            "   - Si tiene códigos de producto: buscá cada uno en product.product por default_code.",
            "   - Si es una factura de proveedor: buscá el proveedor en res.partner.",
            "5. Ofrecé acciones concretas según el tipo de doc (crear pedido, buscar facturas, etc.).",
            f"Fecha hoy: {_today}",
        ])
        _ublocks = []
        if file_blocks:
            _ublocks.extend(file_blocks)
        _ublocks.append({"type": "text", "text": user_text})
        st.session_state.chat_msgs.append({"role": "user", "content": _ublocks})
        _msgs = [{"role": m["role"], "content": m["content"]}
                 for m in st.session_state.chat_msgs]
        for _ in range(10):
            _resp = _client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=4096,
                system=_system,
                tools=_tools,
                messages=_msgs,
            )
            _msgs.append({"role": "assistant", "content": _resp.content})
            if _resp.stop_reason != "tool_use":
                break
            _results = []
            for _blk in _resp.content:
                if _blk.type == "tool_use":
                    _out = _exec_tool(_blk.name, _blk.input)
                    _results.append({"type": "tool_result", "tool_use_id": _blk.id, "content": _out})
            _msgs.append({"role": "user", "content": _results})
        st.session_state.chat_msgs = [
            {"role": m["role"],
             "content": [_blk_to_dict(b) for b in (
                 m["content"] if isinstance(m["content"], list)
                 else [{"type": "text", "text": str(m["content"])}])]}
            for m in _msgs
        ]
        # Extraer opciones clickeables de la ultima respuesta del asistente
        import re as _re
        st.session_state.chat_qr = []
        for _lm in reversed(st.session_state.chat_msgs):
            if _lm.get("role") == "assistant":
                _ltexts = [b["text"] for b in _lm.get("content", [])
                           if isinstance(b, dict) and b.get("type") == "text"]
                _ltext = " ".join(_ltexts)
                _qr_matches = _re.findall(r"[-*]?\s*(.+?)\s+\(ID\s+(\d+)\)", _ltext)
                if len(_qr_matches) > 1:
                    st.session_state.chat_qr = [{"label": n.strip(), "id": int(i)} for n, i in _qr_matches]
                break


    # Downloads pendientes
    if st.session_state.chat_dl:
        _dl_cols = st.columns(min(len(st.session_state.chat_dl), 4))
        for _i, _dl in enumerate(st.session_state.chat_dl):
            _dl_cols[_i % 4].download_button(
                label=f"Descargar {_dl['name']}",
                data=_dl["data"],
                file_name=_dl["name"],
                mime=_dl["mime"],
                key=f"dl_{_i}_{_dl['name']}"
            )
        st.divider()

    # Historial de mensajes
    for _m in st.session_state.chat_msgs:
        _role    = _m.get("role", "assistant")
        _content = _m.get("content", [])
        if not isinstance(_content, list):
            _content = [{"type": "text", "text": str(_content)}]
        _texts      = [b["text"] for b in _content
                       if isinstance(b, dict) and b.get("type") == "text" and b.get("text", "").strip()]
        _tool_calls = [b for b in _content
                       if isinstance(b, dict) and b.get("type") == "tool_use"]
        if not _texts and not _tool_calls:
            continue
        if _role == "user":
            if _texts:
                with st.chat_message("user"):
                    st.markdown("\n".join(_texts))
        else:
            with st.chat_message("assistant", avatar="🤖"):
                for _tc in _tool_calls:
                    with st.expander(f"Tool: {_tc.get('name','tool')}", expanded=False):
                        st.json(_tc.get("input", {}))
                if _texts:
                    st.markdown("\n".join(_texts))

    # Quick reply buttons (opciones clickeables)
    if st.session_state.chat_qr:
        st.markdown("**Selecciona uno:**")
        _qr_cols = st.columns(min(len(st.session_state.chat_qr), 3))
        for _qi, _qr in enumerate(st.session_state.chat_qr):
            if _qr_cols[_qi % 3].button(
                _qr["label"], key=f"qr_{_qi}_{_qr['id']}", use_container_width=True
            ):
                _sel_msg = f"{_qr['label']} (ID {_qr['id']})"
                st.session_state.chat_qr = []
                with st.spinner("Pensando..."):
                    _run_agent(_sel_msg)
                st.rerun()

    # Input y upload
    st.divider()
    _cu1, _cu2 = st.columns([5, 1])
    with _cu1:
        _chat_upload = st.file_uploader(
            "Adjuntar", type=["pdf", "xlsx", "xls", "png", "jpg", "jpeg"],
            key=f"chat_up_{len(st.session_state.chat_msgs)}",
            label_visibility="collapsed")
    with _cu2:
        if st.button("Nueva", key="chat_new_btn", use_container_width=True):
            st.session_state.chat_msgs = []
            st.session_state.chat_dl   = []
            st.rerun()

    _chat_in = st.chat_input("Pregunta algo, ej: Facturas pendientes de PETDUR / Descargame la ultima factura de Castillo")

    if _chat_in or _chat_upload:
        _fblocks = []
        if _chat_upload:
            _fb = _chat_upload.read()
            _fn = _chat_upload.name.lower()
            if _fn.endswith(".pdf"):
                _fblocks.append({"type": "document", "source": {
                    "type": "base64", "media_type": "application/pdf",
                    "data": _b64c.b64encode(_fb).decode()}})
            elif _fn.endswith((".png", ".jpg", ".jpeg")):
                _mime2 = "image/png" if _fn.endswith(".png") else "image/jpeg"
                _fblocks.append({"type": "image", "source": {
                    "type": "base64", "media_type": _mime2,
                    "data": _b64c.b64encode(_fb).decode()}})
            else:
                _fblocks.append({"type": "text", "text": f"[Archivo adjunto: {_chat_upload.name}]"})
        _user_text = _chat_in if _chat_in else (
            f"Analizá este documento adjunto ({_chat_upload.name}) y extrae toda la informacion relevante."
            if _chat_upload else "")
        if not _user_text:
            st.rerun()
        _spin_msg = "Analizando documento..." if (not _chat_in and _chat_upload) else "Pensando..."
        with st.spinner(_spin_msg):
            _run_agent(_user_text, _fblocks or None)
        st.rerun()


    pass  # end render
