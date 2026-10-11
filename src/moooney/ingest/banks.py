"""Perfiles de banco: cómo se compone el concepto a partir de las celdas del Excel.

Cada banco exporta distinto. El resto de la pipeline (claves de comercio, tipo de
operación, reglas) está calibrado con los conceptos del Santander, que llevan el tipo
de operación como prefijo ("COMPRA EN …", "RECIBO …", "BIZUM A FAVOR DE …"). Para
otros bancos, el constructor de concepto reescribe sus celdas a esa misma forma, así
que no hace falta tocar normalización ni reglas.

Los constructores reciben dos celdas: el concepto y, si el banco la trae, la columna
de tipo de movimiento. Nunca reciben la columna de observaciones (en el BBVA lleva el
número de tarjeta) porque el parser ni siquiera la mapea.
"""

import re
import unicodedata
from collections.abc import Callable


def clean_concept(value: object) -> str:
    s = str(value or "").upper().replace(" ", " ")
    return re.sub(r"\s+", " ", s).strip()


def _sin_acentos(s: str) -> str:
    nfkd = unicodedata.normalize("NFKD", s)
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def plain(concept: object, operation: object = None) -> str:
    """Santander: el concepto ya lleva el tipo de operación delante."""
    return clean_concept(concept)


def bbva(concept: object, operation: object = None) -> str:
    """BBVA: "Concepto" + "Movimiento" → concepto con el tipo de operación como prefijo.

    - Pago con tarjeta → "COMPRA EN <comercio>"
    - Concepto "Bizum", Movimiento "Enviado: nota" → "BIZUM ENVIADO: nota" (la clave es
      BIZUM ENVIADO / BIZUM RECIBIDO, sin la nota)
    - "Adeudo <emisor>" → "RECIBO <emisor>"
    - Retirada en cajero → "RETIRADA DE EFECTIVO EN CAJERO"
    - Transferencia realizada/recibida + Movimiento → "TRANSFERENCIA ENVIADA/RECIBIDA <texto>"
    - Resto: "<concepto> <movimiento>"
    """
    # Sin acentos: el BBVA escribe «Nómina»; los conceptos nuevos no tienen huellas previas.
    c = _sin_acentos(clean_concept(concept))
    m = _sin_acentos(clean_concept(operation))
    if m.startswith("PAGO CON TARJETA"):
        return f"COMPRA EN {c}".strip()
    if c == "BIZUM" or c.startswith("BIZUM "):
        return f"BIZUM {m}".strip() if m else c
    if c.startswith("ADEUDO"):
        return f"RECIBO {c[len('ADEUDO'):].strip()}".strip()
    if c.startswith("RET. EFECTIVO") or c.startswith("RETIRADA") or c.startswith("REINTEGRO"):
        return "RETIRADA DE EFECTIVO EN CAJERO"
    if c.startswith("INGRESO EN CAJERO") or c.startswith("INGRESO EFECTIVO"):
        return "INGRESO EN CAJERO"
    if c.startswith("TRANSFERENCIA REALIZADA") or c.startswith("TRANSFERENCIA EMITIDA"):
        return f"TRANSFERENCIA ENVIADA {m}".strip()
    if c.startswith("TRANSFERENCIA RECIBIDA"):
        return f"TRANSFERENCIA RECIBIDA {m}".strip()
    if c.startswith("TRASPASO"):
        return f"{c} {m}".strip()
    if "NOMINA" in c or "NOMINA" in m:
        return f"NOMINA {m if 'NOMINA' not in m else ''}".strip()
    return f"{c} {m}".strip()


BUILDERS: dict[str, Callable[[object, object], str]] = {"plain": plain, "bbva": bbva}
