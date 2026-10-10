"""Última barrera antes de enviar nada a un modelo.

Nunca viajan IBANs, referencias largas ni saldos con precisión sospechosa.
Si el state huele a dato crudo, se aborta la llamada: mejor un error local
que un dato personal en un modelo.
"""

import re


class AnonymizeError(ValueError):
    """El state contiene algo que no debe salir de la máquina."""


# IBAN con o sin espacios (ES66 0081 5700 23 4567890123 / ES6600815700234567890123).
_IBAN = re.compile(r"\b[A-Z]{2}\d{2}[ ]?(?:[0-9A-Z][ ]?){14,24}\b")
# Cualquier secuencia de 8+ dígitos seguidos (PANS, contratos, cuentas).
_LONG_DIGITS = re.compile(r"\d{8,}")
# Grupos de 4 dígitos repetidos (números de tarjeta separados).
_DIGIT_GROUPS = re.compile(r"(?:\d{4}[ ]){3,}")
# Importes con más de dos decimales: saldos o calculos crudos colados.
_MANY_DECIMALS = re.compile(r"\d+\.\d{3,}")

MAX_STATE_CHARS = 3500


def assert_safe_state(state: str) -> str:
    """Valida el texto que va a un modelo. El error describe el problema,
    nunca reproduce el contenido (para no filtrarlo a logs ni pantallas)."""
    if _IBAN.search(state):
        raise AnonymizeError("el state contiene algo con forma de IBAN")
    if _LONG_DIGITS.search(state):
        raise AnonymizeError("el state contiene una secuencia de 8+ digitos")
    if _DIGIT_GROUPS.search(state):
        raise AnonymizeError("el state contiene grupos de digitos tipo tarjeta")
    if _MANY_DECIMALS.search(state):
        raise AnonymizeError("el state contiene importes con mas de dos decimales")
    if len(state) > MAX_STATE_CHARS:
        raise AnonymizeError(f"el state supera {MAX_STATE_CHARS} caracteres")
    return state
