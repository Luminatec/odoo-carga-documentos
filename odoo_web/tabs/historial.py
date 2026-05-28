"""Tab Historial."""
import streamlit as st
import config as _cfg
from odoo_client import *


def render(models, uid, api_key, models_url, is_admin):
    st.subheader("📋 Historial de esta sesión")
    _hist = st.session_state.get("history", [])
    if not _hist:
        st.caption("Todavía no se procesó ningún documento en esta sesión.")
    else:
        import pandas as _pd_hist
        _hdf = _pd_hist.DataFrame(_hist)
        _hcols = [c for c in ["hora","tipo","archivo","estado","id","url"] if c in _hdf.columns]
        _hdf_disp = _hdf[_hcols].copy()
        if "url" in _hdf_disp.columns:
            _hdf_disp["url"] = _hdf_disp["url"].apply(
                lambda u: f"[Abrir]({u})" if u else "")
        st.dataframe(_hdf_disp, use_container_width=True, hide_index=True)



    pass  # end render
