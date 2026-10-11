"""Normalización determinista de conceptos a claves de comercio.

clean_concept convierte el concepto libre del banco en una clave estable:
la misma tienda, con distinto terminal o ciudad, debe dar la misma clave.
Los nombres de personas en Bizum y transferencias se eliminan; la clave se
queda en "BIZUM ENVIADO" o "TRANSFERENCIA A PARTICULAR".
"""

import re
import unicodedata

# Prefijos de operación que no identifican al comercio. Se prueban en orden
# (los más largos primero) y se eliminan repetidamente.
_OPERATION_PREFIXES = [
    "TRANSACCION CONTACTLESS EN",
    "TRANSACCION CONTACTLESS",
    "PAGO MOVIL EN",
    "PAGO POR MOVIL EN",
    "PAGO MOVIL",
    "COMPRA POR INTERNET EN",
    "COMPRA INTERNET EN",
    "COMPRA EN LINEA DE",
    "COMPRA EN ESTABLECIMIENTO DE",
    "COMPRA EN",
    "COMPRA DE",
    "COMPRA",
    "PAGO EN",
    "PAGO DE",
    "PAGO",
    "RECIBO DE",
    "RECIBO",
    "ADEUDO DE",
    "ADEUDO",
    "RECARGO DE",
    "RECARGO",
    # Procesadores de pago que anteponen su marca al comercio
    "SUMUP",
    "ZETTLE",
    "ZTL",
]

# Palabras que introducen datos del recibo (no del comercio): se corta ahí.
_CUT_MARKERS = re.compile(
    r"\b(CONCEPTO|TARJETA|TARJ|COMISION|CIF|POLIZA|RAMO|REF|RECIBO)\b"
)

# Marcadores fiscales que sobreviven al borrado de nombres en transferencias.
_AEAT = re.compile(r"\b(AEAT|DEVOLUCIONES? TRIBUTARIAS|HACIENDA)\b")

FALLBACK_KEY = "SIN CLAVE"


def _upper_sin_acentos(texto: str) -> str:
    nfkd = unicodedata.normalize("NFKD", texto.upper())
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def _strip_prefixes(texto: str) -> str:
    changed = True
    while changed:
        changed = False
        for prefix in _OPERATION_PREFIXES:
            if texto == prefix or texto.startswith(prefix + " "):
                texto = texto[len(prefix) :].strip()
                changed = True
                break
    return texto


def _tokeniza(texto: str) -> str:
    """Solo letras, sin tokens con dígitos (PANS, IDs, terminales, años)."""
    texto = re.sub(r"[^A-Z0-9 ]+", " ", texto)
    tokens = [t for t in texto.split() if not any(c.isdigit() for c in t)]
    return " ".join(tokens)


def _primera_clave_de(resto: str) -> str:
    """Primer segmento (antes de ciudad o de marcadores del recibo)."""
    head = resto.split(",")[0]
    match = _CUT_MARKERS.search(head)
    if match and match.start() > 0:
        # el marcador separa comercio y datos del recibo; si va al principio
        # ("COMISION MANTENIMIENTO"), es el propio concepto y se conserva
        head = head[: match.start()]
    return head


def _family_key(texto: str, original: str) -> str | None:
    """Claves fijas para familias estructurales del banco."""
    if texto.startswith("TRANSFERENCIA"):
        sufijo = " AEAT" if _AEAT.search(original) else ""
        if "HUCHA" in texto:
            return "TRASPASO HUCHA"
        if "A FAVOR DE" in texto or texto.startswith("TRANSFERENCIA ENVIADA"):
            return f"TRANSFERENCIA A PARTICULAR{sufijo}"
        return f"TRANSFERENCIA DE PARTICULAR{sufijo}"
    if texto.startswith("TRASPASO"):
        return "TRASPASO HUCHA" if "HUCHA" in texto else "TRASPASO INTERNO"
    if texto.startswith("BIZUM"):
        resto = texto[len("BIZUM") :].strip()
        # "COMPRA/PAGO BIZUM <comercio>" es un pago a un comercio, no a una persona.
        if original.startswith(("COMPRA BIZUM", "PAGO BIZUM")) and resto:
            comercio = _tokeniza(_primera_clave_de(resto))
            if comercio:
                return f"BIZUM ENVIADO {comercio}"
        if resto.startswith(("A FAVOR DE", "ENVIADO", "A ")):
            return "BIZUM ENVIADO"
        if resto.startswith("DE ") or resto.startswith("RECIBIDO"):
            return "BIZUM RECIBIDO"
        return "BIZUM ENVIADO"
    if texto.startswith("LIQUIDACION") and "TARJETAS DE CREDITO" in texto:
        return "LIQUIDACION DE TARJETAS DE CREDITO"
    if texto.startswith("RETIRADA DE EFECTIVO") or texto.startswith("REINTEGRO"):
        return "RETIRADA DE EFECTIVO EN CAJERO"
    if texto.startswith("INGRESO EN CAJERO"):
        return "INGRESO EN CAJERO"
    return None


def clean_concept(concept: str) -> str:
    """Devuelve la clave de comercio estable para un concepto del banco."""
    original = re.sub(r"\s+", " ", _upper_sin_acentos(concept or "")).strip()
    if not original:
        return FALLBACK_KEY

    texto = original
    devolucion = False
    for marker in ("DEVOLUCION DE", "DEVOLUCION"):
        if texto.startswith(marker):
            devolucion = True
            texto = texto[len(marker) :].strip()
            break

    if re.search(r"\bNOMINA\b", original) and not devolucion:
        return "NOMINA"

    texto = _strip_prefixes(texto)
    if not texto:
        return _con_devolucion("SIN CLAVE", devolucion)

    clave = _family_key(texto, original)
    if clave is None:
        clave = _tokeniza(_primera_clave_de(texto)) or FALLBACK_KEY
    return _con_devolucion(clave, devolucion)


def _con_devolucion(clave: str, devolucion: bool) -> str:
    return f"DEVOLUCION {clave}" if devolucion else clave
