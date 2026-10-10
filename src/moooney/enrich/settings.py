"""Configuración del enriquecimiento externo (config/enrichment.yaml)."""

import re
from pathlib import Path

import yaml
from pydantic import BaseModel, field_validator

KNOWN_PROVIDERS = ("nominatim", "searxng", "duckduckgo", "google_places")


class TriggerSettings(BaseModel):
    max_confidence: float = 0.6
    also_if_category: list[str] = ["Otros"]
    skip_flow_types: list[str] = ["transfer_internal", "income", "card_settlement"]
    skip_patterns: list[str] = []

    @field_validator("skip_patterns")
    @classmethod
    def _regex_validas(cls, v: list[str]) -> list[str]:
        for pattern in v:
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValueError(f"regex invalida en skip_patterns: {pattern!r} ({exc})") from exc
        return v


class EnrichmentSettings(BaseModel):
    enabled: bool = False
    trigger: TriggerSettings = TriggerSettings()
    providers: list[str] = ["nominatim", "duckduckgo"]
    searxng_url: str | None = None
    google_places_api_key_env: str = "GOOGLE_PLACES_API_KEY"
    region_hint: str = "Galicia, España"
    rate_limit_seconds: float = 1.5
    max_queries_per_run: int = 50
    max_summary_chars: int = 600
    timeout_s: float = 10.0

    @field_validator("providers")
    @classmethod
    def _proveedores_conocidos(cls, v: list[str]) -> list[str]:
        desconocidos = [p for p in v if p not in KNOWN_PROVIDERS]
        if desconocidos:
            raise ValueError(f"proveedores desconocidos en enrichment.yaml: {desconocidos}")
        return v


def load_enrichment_settings(path: Path | None = None) -> EnrichmentSettings:
    if path is None:
        path = Path("config/enrichment.yaml")
    if not path.exists():
        raise FileNotFoundError(f"no se encuentra la configuracion de enriquecimiento: {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return EnrichmentSettings.model_validate(data)
