"""Ponto de entrada do iOS Location Studio.

Uso: python main.py
"""

from __future__ import annotations

import sys

MIN_PYTHON = (3, 10)

DEPENDENCIES = {
    "customtkinter": "customtkinter",
    "tkintermapview": "tkintermapview",
    "PIL": "Pillow",
    "requests": "requests",
    "pymobiledevice3": "pymobiledevice3",
}


def check_environment() -> list[str]:
    import importlib.util

    missing = []
    for module, package in DEPENDENCIES.items():
        if importlib.util.find_spec(module) is None:
            missing.append(package)
    return missing


def main() -> int:
    if sys.version_info < MIN_PYTHON:
        print(
            f"Este programa precisa do Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]} ou superior "
            f"(voce esta usando {sys.version.split()[0]})."
        )
        return 1

    try:
        import tkinter  # noqa: F401
    except ImportError:
        print(
            "Sua instalacao do Python nao inclui o Tkinter. Reinstale o Python do site "
            "oficial mantendo a opcao 'tcl/tk and IDLE' marcada."
        )
        return 1

    missing = check_environment()
    if missing:
        print("Dependencias faltando:", ", ".join(missing))
        print("Instale com: python -m pip install -r requirements.txt")
        return 1

    from app.ui import run

    run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
