"""Instalacao do suporte a dispositivos Apple no Windows, sem instalar o iTunes.

Para falar com o iPhone por USB, o Windows precisa do driver da Apple: ele publica
o servico usbmux em 127.0.0.1:27015 e cria a interface de rede usada pelo tunel do
iOS 17+. Nao existe caminho sem esse driver - ferramentas comerciais resolvem isso
embutindo o mesmo pacote da Apple.

Aqui o pacote e baixado direto da Apple na hora do uso (nada e redistribuido junto
do projeto). O instalador oficial do iTunes e um executavel com um cabinet embutido
que contem tres arquivos:

    iTunes64.msi, AppleMobileDeviceSupport64.msi, SetupAdmin.exe

O cabinet e recortado do executavel e apenas o MSI do suporte a dispositivos e
extraido com o `expand.exe` do proprio Windows, resultando em ~40 MB instalados no
lugar dos ~208 MB do iTunes completo.
"""

from __future__ import annotations

import os
import socket
import struct
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Callable

USBMUX_HOST = "127.0.0.1"
USBMUX_PORT = 27015

ITUNES_URL = "https://www.apple.com/itunes/download/win64"
AMDS_MSI = "AppleMobileDeviceSupport64.msi"
CAB_SIGNATURE = b"MSCF"

# codigos de saida do msiexec considerados sucesso (o segundo pede reinicializacao)
MSI_SUCCESS = (0, 3010)

Progress = Callable[[str, str], None]


def usbmux_available(timeout: float = 1.5) -> bool:
    """Indica se o servico da Apple esta respondendo (driver presente e ativo)."""
    try:
        with socket.create_connection((USBMUX_HOST, USBMUX_PORT), timeout=timeout):
            return True
    except OSError:
        return False


def is_supported_platform() -> bool:
    return os.name == "nt"


def install_apple_device_support(progress: Progress) -> bool:
    """Baixa, extrai e instala o suporte a dispositivos Apple.

    :param progress: callback `(mensagem, nivel)` para acompanhar o andamento.
    :returns: True se o servico da Apple passou a responder.
    """
    if not is_supported_platform():
        progress("Instalacao automatica do driver disponivel apenas no Windows.", "warn")
        return False

    if usbmux_available():
        progress("O suporte a dispositivos Apple ja esta ativo.", "ok")
        return True

    workspace = Path(tempfile.gettempdir()) / "ios_location_studio_driver"
    workspace.mkdir(parents=True, exist_ok=True)
    installer = workspace / "AppleSetup.exe"
    cabinet = workspace / "payload.cab"

    try:
        _download(installer, progress)
        _carve_cabinet(installer, cabinet, progress)
        msi = _extract_msi(cabinet, workspace, progress)
        _run_installer(msi, progress)
    except DriverError as exc:
        progress(str(exc), "error")
        return False
    except Exception as exc:  # noqa: BLE001
        progress(f"Falha inesperada na instalacao do driver: {exc}", "error")
        return False
    finally:
        for leftover in (installer, cabinet):
            leftover.unlink(missing_ok=True)

    progress("Aguardando o servico da Apple iniciar...", "info")
    for _ in range(30):
        if usbmux_available():
            progress("Suporte a dispositivos Apple instalado e ativo.", "ok")
            return True
        time.sleep(2)

    progress(
        "Driver instalado, mas o servico ainda nao respondeu. Reinicie o Windows e "
        "conecte o iPhone novamente.",
        "warn",
    )
    return False


class DriverError(RuntimeError):
    """Falha com mensagem pronta para exibicao."""


def _download(destination: Path, progress: Progress) -> None:
    import requests

    progress("Baixando o pacote oficial da Apple (~208 MB)...", "info")
    try:
        with requests.get(ITUNES_URL, stream=True, timeout=60) as response:
            response.raise_for_status()
            total = int(response.headers.get("Content-Length") or 0)
            written = 0
            next_mark = 10
            with destination.open("wb") as handle:
                for chunk in response.iter_content(1 << 20):
                    handle.write(chunk)
                    written += len(chunk)
                    if total and written * 100 // total >= next_mark:
                        progress(f"Download: {written * 100 // total}%", "info")
                        next_mark += 10
    except requests.RequestException as exc:
        raise DriverError(f"Nao foi possivel baixar o pacote da Apple: {exc}") from exc


def _carve_cabinet(installer: Path, cabinet: Path, progress: Progress) -> None:
    """Recorta o cabinet embutido no executavel do instalador."""
    data = installer.read_bytes()
    offset = data.find(CAB_SIGNATURE)
    if offset < 0:
        raise DriverError("O instalador da Apple mudou de formato: cabinet nao encontrado.")

    (size,) = struct.unpack_from("<I", data, offset + 8)
    if size <= 0 or offset + size > len(data):
        size = len(data) - offset
    cabinet.write_bytes(data[offset : offset + size])
    progress("Pacote aberto, extraindo apenas o driver...", "info")


def _extract_msi(cabinet: Path, workspace: Path, progress: Progress) -> Path:
    result = subprocess.run(
        ["expand.exe", str(cabinet), f"-F:{AMDS_MSI}", str(workspace)],
        capture_output=True,
        text=True,
        errors="ignore",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    msi = workspace / AMDS_MSI
    if result.returncode != 0 or not msi.exists():
        raise DriverError(
            "Falha ao extrair o driver do pacote da Apple. "
            f"Detalhe: {(result.stderr or result.stdout or '').strip()[:200]}"
        )
    progress(f"Driver extraido ({msi.stat().st_size // (1 << 20)} MB). Instalando...", "info")
    return msi


def _run_installer(msi: Path, progress: Progress) -> None:
    """Roda o MSI em modo silencioso com um unico pedido de administrador (UAC)."""
    progress("Aceite o pedido de administrador do Windows para instalar o driver.", "info")
    command = (
        "$p = Start-Process -Verb RunAs -Wait -PassThru -FilePath 'msiexec.exe' "
        f"-ArgumentList '/i','\"{msi}\"','/qn','/norestart'; exit $p.ExitCode"
    )
    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command", command],
        capture_output=True,
        text=True,
        errors="ignore",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode not in MSI_SUCCESS:
        raise DriverError(
            "A instalacao do driver foi cancelada ou falhou "
            f"(codigo {result.returncode}). Tente de novo aceitando o pedido de administrador."
        )
    if result.returncode == 3010:
        progress("Driver instalado. O Windows pede reinicializacao para concluir.", "warn")
