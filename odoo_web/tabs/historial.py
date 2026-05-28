"""Tab Historial."""
import streamlit as st
import config as _cfg
from odoo_client import get_odoo_error_log


def render(models, uid, api_key, models_url, is_admin):
    st.subheader("Historial de esta sesion")

    # Documentos procesados
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

    # Log de errores de la sesion
    _errors = get_odoo_error_log()
    if _errors:
        st.divider()
        st.subheader("Errores registrados en esta sesion")
        st.caption("Errores que ocurrieron al interactuar con Odoo. Util para diagnostico.")
        import pandas as _pd_err
        _edf = _pd_err.DataFrame(_errors)
        _edf.columns = ["Hora", "Operacion", "Detalle"]
        st.dataframe(_edf, use_container_width=True, hide_index=True)
