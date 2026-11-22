"""Acceso con usuario y contraseña a la web.

Se activa solo si están `MOOONEY_USER` y `MOOONEY_PASSWORD` (entorno o `data/.env`); sin ellas la web
queda abierta, que es lo razonable en 127.0.0.1. La sesión es una cookie firmada con HMAC cuya clave sale
de las credenciales: no hay tabla de sesiones y cambiar la contraseña cierra todas.
"""

import hashlib
import hmac
import os
import time

COOKIE = "moooney_session"
DAYS = 30
Creds = tuple[str, str]


def credentials() -> Creds | None:
    user, password = os.environ.get("MOOONEY_USER", "").strip(), os.environ.get("MOOONEY_PASSWORD", "")
    return (user, password) if user and password else None


def _sign(creds: Creds, exp: str) -> str:
    key = hashlib.sha256("\0".join(("moooney-session", *creds)).encode()).digest()
    return hmac.new(key, exp.encode(), "sha256").hexdigest()


def make_token(creds: Creds, now: float | None = None) -> str:
    exp = str(int((now or time.time()) + DAYS * 86400))
    return f"{exp}.{_sign(creds, exp)}"


def valid_token(creds: Creds, token: str | None, now: float | None = None) -> bool:
    exp, _, sig = (token or "").partition(".")
    return exp.isdigit() and int(exp) > (now or time.time()) and hmac.compare_digest(sig, _sign(creds, exp))


def check(creds: Creds, user: str, password: str) -> bool:
    # las dos comparaciones siempre, en tiempo constante: no se filtra cuál de las dos falla
    ok_user = hmac.compare_digest(user.strip().encode(), creds[0].encode())
    ok_pass = hmac.compare_digest(password.encode(), creds[1].encode())
    return ok_user and ok_pass


def safe_next(target: str | None) -> str:
    """Solo rutas de esta web: nada de `//otro.sitio` ni URL absolutas tras entrar."""
    return target if target and target.startswith("/") and not target.startswith(("//", "/\\")) else "/"
