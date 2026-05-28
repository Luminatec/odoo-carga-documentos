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

    # ── Log de errores/warnings de la sesion ───────────────────────────────
    _entries = get_odoo_error_log()
    if _entries:
        st.divider()
        _n_err  = sum(1 for e in _entries if e.get("nivel", "ERROR") == "ERROR")
        _n_warn = sum(1 for e in _entries if e.get("nivel", "ERROR") == "WARNING")
        _label  = []
        if _n_err:  _label.append(f"{_n_err} error(es)")
        if _n_warn: _label.append(f"{_n_warn} advertencia(s)")
        st.subheader(f"Log de sesion — {', '.join(_label)}")
        st.caption("Eventos registrados al interactuar con Odoo. Util para diagnostico.")

        import pandas as _pd_err
        _edf = _pd_err.DataFrame(_entries)

        # Normalizar columnas (nivel puede no existir en entradas antiguas)
        if "nivel" not in _edf.columns:
            _edf["nivel"] = "ERROR"
        _col_order = [c for c in ["ts", "nivel", "context", "error"] if c in _edf.columns]
        _edf = _edf[_col_order].copy()
        _edf.columns = {
            "ts":      "Hora",
            "nivel":   "Nivel",
            "context": "Operacion",
            "error":   "Detalle",
        }.get

        # Renombrar con map seguro
        _rename = {"ts": "Hora", "nivel": "Nivel", "context": "Operacion", "error": "Detalle"}
        _edf = _edf.rename(columns=_rename)

        # Icono por nivel
        if "Nivel" in _edf.columns:
            _edf["Nivel"] = _edf["Nivel"].map(
                lambda v: "🔴 ERROR" if v == "ERROR" else "🟡 WARN")

        st.dataframe(
            _edf,
            use_container_width=True,
            hide_index=True,
            column_config={
                "Nivel":    st.column_config.TextColumn("Nivel",    width="small"),
                "Hora":     st.column_config.TextColumn("Hora",     width="small"),
                "Operacion":st.column_config.TextColumn("Operacion",width="medium"),
                "Detalle":  st.column_config.TextColumn("Detalle",  width="large"),
            },
        )

        if st.button("🗑️ Limpiar log", key="historial_clear_log"):
            st.session_state["error_log"] = []
            st.rerun()
