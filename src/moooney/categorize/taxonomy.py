"""Taxonomía cerrada de categorías, cargada de config/categories.yaml.

Las descripciones se envían literalmente a Clef-Flash como criteria de la
pregunta "category", por eso viven en configuración y no en código.
"""

from pathlib import Path

import yaml
from pydantic import BaseModel


class CategorySpec(BaseModel):
    flow_type: str
    description: str
    subcategories: list[str] = []


class Taxonomy(BaseModel):
    categories: dict[str, CategorySpec]
    flow_types: dict[str, str]

    @property
    def category_names(self) -> list[str]:
        return list(self.categories)

    @property
    def flow_type_names(self) -> list[str]:
        return list(self.flow_types)

    def default_flow_type(self, category: str) -> str:
        return self.categories[category].flow_type


def load_taxonomy(path: Path | None = None) -> Taxonomy:
    """Carga la taxonomía. Por defecto, config/categories.yaml bajo el cwd."""
    if path is None:
        path = Path("config/categories.yaml")
    if not path.exists():
        raise FileNotFoundError(f"no se encuentra la taxonomía: {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    taxonomy = Taxonomy.model_validate(data)
    invalidos = [f for f in taxonomy.categories.values() if f.flow_type not in taxonomy.flow_types]
    if invalidos:
        raise ValueError("flow_type desconocido en alguna categoría de categories.yaml")
    return taxonomy
