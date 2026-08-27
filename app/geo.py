"""Geocodificacao (Nominatim/OpenStreetMap) e calculos geograficos."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable

import requests

from . import APP_NAME, APP_VERSION

NOMINATIM = "https://nominatim.openstreetmap.org"
USER_AGENT = f"{APP_NAME}/{APP_VERSION} (ferramenta local de teste de localizacao)"
TIMEOUT = 12

EARTH_RADIUS_M = 6_371_000.0


@dataclass
class Place:
    label: str
    lat: float
    lon: float


def search(query: str, limit: int = 6) -> list[Place]:
    """Converte um endereco/nome de lugar em coordenadas."""
    query = query.strip()
    if not query:
        return []

    coords = parse_coordinates(query)
    if coords:
        lat, lon = coords
        return [Place(label=f"{lat:.6f}, {lon:.6f}", lat=lat, lon=lon)]

    response = requests.get(
        f"{NOMINATIM}/search",
        params={"q": query, "format": "jsonv2", "limit": limit, "addressdetails": 0},
        headers={"User-Agent": USER_AGENT, "Accept-Language": "pt-BR,pt,en"},
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    results = []
    for item in response.json():
        try:
            results.append(
                Place(
                    label=item.get("display_name", "?"),
                    lat=float(item["lat"]),
                    lon=float(item["lon"]),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return results


def reverse(latitude: float, longitude: float) -> str:
    """Nome legivel para um par de coordenadas."""
    try:
        response = requests.get(
            f"{NOMINATIM}/reverse",
            params={"lat": latitude, "lon": longitude, "format": "jsonv2", "zoom": 16},
            headers={"User-Agent": USER_AGENT, "Accept-Language": "pt-BR,pt,en"},
            timeout=TIMEOUT,
        )
        response.raise_for_status()
        return response.json().get("display_name") or ""
    except (requests.RequestException, ValueError):
        return ""


def approximate_self_location() -> tuple[float, float] | None:
    """Localizacao aproximada do computador pelo IP publico.

    Serve apenas para centralizar o mapa em algo familiar na abertura do app:
    o protocolo do iPhone nao expoe a leitura do GPS real do aparelho.
    """
    endpoints = (
        ("http://ip-api.com/json/", "lat", "lon"),
        ("https://ipapi.co/json/", "latitude", "longitude"),
    )
    for url, lat_key, lon_key in endpoints:
        try:
            response = requests.get(url, timeout=6)
            response.raise_for_status()
            payload = response.json()
            return float(payload[lat_key]), float(payload[lon_key])
        except (requests.RequestException, ValueError, KeyError, TypeError):
            continue
    return None


# --------------------------------------------------------------------- utilitarios
def parse_coordinates(text: str) -> tuple[float, float] | None:
    """Aceita entradas como '-23.55, -46.63' ou '-23.55 -46.63'."""
    cleaned = text.replace(";", ",").replace("\t", " ").strip()
    parts = [p for p in (cleaned.split(",") if "," in cleaned else cleaned.split()) if p.strip()]
    if len(parts) != 2:
        return None
    try:
        lat = float(parts[0].strip())
        lon = float(parts[1].strip())
    except ValueError:
        return None
    if is_valid(lat, lon):
        return lat, lon
    return None


def is_valid(latitude: float, longitude: float) -> bool:
    return -90.0 <= latitude <= 90.0 and -180.0 <= longitude <= 180.0


def distance_m(origin: tuple[float, float], target: tuple[float, float]) -> float:
    """Distancia em metros entre dois pontos (formula de haversine)."""
    lat1, lon1 = map(math.radians, origin)
    lat2, lon2 = map(math.radians, target)
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


def move_towards(
    origin: tuple[float, float], target: tuple[float, float], meters: float
) -> tuple[float, float]:
    """Ponto a `meters` de `origin` na direcao de `target` (interpolacao linear)."""
    total = distance_m(origin, target)
    if total <= 0.01 or meters >= total:
        return target
    ratio = meters / total
    return (
        origin[0] + (target[0] - origin[0]) * ratio,
        origin[1] + (target[1] - origin[1]) * ratio,
    )


def path_length_m(points: Iterable[tuple[float, float]]) -> float:
    points = list(points)
    return sum(distance_m(points[i], points[i + 1]) for i in range(len(points) - 1))
