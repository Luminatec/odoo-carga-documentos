"""
Preferencias de usuario — persiste entre sesiones mediante JSON.

Las preferencias se guardan en user_prefs.json en el directorio de trabajo
de la app. En Streamlit Cloud el archivo sobrevive mientras el proceso esté
activo; en una instalación local persiste indefinidamente.

Uso:
    from user_prefs import load_prefs, save_prefs

    prefs = load_prefs()
    diario_id = prefs.get("diario_cobros_nombre", "")
    save_prefs({**prefs, "referido_nombre": "Juan Pérez"})
"""

import json
import logging
import streamlit as st

_logger = logging.getLogger("lumidoo.user_prefs")
_PREFS_FILE = "user_prefs.json"

_DEFAULTS: dict = {
    "diario_cobros_nombre": "",   # nombre del diario de cobros preferido
    "referido_nombre":      "",   # nombre del ejecutivo / referido por defecto
    "plazo_pago_nombre":    "",   # nombre del término de pago preferido
    "diario_facturas_nombre": "",  # nombre del diario de facturas de proveedor preferido
}


def load_prefs() -> dict:
    """Carga preferencias desde session_state (cache) o desde el archivo JSON."""
    if "user_prefs" in st.session_state:
        return st.session_state["user_prefs"]
    try:
        with open(_PREFS_FILE, encoding="utf-8") as f:
            stored = json.load(f)
        prefs = {**_DEFAULTS, **stored}
    except (FileNotFoundError, json.JSONDecodeError):
        prefs = dict(_DEFAULTS)
    st.session_state["user_prefs"] = prefs
    return prefs


def save_prefs(prefs: dict) -> None:
    """Guarda preferencias en session_state y en el archivo JSON."""
    merged = {**_DEFAULTS, **prefs}
    st.session_state["user_prefs"] = merged
    try:
        with open(_PREFS_FILE, "w", encoding="utf-8") as f:
            json.dump(merged, f, indent=2, ensure_ascii=False)
        _logger.info("Preferencias guardadas: %s", merged)
    except OSError as e:
        _logger.warning("No se pudo escribir user_prefs.json: %s", e)


# ── Memoria de cuenta contable por proveedor ────────────────────────────────
_VENDOR_ACCTS_FILE = "vendor_accounts.json"


def load_vendor_account_pref(partner_id: int) -> dict:
    """Retorna {"account_id": int, "account_label": str} o {} si no hay guardado."""
    try:
        with open(_VENDOR_ACCTS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data.get(str(partner_id), {})
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_vendor_account_pref(partner_id: int, account_id: int, account_label: str) -> None:
    """Guarda la última cuenta contable usada para un proveedor."""
    try:
        try:
            with open(_VENDOR_ACCTS_FILE, encoding="utf-8") as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            data = {}
        data[str(partner_id)] = {
            "account_id":    account_id,
            "account_label": account_label,
        }
        with open(_VENDOR_ACCTS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except OSError as e:
        _logger.warning("No se pudo escribir vendor_accounts.json: %s", e)
