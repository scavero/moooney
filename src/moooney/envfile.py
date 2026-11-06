"""Ficheros .env: las claves del proxy sin depender de variables del sistema.

Orden de precedencia, de más a menos: la variable ya definida en el entorno del proceso
(la de Windows o la que inyecta docker compose), después `<data>/.env` (lo que se guarda
desde la página Ajustes de la web) y por último `./.env` en la raíz del proyecto.
Solo se cargan los nombres de `MANAGED`: un .env nunca ejecuta nada ni expande variables.
Los valores no se registran ni se devuelven enteros a la interfaz; ver `hint`.
"""

import os
from pathlib import Path

MANAGED = ("LITELLM_API_KEY", "LITELLM_BASE_URL", "GOOGLE_PLACES_API_KEY", "MOOONEY_OFFLINE")
# Las que se pueden guardar desde la web. MOOONEY_OFFLINE tiene su propio interruptor.
SECRET_NAMES = ("LITELLM_API_KEY", "GOOGLE_PLACES_API_KEY")
SOURCE_ENV = "entorno"
CABECERA = "# Claves guardadas desde la web de moooney. Este fichero no entra en git ni en la imagen."


def parse_env(text: str) -> dict[str, str]:
    """`NOMBRE=valor` por línea; ignora comentarios y vacías; quita `export` y comillas."""
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        name, _, value = line.partition("=")
        name, value = name.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if name:
            out[name] = value
    return out


def data_env_path(data_dir: Path) -> Path:
    return data_dir / ".env"


def env_file_paths(data_dir: Path) -> list[Path]:
    """Ficheros que se consultan, en orden de precedencia."""
    return [data_env_path(data_dir), Path(".env")]


def load_env_files(data_dir: Path) -> dict[str, str]:
    """Carga en os.environ las variables gestionadas que no estén ya definidas.

    Devuelve de dónde ha salido cada una: `SOURCE_ENV` si ya estaba en el entorno, o la
    ruta del fichero. Las que no aparecen en ningún sitio no están en el resultado.
    """
    sources: dict[str, str] = {name: SOURCE_ENV for name in MANAGED if os.environ.get(name)}
    for path in env_file_paths(data_dir):
        if not path.is_file():
            continue
        for name, value in parse_env(path.read_text(encoding="utf-8")).items():
            if name in MANAGED and value and name not in sources:
                os.environ[name] = value
                sources[name] = str(path.resolve())
    return sources


def save_env_value(path: Path, name: str, value: str) -> None:
    """Escribe o sustituye `name=value` en el fichero, conservando el resto de líneas."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else [CABECERA]
    nueva = f"{name}={value}"
    hecho = False
    for i, line in enumerate(lines):
        if line.strip().startswith(f"{name}="):
            lines[i] = nueva
            hecho = True
    if not hecho:
        lines.append(nueva)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        os.chmod(path, 0o600)  # en Windows no hace nada; en Linux solo lo lee el usuario
    except OSError:
        pass


def remove_env_value(path: Path, name: str) -> bool:
    """Quita la línea de `name`. Devuelve True si había algo que quitar."""
    if not path.is_file():
        return False
    lines = path.read_text(encoding="utf-8").splitlines()
    nuevas = [line for line in lines if not line.strip().startswith(f"{name}=")]
    if len(nuevas) == len(lines):
        return False
    path.write_text("\n".join(nuevas) + "\n", encoding="utf-8")
    return True


def hint(value: str | None) -> str:
    """Pista para la interfaz sin revelar la clave: solo los últimos cuatro caracteres."""
    if not value:
        return ""
    return "…" + value[-4:] if len(value) > 8 else "configurada"
