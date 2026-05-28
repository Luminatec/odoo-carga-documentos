"""Tab Historial."""
import streamlit as st
import config as _cfg
from odoo_client import get_odoo_error_log


def render(models, uid, api_key, models_url, is_admin):
    st.subheader("Historial de esta sesion")

    # ── Documentos procesados ───────────────────────────────────────────────
    _hist = st.session_state.get("history", [])
    if not _hist:
        st.caption("Todavia no se proceso ningun documento en esta sesion.")
    else:
        import pandas as _pd_hist
        _hdf = _pd_hist.DataFrame(_hist)
        _hcols = [c for c in ["hora", "tipo", "archivo", "estado", "id", "url"] if c in _hdf.columns]
        _hdf_disp = _hdf[_hcols].copy()
        if "url" in _hdf_disp.columns:
            _hdf_disp["url"] = _hdf_disp["url"].apply(
                lambda u: "[Abrir](" + u + ")" if u else "")
        st.dataframe(_hdf_disp, use_container_width=True, hide_index=True)

    # ── Exportar a Excel ───────────────────────────────────────────────────
    _errors = get_odoo_error_log()
    if _hist or _errors:
        st.divider()
        if st.button("📥 Exportar historial a Excel", key="historial_export"):
            import io as _io
            import pandas as _pd_exp
            _buf = _io.BytesIO()
            with _pd_exp.ExcelWriter(_buf, engine="openpyxl") as _writer:
                # Hoja 1: documentos procesados
                if _hist:
                    _exp_df = _pd_exp.DataFrame(_hist)
                    _exp_cols = [c for c in ["hora","tipo","archivo","estado","id","url"] if c in _exp_df.columns]
                    _exp_df[_exp_cols].to_excel(_writer, sheet_name="Documentos", index=False)
                else:
                    _pd_exp.DataFrame([{"info":"Sin documentos procesados"}]).to_excel(
                        _writer, sheet_name="Documentos", index=False)
                # Hoja 2: log de errores
                if _errors:
                    _err_df = _pd_exp.DataFrame(_errors)
                    _rename = {"ts":"Hora","nivel":"Nivel","context":"Operacion","error":"Detalle"}
                    _err_df = _err_df.rename(columns=_rename)
                    if "Nivel" not in _err_df.columns:
                        _err_df["Nivel"] = "ERROR"
                    _err_df.to_excel(_writer, sheet_name="Log de errores", index=False)
            _buf.seek(0)
            from datetime import datetime as _dt
            _fname = f"historial_{_dt.now().strftime('%Y%m%d_%H%M')}.xlsx"
            st.download_button(
                label="⬇️ Descargar Excel",
                data=_buf.getvalue(),
                file_name=_fname,
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                key="historial_download",
            )

    # ── Log de errores/warnings de la sesion ───────────────────────────────
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
