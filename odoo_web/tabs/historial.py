"""Tab Historial."""
import streamlit as st
import config as _cfg
from odoo_client import get_odoo_error_log
from user_prefs import append_persistent_history, load_persistent_history


def render(models, uid, api_key, models_url, is_admin):
    st.subheader("Historial de esta sesion")

    # ── Persistir entradas nuevas de esta sesion ────────────────────────────
    _hist = st.session_state.get("history", [])
    _persisted_set = st.session_state.setdefault("_persisted_hist_ids", set())
    from datetime import date as _hist_date
    for _he in _hist:
        _hkey = f"{_he.get('tipo','')}|{_he.get('id','')}|{_he.get('hora','')}"
        if _hkey not in _persisted_set:
            append_persistent_history({
                **_he,
                "fecha": str(_hist_date.today()),
            })
            _persisted_set.add(_hkey)

    # ── Documentos creados en Odoo ─────────────────────────────────────────
    
    if not _hist:
        st.caption("Todavia no se creo ningun documento en Odoo en esta sesion.")
    else:
        import pandas as _pd_hist

        _hdf = _pd_hist.DataFrame(_hist)

        # ── Filtros ────────────────────────────────────────────────────────
        _h_tipos = sorted(_hdf["tipo"].unique().tolist()) if "tipo" in _hdf.columns else []
        _hf1, _hf2 = st.columns([2, 3])
        _h_tipo_sel = _hf1.multiselect(
            "Filtrar por tipo", _h_tipos, default=[],
            key="hist_tipo_filter", placeholder="Todos los tipos")
        _h_buscar = _hf2.text_input(
            "🔍 Buscar", key="hist_search",
            placeholder="archivo, tipo, ID…")

        # Aplicar filtros
        _hdf_f = _hdf.copy()
        if _h_tipo_sel:
            _hdf_f = _hdf_f[_hdf_f["tipo"].isin(_h_tipo_sel)]
        if _h_buscar.strip():
            _q = _h_buscar.strip().lower()
            _mask = _pd_hist.Series([False] * len(_hdf_f), index=_hdf_f.index)
            for _col in ["archivo", "tipo", "estado"]:
                if _col in _hdf_f.columns:
                    _mask |= _hdf_f[_col].astype(str).str.lower().str.contains(_q, na=False)
            _hdf_f = _hdf_f[_mask]

        _hcols = [c for c in ["hora", "tipo", "archivo", "estado", "id", "url"] if c in _hdf_f.columns]
        _hdf_disp = _hdf_f[_hcols].copy()
        if "url" in _hdf_disp.columns:
            _hdf_disp["url"] = _hdf_disp["url"].apply(
                lambda u: "[Abrir](" + u + ")" if u else "")

        _n_total = len(_hdf)
        _n_shown = len(_hdf_disp)
        if _n_shown < _n_total:
            st.caption(f"Mostrando {_n_shown} de {_n_total} documento(s).")

        st.dataframe(_hdf_disp, use_container_width=True, hide_index=True)

    # ── Archivos subidos esta sesion ───────────────────────────────────────
    _proc = st.session_state.get("processed_files", {})
    if _proc:
        st.divider()
        st.subheader(f"Archivos procesados ({len(_proc)})")
        st.caption("Archivos subidos y procesados exitosamente en esta sesion.")
        import pandas as _pd_proc
        _prows = [
            {"Hora": v["hora"], "Tipo": v["tipo"],
             "Archivo": v["filename"], "Resultado": v["resultado"]}
            for v in _proc.values()
        ]
        st.dataframe(_pd_proc.DataFrame(_prows), use_container_width=True, hide_index=True)

    # ── Exportar a Excel ───────────────────────────────────────────────────
    _errors = get_odoo_error_log()
    if _hist or _errors:
        st.divider()
        if st.button("📥 Exportar historial a Excel", key="historial_export"):
            import io as _io
            import pandas as _pd_exp
            from zoneinfo import ZoneInfo as _ZI
            from datetime import datetime as _dt
            _buf = _io.BytesIO()
            with _pd_exp.ExcelWriter(_buf, engine="openpyxl") as _writer:
                if _hist:
                    _exp_df = _pd_exp.DataFrame(_hist)
                    _exp_cols = [c for c in ["hora","tipo","archivo","estado","id","url"] if c in _exp_df.columns]
                    _exp_df[_exp_cols].to_excel(_writer, sheet_name="Documentos", index=False)
                else:
                    _pd_exp.DataFrame([{"info":"Sin documentos procesados"}]).to_excel(
                        _writer, sheet_name="Documentos", index=False)
                if _errors:
                    _err_df = _pd_exp.DataFrame(_errors)
                    _rename = {"ts":"Hora","nivel":"Nivel","context":"Operacion","error":"Detalle"}
                    _err_df = _err_df.rename(columns=_rename)
                    if "Nivel" not in _err_df.columns:
                        _err_df["Nivel"] = "ERROR"
                    _err_df.to_excel(_writer, sheet_name="Log de errores", index=False)
            _buf.seek(0)
            _fname = f"historial_{_dt.now(_ZI('America/Argentina/Buenos_Aires')).strftime('%Y%m%d_%H%M')}.xlsx"
            st.download_button(
                label="⬇️ Descargar Excel",
                data=_buf.getvalue(),
                file_name=_fname,
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                key="historial_download",
            )

    # ── Log de errores/warnings de la sesion ──────────────────────────────
    if _errors:
        st.divider()
        _n_err  = sum(1 for e in _errors if e.get("nivel", "ERROR") == "ERROR")
        _n_warn = sum(1 for e in _errors if e.get("nivel", "ERROR") == "WARNING")
        _label  = []
        if _n_err:  _label.append(f"{_n_err} error(es)")
        if _n_warn: _label.append(f"{_n_warn} advertencia(s)")
        st.subheader(f"Log de sesion — {', '.join(_label)}")
        st.caption("Eventos registrados al interactuar con Odoo. Util para diagnostico.")

        import pandas as _pd_err
        _edf = _pd_err.DataFrame(_errors)
        if "nivel" not in _edf.columns:
            _edf["nivel"] = "ERROR"
        _col_order = [c for c in ["ts", "nivel", "context", "error"] if c in _edf.columns]
        _edf = _edf[_col_order].rename(columns={
            "ts": "Hora", "nivel": "Nivel", "context": "Operacion", "error": "Detalle"})
        _edf["Nivel"] = _edf["Nivel"].map(
            lambda v: "🔴 ERROR" if v == "ERROR" else "🟡 WARN")

        st.dataframe(
            _edf,
            use_container_width=True,
            hide_index=True,
            column_config={
                "Nivel":     st.column_config.TextColumn("Nivel",     width="small"),
                "Hora":      st.column_config.TextColumn("Hora",      width="small"),
                "Operacion": st.column_config.TextColumn("Operacion", width="medium"),
                "Detalle":   st.column_config.TextColumn("Detalle",   width="large"),
            },
        )
        if st.button("🗑️ Limpiar log", key="historial_clear_log"):
            st.session_state["error_log"] = []
            st.rerun()

    # ── Historial persistente entre sesiones ──────────────────────────────
    st.divider()
    with st.expander("📋 Historial de sesiones anteriores", expanded=False):
        _ph_all = load_persistent_history(limit=300)
        if not _ph_all:
            st.caption("Todavía no hay historial guardado de sesiones anteriores.")
        else:
            import pandas as _pd_ph
            # Filtros
            _ph_tipos = sorted(set(e.get("tipo","") for e in _ph_all if e.get("tipo")))
            _phf1, _phf2, _phf3 = st.columns([2, 1, 1])
            _ph_tipo_sel = _phf1.multiselect(
                "Tipo", _ph_tipos, default=[], key="ph_tipo_filter",
                placeholder="Todos")
            _ph_desde = _phf2.date_input("Desde", value=None, key="ph_desde")
            _ph_hasta = _phf3.date_input("Hasta", value=None, key="ph_hasta")

            _ph_filtered = _ph_all
            if _ph_tipo_sel:
                _ph_filtered = [e for e in _ph_filtered if e.get("tipo") in _ph_tipo_sel]
            if _ph_desde:
                _ph_filtered = [e for e in _ph_filtered
                                if e.get("fecha","") >= str(_ph_desde)]
            if _ph_hasta:
                _ph_filtered = [e for e in _ph_filtered
                                if e.get("fecha","") <= str(_ph_hasta)]

            _ph_df = _pd_ph.DataFrame([{
                "Fecha":    e.get("fecha",""),
                "Hora":     e.get("hora",""),
                "Tipo":     e.get("tipo",""),
                "Archivo":  e.get("archivo",""),
                "Estado":   e.get("estado",""),
                "Link":     ("[Abrir](" + e["url"] + ")" if e.get("url") else ""),
            } for e in reversed(_ph_filtered)])

            if _ph_df.empty:
                st.caption("Sin entradas para los filtros seleccionados.")
            else:
                st.caption(f"{len(_ph_df)} entrada(s) — {len(_ph_all)} total en historial.")
                st.dataframe(_ph_df, use_container_width=True, hide_index=True)

                if st.button("📥 Exportar historial completo a Excel", key="ph_export"):
                    import io as _io2
                    _ph_buf = _io2.BytesIO()
                    _pd_ph.DataFrame([{
                        "Fecha": e.get("fecha",""), "Hora": e.get("hora",""),
                        "Tipo": e.get("tipo",""), "Archivo": e.get("archivo",""),
                        "ID Odoo": e.get("id",""), "Estado": e.get("estado",""),
                        "URL": e.get("url",""),
                    } for e in reversed(_ph_all)]).to_excel(_ph_buf, index=False)
                    _ph_buf.seek(0)
                    st.download_button(
                        "⬇️ Descargar Excel completo",
                        data=_ph_buf.getvalue(),
                        file_name=f"historial_completo_{str(_hist_date.today())}.xlsx",
                        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        key="ph_dl")
