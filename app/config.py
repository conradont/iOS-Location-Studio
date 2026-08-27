"""Persistencia de preferencias e locais favoritos."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

DEFAULTS: dict[str, Any] = {
    "last_position": [-23.5613, -46.6565],  # Av. Paulista, Sao Paulo
    "last_zoom": 13,
    "last_udid": None,
    "favorites": [],
    "speed_kmh": 40.0,
}


def app_dir() -> Path:
    """Diretorio de configuracao do usuario, dependente do sistema operacional."""
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA") or Path.home())
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    path = base / "iOSLocationStudio"
    path.mkdir(parents=True, exist_ok=True)
    return path


class Store:
    """Arquivo JSON simples com as preferencias do app."""

    def __init__(self) -> None:
        self.path = app_dir() / "config.json"
        self.data: dict[str, Any] = dict(DEFAULTS)
        self.load()

    def load(self) -> None:
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                stored = json.load(handle)
            if isinstance(stored, dict):
                self.data.update(stored)
        except (OSError, json.JSONDecodeError):
            pass

    def save(self) -> None:
        try:
            with self.path.open("w", encoding="utf-8") as handle:
                json.dump(self.data, handle, indent=2, ensure_ascii=False)
        except OSError:
            pass

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, DEFAULTS.get(key, default))

    def set(self, key: str, value: Any) -> None:
        self.data[key] = value
        self.save()

    # ------------------------------------------------------------------ favoritos
    @property
    def favorites(self) -> list[dict[str, Any]]:
        items = self.data.get("favorites")
        return items if isinstance(items, list) else []

    def add_favorite(self, name: str, latitude: float, longitude: float) -> None:
        favorites = self.favorites
        favorites.append({"name": name, "lat": round(latitude, 6), "lon": round(longitude, 6)})
        self.set("favorites", favorites)

    def remove_favorite(self, index: int) -> None:
        favorites = self.favorites
        if 0 <= index < len(favorites):
            favorites.pop(index)
            self.set("favorites", favorites)
