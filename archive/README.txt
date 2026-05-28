app_final.py — monolito original (~7000 lineas)
Refactorizado en fix187/188 hacia estructura modular:
  odoo_web/app.py          (entry point)
  odoo_web/config.py       (constantes)
  odoo_web/odoo_client.py  (helpers Odoo + cache)
  odoo_web/parsers.py      (parsing documentos)
  odoo_web/tabs/           (un render() por tab)

Este archivo se mantiene como referencia historica solamente.
NO editar — la fuente de verdad son los modulos en odoo_web/.
