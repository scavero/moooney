"""Construcción de la consulta externa a partir de una clave de comercio.

La consulta es lo único que sale fuera de la máquina y de ClusterTIC: solo la
clave limpia (sin dígitos ni palabras de la operación). Nunca importes, fechas,
frecuencias ni saldos. No se añade ninguna región al texto: el comercio puede
estar en Sevilla, en Alemania o ser una tienda online; situar la búsqueda es
cosa de cada proveedor (Nominatim acota por país con ``countrycodes``).
"""

import re

from moooney.llm.anonymize import assert_safe_state

# Palabras de la operación bancaria o del procesador de pago que no identifican al comercio.
_GENERIC_TOKENS = {
    "COMPRA", "PAGO", "TARJETA", "ADEUDO", "RECIBO", "MOVIL", "INTERNET", "EN", "DE", "DEL",
    "LA", "EL", "LOS", "LAS", "A", "Y", "POR", "CON", "SL", "SLU", "SA", "SAU", "SCOOP",
    "ESPANA", "ESPAÑA", "ES", "EUR", "WWW", "COM", "HTTP", "HTTPS",
    "TRANSACCION", "CONTACTLESS", "CONTACTLES", "TPV", "SUMUP", "ZETTLE", "STRIPE", "PAY",
}

# Nombres de pila frecuentes: si la clave empieza por uno, probablemente es una
# persona (autónomo, bizum a un comercio pequeño) y no se consulta fuera.
_FIRST_NAMES = {
    "ADRIAN", "ALBA", "ALBERTO", "ALEJANDRO", "ALFONSO", "ALICIA", "ALVARO", "ANA", "ANDREA",
    "ANDRES", "ANGEL", "ANGELA", "ANTONIO", "ANXO", "BEATRIZ", "BELEN", "BRAIS", "CARLA",
    "CARLOS", "CARMEN", "CELIA", "CESAR", "CLARA", "CRISTINA", "DANIEL", "DAVID", "DIEGO",
    "ELENA", "EMILIO", "ENRIQUE", "EVA", "FERNANDO", "FRANCISCO", "GABRIEL", "GERARDO",
    "GONZALO", "GUILLERMO", "HECTOR", "HUGO", "IAGO", "IGNACIO", "INES", "IRENE", "ISABEL",
    "IVAN", "JAIME", "JAVIER", "JESUS", "JOAQUIN", "JORGE", "JOSE", "JUAN", "JULIA", "JULIO",
    "LAURA", "LORENA", "LUCIA", "LUIS", "MANUEL", "MANUELA", "MARCOS", "MARIA", "MARTA",
    "MARTIN", "MIGUEL", "MONICA", "NATALIA", "NEREA", "NOELIA", "NOA", "NURIA", "OSCAR",
    "PABLO", "PATRICIA", "PAULA", "PEDRO", "PILAR", "RAFAEL", "RAMON", "RAQUEL", "RAUL",
    "ROBERTO", "ROCIO", "ROSA", "RUBEN", "SANDRA", "SARA", "SERGIO", "SILVIA", "SOFIA",
    "SUSANA", "TERESA", "UXIA", "VICTOR", "VICENTE", "XABIER", "XOAN", "XOSE", "YOLANDA",
}

# Dígitos sueltos o pegados a letras, y todo lo que no sea letra/espacio.
_NON_LETTERS = re.compile(r"[^A-ZÑ ]+")
_MULTISPACE = re.compile(r"\s+")

MIN_TOKENS = 2
# Un último token de 1-2 letras suele ser el principio de una ciudad truncada por
# el banco ("HOFF BRAND SEVI" → SEVI(lla), "SPRINTER VI" → VI(go)): se descarta.
MAX_TRAILING_FRAGMENT = 2


def is_skipped(key: str, skip_patterns: list[str]) -> bool:
    """La clave casa con alguno de los patrones vetados (no se consulta)."""
    return any(re.search(p, key) for p in skip_patterns)


def looks_like_person(tokens: list[str]) -> bool:
    """Empieza por un nombre de pila y tiene pinta de nombre y apellidos."""
    return len(tokens) >= 2 and tokens[0] in _FIRST_NAMES


def build_query(key: str, *, skip_patterns: list[str] | None = None) -> str | None:
    """Consulta de búsqueda para una clave, o None si no merece/puede consultarse."""
    if skip_patterns and is_skipped(key, skip_patterns):
        return None
    texto = _NON_LETTERS.sub(" ", key.upper())
    tokens = [t for t in _MULTISPACE.split(texto) if t]
    tokens = [t for t in tokens if len(t) > 1 and t not in _GENERIC_TOKENS]
    if looks_like_person(tokens):
        return None
    if len(tokens) > MIN_TOKENS and len(tokens[-1]) <= MAX_TRAILING_FRAGMENT:
        tokens = tokens[:-1]
    if len(tokens) < MIN_TOKENS:
        return None
    # Misma barrera que un state de Clef: si algo huele a dato crudo, no sale.
    return assert_safe_state(" ".join(tokens))


_DIGIT_RUNS = re.compile(r"\d{4,}")
_PHONE_LIKE = re.compile(r"(?:\+?\d[\d .-]{6,}\d)")


def sanitize_summary(text: str, max_chars: int) -> str:
    """Resumen externo apto para volver a Clef: sin teléfonos, sin referencias
    largas, sin saltos de línea y acotado en longitud."""
    text = _PHONE_LIKE.sub(" ", text)
    text = _DIGIT_RUNS.sub(" ", text)
    text = _MULTISPACE.sub(" ", text).strip()
    return text[:max_chars]
