"""Construcción de la consulta externa a partir de una clave de comercio.

La consulta es lo único que sale fuera de la máquina y de ClusterTIC: solo la
clave limpia (sin dígitos ni palabras de la operación) más una pista regional.
Nunca importes, fechas, frecuencias ni saldos.
"""

import re

from moooney.llm.anonymize import assert_safe_state

# Palabras de la operación bancaria que no identifican al comercio.
_GENERIC_TOKENS = {
    "COMPRA", "PAGO", "TARJETA", "ADEUDO", "RECIBO", "MOVIL", "INTERNET", "EN", "DE", "DEL",
    "LA", "EL", "LOS", "LAS", "A", "Y", "POR", "CON", "SL", "SLU", "SA", "SAU", "SCOOP",
    "ESPANA", "ESPAÑA", "ES", "EUR", "WWW", "COM", "HTTP", "HTTPS",
}

# Ciudades y comarcas gallegas que ya sitúan la consulta: no se añade la región.
_KNOWN_PLACES = {
    "VIGO", "CORUNA", "A CORUNA", "SANTIAGO", "OURENSE", "LUGO", "PONTEVEDRA", "FERROL",
    "LALIN", "VILAGARCIA", "CANGAS", "REDONDELA", "PORRINO", "O PORRINO", "MARIN",
    "TUI", "OLEIROS", "CARBALLO", "NARON", "RIBEIRA", "AMES", "CULLEREDO", "ARTEIXO",
    "MOANA", "NIGRAN", "BAIONA", "GONDOMAR", "SANXENXO", "BOIRO", "NOIA", "BETANZOS",
    "SADA", "MONFORTE", "VERIN", "XINZO", "VIVEIRO", "BURELA", "ESTRADA", "A ESTRADA",
    "PADRON", "CAMBADOS", "O GROVE", "GROVE", "POIO", "BUEU", "SALCEDA", "PONTEAREAS",
    "MOS", "CALDAS", "SILLEDA", "CHANTADA", "SARRIA", "MADRID", "BARCELONA", "SEVILLA",
    "VALENCIA", "BILBAO", "OVIEDO", "GIJON", "LEON", "ZARAGOZA", "MALAGA", "LISBOA",
    "PORTO", "OPORTO", "BRAGA", "VIANA",
}

# Dígitos sueltos o pegados a letras, y todo lo que no sea letra/espacio.
_NON_LETTERS = re.compile(r"[^A-ZÑ ]+")
_MULTISPACE = re.compile(r"\s+")

MIN_TOKENS = 2


def is_skipped(key: str, skip_patterns: list[str]) -> bool:
    """La clave casa con alguno de los patrones vetados (no se consulta)."""
    return any(re.search(p, key) for p in skip_patterns)


def _has_known_place(tokens: list[str]) -> bool:
    padded = f" {' '.join(tokens)} "
    return any(f" {place} " in padded for place in _KNOWN_PLACES)


def build_query(key: str, *, region_hint: str, skip_patterns: list[str] | None = None) -> str | None:
    """Consulta de búsqueda para una clave, o None si no merece/puede consultarse."""
    if skip_patterns and is_skipped(key, skip_patterns):
        return None
    texto = _NON_LETTERS.sub(" ", key.upper())
    tokens = [t for t in _MULTISPACE.split(texto) if t]
    tokens = [t for t in tokens if len(t) > 1 and t not in _GENERIC_TOKENS]
    if len(tokens) < MIN_TOKENS:
        return None
    query = " ".join(tokens)
    if region_hint and not _has_known_place(tokens):
        query = f"{query} {region_hint}"
    # Misma barrera que un state de Clef: si algo huele a dato crudo, no sale.
    return assert_safe_state(query)


_DIGIT_RUNS = re.compile(r"\d{4,}")
_PHONE_LIKE = re.compile(r"(?:\+?\d[\d .-]{6,}\d)")


def sanitize_summary(text: str, max_chars: int) -> str:
    """Resumen externo apto para volver a Clef: sin teléfonos, sin referencias
    largas, sin saltos de línea y acotado en longitud."""
    text = _PHONE_LIKE.sub(" ", text)
    text = _DIGIT_RUNS.sub(" ", text)
    text = _MULTISPACE.sub(" ", text).strip()
    return text[:max_chars]
