"""The product map: what each golden template is built from, as the feeds name it.

``content/catalogue/feed_products.yaml`` (its header documents the fields). Found through
``TN_FEED_PRODUCTS``, then ``/app/content/catalogue`` (the worker image), then the
repository checkout this module sits in, as ``software_catalogue.py`` finds its file.
Pure apart from reading that file.
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml

from ..feed_backends.base import FeedProduct

_REL = Path("content") / "catalogue" / "feed_products.yaml"
_HERE = Path(__file__).resolve()
_REPO_COPY = (_HERE.parents[4] / _REL) if len(_HERE.parents) > 4 else _REL
_CONTAINER_COPY = Path("/app/content/catalogue/feed_products.yaml")


class CatalogueError(ValueError):
    """The product map is missing or malformed."""


def path() -> Path:
    env = os.environ.get("TN_FEED_PRODUCTS", "").strip()
    if env:
        return Path(env)
    return _CONTAINER_COPY if _CONTAINER_COPY.exists() else _REPO_COPY


def _product(raw) -> FeedProduct:
    if not isinstance(raw, dict) or not str(raw.get("name") or "").strip():
        raise CatalogueError("every product has a name")
    eol = raw.get("eol") or {}
    if not isinstance(eol, dict):
        raise CatalogueError(f"{raw['name']}: eol is {{product, cycle}}")
    kev = []
    for pair in raw.get("kev") or []:
        if not isinstance(pair, dict) or not pair.get("vendor") or not pair.get("product"):
            raise CatalogueError(f"{raw['name']}: each kev entry is {{vendor, product}}")
        kev.append((str(pair["vendor"]), str(pair["product"])))
    return FeedProduct(
        name=str(raw["name"]).strip(),
        eol_product=str(eol["product"]).strip() if eol.get("product") else None,
        eol_cycle=str(eol["cycle"]).strip() if eol.get("cycle") is not None else None,
        release=str(raw["release"]).strip() if raw.get("release") is not None else None,
        cpe=str(raw["cpe"]).strip() if raw.get("cpe") else None,
        kev=tuple(kev),
    )


def parse(doc) -> dict[str, list[FeedProduct]]:
    if not isinstance(doc, dict) or not isinstance(doc.get("golden_templates"), dict):
        raise CatalogueError("the product map has no golden_templates mapping")
    out: dict[str, list[FeedProduct]] = {}
    for catalogue_id, products in doc["golden_templates"].items():
        if not isinstance(products, list):
            raise CatalogueError(f"{catalogue_id}: a list of products")
        out[str(catalogue_id)] = [_product(p) for p in products]
    return out


def load(file: Path | None = None) -> dict[str, list[FeedProduct]]:
    """Golden template catalogue id -> its products."""
    target = file or path()
    try:
        doc = yaml.safe_load(target.read_text(encoding="utf-8"))
    except OSError as exc:
        raise CatalogueError(f"the product map {target} cannot be read") from exc
    except yaml.YAMLError as exc:
        raise CatalogueError(f"the product map {target} is not YAML") from exc
    return parse(doc)


def all_products(mapping: dict[str, list[FeedProduct]]) -> list[FeedProduct]:
    """Each product once (several templates share Windows Server 2022)."""
    return list(dict.fromkeys(p for products in mapping.values() for p in products))
