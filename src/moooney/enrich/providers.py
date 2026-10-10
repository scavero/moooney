"""Proveedores externos: dado un texto de consulta devuelven qué es el comercio.

Todos comparten la interfaz ``Provider.lookup(query) -> Enrichment | None`` y
el limitador de ritmo. Ninguno recibe nada más que la consulta ya construida
por ``query.build_query``. El modo offline (MOOONEY_OFFLINE=1) los bloquea.
"""

import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import urlencode

import httpx

from moooney.enrich.settings import EnrichmentSettings
from moooney.llm.client import OFFLINE_ENV, offline_enabled

USER_AGENT = "moooney/0.1 (uso personal; finanzas locales)"
MAX_RESULTS = 3


class ProviderError(RuntimeError):
    """Fallo de red o respuesta inesperada de un proveedor."""


@dataclass
class Enrichment:
    provider: str
    query: str
    summary: str
    place_types: list[str] = field(default_factory=list)
    url: str | None = None


class Provider(Protocol):
    name: str

    def lookup(self, query: str) -> Enrichment | None: ...


class RateLimiter:
    """Espera lo que falte para respetar un intervalo mínimo entre llamadas."""

    def __init__(self, seconds: float, *, sleeper: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.seconds = seconds
        self._sleeper = sleeper
        self._clock = clock
        self._last: float | None = None

    def wait(self) -> None:
        now = self._clock()
        if self._last is not None:
            remaining = self.seconds - (now - self._last)
            if remaining > 0:
                self._sleeper(remaining)
        self._last = self._clock()


class _HttpProvider:
    name = "http"

    def __init__(self, settings: EnrichmentSettings, limiter: RateLimiter,
                 transport: httpx.BaseTransport | None = None) -> None:
        self.settings = settings
        self.limiter = limiter
        self._transport = transport
        self._client: httpx.Client | None = None

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                timeout=self.settings.timeout_s,
                headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
                transport=self._transport,
                follow_redirects=True,
            )
        return self._client

    def _get_json(self, url: str, params: dict | None = None, headers: dict | None = None):
        if offline_enabled():
            raise ProviderError(f"modo offline ({OFFLINE_ENV}=1): no se permite red")
        self.limiter.wait()
        try:
            resp = self._http().get(url, params=params, headers=headers)
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as exc:
            raise ProviderError(f"{self.name}: HTTP {exc.response.status_code}") from exc
        except (httpx.TransportError, ValueError) as exc:
            raise ProviderError(f"{self.name}: {exc.__class__.__name__}") from exc

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None


class NominatimProvider(_HttpProvider):
    """OpenStreetMap Nominatim. Sin clave; exige User-Agent y <= 1 req/s."""

    name = "nominatim"
    BASE = "https://nominatim.openstreetmap.org/search"

    def lookup(self, query: str) -> Enrichment | None:
        data = self._get_json(
            self.BASE,
            params={"q": query, "format": "jsonv2", "limit": MAX_RESULTS, "accept-language": "es"},
        )
        if not isinstance(data, list) or not data:
            return None
        types: list[str] = []
        names: list[str] = []
        for item in data[:MAX_RESULTS]:
            if not isinstance(item, dict):
                continue
            cat, typ = item.get("category"), item.get("type")
            if cat and typ:
                types.append(f"{cat}/{typ}")
            name = item.get("name") or item.get("display_name", "").split(",")[0]
            if name:
                names.append(str(name))
        if not types:
            return None
        first = data[0] if isinstance(data[0], dict) else {}
        summary = f"{names[0] if names else query}: {types[0]}"
        if first.get("display_name"):
            summary += f" ({first['display_name']})"
        url = None
        if first.get("osm_type") and first.get("osm_id"):
            url = f"https://www.openstreetmap.org/{first['osm_type']}/{first['osm_id']}"
        return Enrichment(self.name, query, summary, place_types=types, url=url)


class SearxngProvider(_HttpProvider):
    """Buscador SearXNG propio (JSON). Solo si searxng_url está configurado."""

    name = "searxng"

    def lookup(self, query: str) -> Enrichment | None:
        base = self.settings.searxng_url
        if not base:
            return None
        data = self._get_json(
            f"{base.rstrip('/')}/search",
            params={"q": query, "format": "json", "language": "es-ES"},
        )
        results = data.get("results") if isinstance(data, dict) else None
        if not results:
            return None
        return _from_search_results(self.name, query, results)


class DuckDuckGoProvider(_HttpProvider):
    """DuckDuckGo a través del paquete ``ddgs`` (sin clave)."""

    name = "duckduckgo"

    def __init__(self, settings: EnrichmentSettings, limiter: RateLimiter,
                 transport: httpx.BaseTransport | None = None,
                 search_fn: Callable[[str], list[dict]] | None = None) -> None:
        super().__init__(settings, limiter, transport)
        self._search_fn = search_fn

    def _search(self, query: str) -> list[dict]:
        if self._search_fn is not None:
            return self._search_fn(query)
        try:
            from ddgs import DDGS
        except ImportError as exc:  # pragma: no cover - depende del entorno
            raise ProviderError("duckduckgo: falta el paquete ddgs (uv add ddgs)") from exc
        try:
            return list(DDGS().text(query, region="es-es", max_results=MAX_RESULTS))
        except Exception as exc:  # la librería lanza tipos propios
            raise ProviderError(f"duckduckgo: {exc.__class__.__name__}") from exc

    def lookup(self, query: str) -> Enrichment | None:
        if offline_enabled():
            raise ProviderError(f"modo offline ({OFFLINE_ENV}=1): no se permite red")
        self.limiter.wait()
        results = self._search(query)
        if not results:
            return None
        normalized = [
            {"title": r.get("title"), "content": r.get("body") or r.get("content"),
             "url": r.get("href") or r.get("url")}
            for r in results
            if isinstance(r, dict)
        ]
        return _from_search_results(self.name, query, normalized)


class GooglePlacesProvider(_HttpProvider):
    """Google Places Text Search (New). Requiere clave con facturación activa."""

    name = "google_places"
    BASE = "https://places.googleapis.com/v1/places:searchText"

    def lookup(self, query: str) -> Enrichment | None:
        api_key = os.environ.get(self.settings.google_places_api_key_env)
        if not api_key:
            return None
        if offline_enabled():
            raise ProviderError(f"modo offline ({OFFLINE_ENV}=1): no se permite red")
        self.limiter.wait()
        headers = {
            "X-Goog-Api-Key": api_key,
            "X-Goog-FieldMask": "places.displayName,places.types,places.formattedAddress,"
            "places.primaryType,places.googleMapsUri",
            "Content-Type": "application/json",
        }
        try:
            resp = self._http().post(
                self.BASE, json={"textQuery": query, "languageCode": "es", "maxResultCount": 3},
                headers=headers,
            )
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPStatusError as exc:
            raise ProviderError(f"{self.name}: HTTP {exc.response.status_code}") from exc
        except (httpx.TransportError, ValueError) as exc:
            raise ProviderError(f"{self.name}: {exc.__class__.__name__}") from exc
        places = data.get("places") if isinstance(data, dict) else None
        if not places:
            return None
        first = places[0]
        types = [first.get("primaryType")] if first.get("primaryType") else []
        types += [t for t in first.get("types", []) if t not in types]
        name = (first.get("displayName") or {}).get("text", query)
        summary = f"{name}: {', '.join(types[:4])}"
        if first.get("formattedAddress"):
            summary += f" ({first['formattedAddress']})"
        return Enrichment(self.name, query, summary, place_types=types, url=first.get("googleMapsUri"))


def _from_search_results(provider: str, query: str, results: list[dict]) -> Enrichment | None:
    parts: list[str] = []
    url: str | None = None
    for r in results[:MAX_RESULTS]:
        title = (r.get("title") or "").strip()
        content = (r.get("content") or "").strip()
        if not title and not content:
            continue
        parts.append(f"{title}: {content}" if title and content else title or content)
        if url is None and r.get("url"):
            url = str(r["url"])
    if not parts:
        return None
    return Enrichment(provider, query, " | ".join(parts), place_types=[], url=url)


def build_providers(
    settings: EnrichmentSettings,
    *,
    limiter: RateLimiter | None = None,
    transport: httpx.BaseTransport | None = None,
) -> list[Provider]:
    """Instancia los proveedores configurados, en orden, compartiendo el limitador."""
    limiter = limiter or RateLimiter(settings.rate_limit_seconds)
    registry: dict[str, type[_HttpProvider]] = {
        "nominatim": NominatimProvider,
        "searxng": SearxngProvider,
        "duckduckgo": DuckDuckGoProvider,
        "google_places": GooglePlacesProvider,
    }
    providers: list[Provider] = []
    for name in settings.providers:
        if name == "searxng" and not settings.searxng_url:
            continue
        if name == "google_places" and not os.environ.get(settings.google_places_api_key_env):
            continue
        providers.append(registry[name](settings, limiter, transport))
    return providers


def nominatim_url(query: str) -> str:
    """URL exacta que se enviaría a Nominatim (para el dry-run)."""
    return f"{NominatimProvider.BASE}?{urlencode({'q': query, 'format': 'jsonv2'})}"
