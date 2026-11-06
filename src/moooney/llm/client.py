"""Cliente HTTP del proxy LiteLLM: reintentos con backoff, modo offline y registro.

Toda llamada a un modelo pasa por aquí. Cada intento de chat/embedding queda
registrado en la tabla llm_calls (model, purpose, tokens, payload_hash).
El modo offline (MOOONEY_OFFLINE=1) bloquea cualquier petición de red.
"""

import hashlib
import json
import os
import threading
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import httpx
import yaml
from pydantic import BaseModel

from moooney.db import repo

OFFLINE_ENV = "MOOONEY_OFFLINE"
_RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}


BASE_URL_ENV = "LITELLM_BASE_URL"


class LLMError(RuntimeError):
    """Fallo al hablar con el proxy (red, HTTP o respuesta malformada)."""


class LLMSettings(BaseModel):
    base_url: str
    api_key_env: str = "LITELLM_API_KEY"
    decider: str = "clef"
    models: dict[str, str]
    timeout_s: float = 30.0
    max_retries: int = 3
    backoff_s: float = 1.0
    max_concurrency: int = 8
    # Parámetros extra para el modelo de redacción. Los Qwen 3 razonan por defecto y el
    # razonamiento se come el límite de tokens: se desactiva con enable_thinking=false (vLLM).
    chat_extra: dict = {"chat_template_kwargs": {"enable_thinking": False}}


def load_llm_settings(path: Path | None = None) -> LLMSettings:
    if path is None:
        path = Path("config/llm.yaml")
    if not path.exists():
        raise FileNotFoundError(f"no se encuentra la configuracion LLM: {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    # La URL real del proxy no vive en el repo: LITELLM_BASE_URL (entorno o data/.env) manda
    # sobre base_url del YAML, que queda como marcador.
    if os.environ.get(BASE_URL_ENV):
        data["base_url"] = os.environ[BASE_URL_ENV]
    return LLMSettings.model_validate(data)


def offline_enabled() -> bool:
    return os.environ.get(OFFLINE_ENV) == "1"


class CallRecorder:
    """Registra llamadas en llm_calls.

    Los hilos que hacen la peticion solo encolan el asiento; la escritura a la
    base de datos ocurre en ``flush()``, que debe llamarse desde el hilo
    principal. Una conexion sqlite no admite transacciones concurrentes entre
    hilos, y asi evitamos mezclar al grabador con las escrituras de la
    propagacion.
    """

    def __init__(self, conn) -> None:  # sqlite3.Connection
        self._conn = conn
        self._lock = threading.Lock()
        self._pending: list[dict] = []

    def record(
        self,
        *,
        model: str,
        purpose: str,
        payload_hash: str | None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
    ) -> None:
        ts = datetime.now().isoformat(timespec="seconds")
        with self._lock:
            self._pending.append(
                {
                    "ts": ts,
                    "model": model,
                    "purpose": purpose,
                    "payload_hash": payload_hash,
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                }
            )

    def flush(self) -> int:
        """Vuelca lo encolado (hilo principal) y devuelve el numero de asientos."""
        with self._lock:
            pendings, self._pending = self._pending, []
        for item in pendings:
            repo.insert_llm_call(self._conn, **item)
        if pendings:
            self._conn.commit()
        return len(pendings)


class LLMClient:
    def __init__(
        self,
        settings: LLMSettings,
        *,
        recorder: CallRecorder | None = None,
        transport: httpx.BaseTransport | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.settings = settings
        self.recorder = recorder
        self._transport = transport
        self._sleeper = sleeper
        self._client: httpx.Client | None = None

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    # ---- API pública ----

    def chat(
        self,
        *,
        model: str,
        messages: list[dict],
        purpose: str,
        response_format: dict | None = None,
        max_tokens: int | None = None,
        extra: dict | None = None,
    ) -> str:
        """Chat completion. Devuelve el content del primer choice."""
        payload: dict = {"model": model, "messages": messages}
        if response_format is not None:
            payload["response_format"] = response_format
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if extra:
            payload.update(extra)
        data = self._post("/chat/completions", payload, model=model, purpose=purpose)
        try:
            choice = data["choices"][0]
            content = choice["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError("respuesta del LLM sin choices[0].message.content") from exc
        if isinstance(content, list):  # partes de contenido estilo OpenAI
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
        if not isinstance(content, str) or not content.strip():
            motivo = " (limite de tokens agotado)" if choice.get("finish_reason") == "length" else ""
            raise LLMError(f"el LLM devolvio un content vacio{motivo}")
        return content

    def embed(self, *, model: str, texts: list[str], purpose: str) -> list[list[float]]:
        data = self._post("/embeddings", {"model": model, "input": texts}, model=model, purpose=purpose)
        try:
            items = sorted(data["data"], key=lambda d: d.get("index", 0))
            return [item["embedding"] for item in items]
        except (KeyError, TypeError) as exc:
            raise LLMError("respuesta de embeddings sin data[].embedding") from exc

    def list_models(self) -> list[str]:
        """Identificadores de modelo que el proxy ofrece a esta clave. Sirve de prueba de conexión."""
        if offline_enabled():
            raise LLMError(f"modo offline ({OFFLINE_ENV}=1): no se permite red")
        headers = {"Authorization": f"Bearer {self._api_key()}"}
        try:
            resp = self._http().get("/models", headers=headers)
        except httpx.HTTPError as exc:
            raise LLMError(f"sin conexión con el proxy: {exc}") from exc
        if resp.status_code in (401, 403):
            raise LLMError(f"el proxy rechaza la clave ({resp.status_code})")
        if resp.status_code >= 400:
            raise LLMError(f"el proxy responde {resp.status_code}")
        try:
            return [str(m["id"]) for m in resp.json().get("data", [])]
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise LLMError("respuesta de /models malformada") from exc

    # ---- interno ----

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                base_url=self.settings.base_url,
                timeout=self.settings.timeout_s,
                transport=self._transport,
            )
        return self._client

    def _api_key(self) -> str:
        key = os.environ.get(self.settings.api_key_env)
        if not key:
            raise LLMError(f"falta la variable de entorno {self.settings.api_key_env}")
        return key

    def _post(self, path: str, payload: dict, *, model: str, purpose: str) -> dict:
        if offline_enabled():
            raise LLMError(f"modo offline ({OFFLINE_ENV}=1): no se permite red")
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        ph = hashlib.sha256(body).hexdigest()
        headers = {
            "Authorization": f"Bearer {self._api_key()}",
            "Content-Type": "application/json; charset=utf-8",
        }
        last_error: Exception | None = None
        for attempt in range(self.settings.max_retries + 1):
            try:
                resp = self._http().post(path, content=body, headers=headers)
                resp.raise_for_status()
                data = resp.json()
                self._record(model, purpose, ph, data)
                return data
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                if status not in _RETRYABLE_STATUS:
                    self._record(model, purpose, ph, None)
                    raise LLMError(f"LLM {path} fallo con HTTP {status}") from exc
                last_error = exc
            except httpx.TransportError as exc:  # incluye timeouts
                last_error = exc
            if attempt < self.settings.max_retries:
                self._sleeper(self.settings.backoff_s * (2**attempt))
        self._record(model, purpose, ph, None)
        raise LLMError(f"LLM {path} agoto los reintentos: {last_error}")

    def _record(self, model: str, purpose: str, ph: str, data: dict | None) -> None:
        if self.recorder is None:
            return
        usage = (data or {}).get("usage") or {}
        self.recorder.record(
            model=model,
            purpose=purpose,
            payload_hash=ph,
            input_tokens=usage.get("prompt_tokens"),
            output_tokens=usage.get("completion_tokens"),
        )
