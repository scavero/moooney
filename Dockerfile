# moooney: pipeline local de finanzas personales. Imagen pequeña, sin datos dentro:
# data/ y config/ se montan como volúmenes desde la máquina del usuario.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_DEV=1

COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /uvx /bin/

WORKDIR /app

# Dependencias primero (caché de capas): solo cambian si cambia el lock.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-install-project --no-dev

COPY src ./src
RUN uv sync --frozen --no-dev

# Configuración por defecto dentro de la imagen (sin budgets.yaml, que es del usuario);
# el compose la sustituye montando ./config.
COPY config ./config

# Carpetas que luego se montan; existen por si se arranca sin volúmenes.
RUN mkdir -p data/excel data/clean reports config

EXPOSE 8765
VOLUME ["/app/data", "/app/config", "/app/reports"]

# Dentro del contenedor hay que escuchar en todas las interfaces; el compose
# publica el puerto solo en 127.0.0.1 de la máquina anfitriona.
CMD ["uv", "run", "--no-sync", "moooney", "web", "--host", "0.0.0.0", "--port", "8765", "--no-open"]
