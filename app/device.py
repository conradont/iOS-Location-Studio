"""Comunicacao com o iPhone via pymobiledevice3 (>= 11, API assincrona).

Existem dois caminhos para falsificar a localizacao, e o app escolhe conforme a
versao do iOS:

* iOS 16 e anteriores: servico `com.apple.dt.simulatelocation` (DtSimulateLocation),
  acessado direto pelo lockdown/USB. A localizacao fica valendo no aparelho ate
  ser explicitamente removida.
* iOS 17 e superiores: servico DVT/Instruments (o mesmo do "Simulate Location" do
  Xcode), que exige um tunel RSD (`pymobiledevice3 remote tunneld`) rodando como
  administrador. Nesse caminho a simulacao vale enquanto o canal estiver aberto.

Requisitos no aparelho: cabo USB, pareamento aceito, Modo Desenvolvedor ativo e
imagem de desenvolvedor montada (o app tenta montar sozinho).

Toda a biblioteca e assincrona, entao a sessao vive em uma thread propria com seu
event loop. A interface conversa com essa thread por filas.
"""

from __future__ import annotations

import asyncio
import os
import queue
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

import requests

TUNNELD_HOST = "127.0.0.1"
TUNNELD_PORT = 49151
TUNNELD_URL = f"http://{TUNNELD_HOST}:{TUNNELD_PORT}"

STATE_DISCONNECTED = "disconnected"
STATE_CONNECTING = "connecting"
STATE_CONNECTED = "connected"
STATE_ERROR = "error"

DVT_MIN_VERSION = 17

# Um servico de desenvolvedor do iPhone pode travar (por exemplo, se um envio de
# imagem foi interrompido no meio). Sem limite de tempo a sessao ficaria pendurada
# para sempre, entao cada etapa tem seu proprio teto.
TIMEOUT_LIST = 10
TIMEOUT_LOCKDOWN = 30
TIMEOUT_QUERY = 20
TIMEOUT_MOUNT = 900
TIMEOUT_TUNNEL = 45
TIMEOUT_LOCATION = 25


@dataclass
class DeviceInfo:
    udid: str = ""
    name: str = "iPhone"
    ios_version: str = "?"
    model: str = ""
    developer_mode: bool | None = None
    uses_tunnel: bool = False


@dataclass
class Event:
    kind: str
    payload: dict[str, Any] = field(default_factory=dict)


class DeviceError(RuntimeError):
    """Erro com mensagem pronta para exibicao ao usuario."""


def major_version(version: str) -> int:
    try:
        return int(version.split(".")[0])
    except (ValueError, IndexError):
        return 0


class DeviceWorker(threading.Thread):
    """Dono exclusivo da sessao com o aparelho.

    A UI envia comandos com `submit()` / `request_location()` e le o resultado com
    `drain_events()`; nada de pymobiledevice3 roda na thread da interface.
    """

    def __init__(self) -> None:
        super().__init__(name="ios-device-worker", daemon=True)
        self._events: queue.Queue[Event] = queue.Queue()
        self._ready = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._queue: asyncio.Queue[tuple[str, dict[str, Any]]] | None = None

        self._lockdown: Any = None
        self._rsd: Any = None
        self._dvt: Any = None
        self._simulation: Any = None
        self._info: DeviceInfo | None = None

        self._pending_target: tuple[float, float] | None = None
        self._active_target: tuple[float, float] | None = None

    # ------------------------------------------------------------------ API da UI
    def submit(self, action: str, **payload: Any) -> None:
        if not self._ready.wait(timeout=5) or self._loop is None or self._queue is None:
            return
        self._loop.call_soon_threadsafe(self._queue.put_nowait, (action, payload))

    def request_location(self, latitude: float, longitude: float) -> None:
        """Enfileira uma coordenada; alvos antigos ainda nao aplicados sao descartados."""
        self._pending_target = (latitude, longitude)
        self.submit("set_location")

    def drain_events(self) -> list[Event]:
        events: list[Event] = []
        while True:
            try:
                events.append(self._events.get_nowait())
            except queue.Empty:
                return events

    def shutdown(self) -> None:
        self.submit("_stop")

    # ------------------------------------------------------------------- eventos
    def _emit(self, kind: str, **payload: Any) -> None:
        self._events.put(Event(kind, payload))

    def _log(self, message: str, level: str = "info") -> None:
        self._emit("log", message=message, level=level)

    def _emit_state(self, state: str) -> None:
        self._emit("state", state=state, info=self._info)

    # ------------------------------------------------------------------ main loop
    def run(self) -> None:
        try:
            asyncio.run(self._main())
        except Exception as exc:  # noqa: BLE001
            self._log(f"A thread do aparelho terminou: {exc}", "error")

    async def _main(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue()
        self._ready.set()

        while True:
            action, payload = await self._queue.get()
            if action == "_stop":
                break
            handler: Callable[..., Awaitable[None]] | None = getattr(self, f"_do_{action}", None)
            if handler is None:
                self._log(f"Comando desconhecido: {action}", "error")
                continue
            try:
                await handler(**payload)
            except DeviceError as exc:
                self._fail(str(exc))
            except Exception as exc:  # noqa: BLE001 - erros da lib sao variados
                self._fail(self._humanize(exc))

        await self._close_session(silent=True)

    async def _limited(self, awaitable: Any, seconds: float, description: str) -> Any:
        """Executa uma etapa com teto de tempo e mensagem util em caso de estouro."""
        try:
            return await asyncio.wait_for(awaitable, timeout=seconds)
        except asyncio.TimeoutError as exc:
            raise DeviceError(
                f"{description} nao respondeu em {int(seconds)}s. Desconecte e reconecte o "
                "cabo USB com o iPhone desbloqueado; se persistir, reinicie o aparelho."
            ) from exc

    def _fail(self, message: str) -> None:
        self._log(message, "error")
        self._emit_state(STATE_CONNECTED if self._simulation is not None else STATE_ERROR)

    # -------------------------------------------------------------------- comandos
    async def _do_list_devices(self) -> None:
        from pymobiledevice3 import usbmux
        from pymobiledevice3.lockdown import create_using_usbmux

        try:
            entries = await asyncio.wait_for(usbmux.list_devices(), timeout=TIMEOUT_LIST)
        except Exception as exc:  # noqa: BLE001 - servico da Apple ausente/parado
            self._emit("devices", devices=[])
            self._log(self._humanize(exc), "warn")
            return

        devices: list[dict[str, Any]] = []
        for entry in entries:
            udid = getattr(entry, "serial", "") or ""
            item = {"udid": udid, "name": "iPhone", "ios_version": "?"}
            lockdown = None
            try:
                lockdown = await asyncio.wait_for(
                    create_using_usbmux(serial=udid, autopair=False), timeout=TIMEOUT_LIST
                )
                item["name"] = lockdown.short_info.get("DeviceName") or item["name"]
                item["ios_version"] = lockdown.product_version or "?"
            except Exception:  # noqa: BLE001 - aparelho bloqueado ou nao pareado
                pass
            finally:
                if lockdown is not None:
                    await self._safe_close(lockdown)
            devices.append(item)

        self._emit("devices", devices=devices)
        if not devices:
            self._log(
                "Nenhum iPhone encontrado. Conecte o cabo USB, desbloqueie o aparelho e "
                "toque em 'Confiar neste computador'.",
                "warn",
            )

    async def _do_connect(self, udid: str | None = None) -> None:
        await self._close_session(silent=True)
        self._emit_state(STATE_CONNECTING)

        from pymobiledevice3.lockdown import create_using_usbmux

        self._lockdown = await self._limited(
            create_using_usbmux(serial=udid or None), TIMEOUT_LOCKDOWN, "O pareamento do iPhone"
        )
        short = self._lockdown.short_info or {}
        version = self._lockdown.product_version or "?"
        info = DeviceInfo(
            udid=getattr(self._lockdown, "udid", None) or (udid or ""),
            name=short.get("DeviceName") or "iPhone",
            ios_version=version,
            model=short.get("ProductType") or "",
            developer_mode=await self._developer_mode(),
            uses_tunnel=major_version(version) >= DVT_MIN_VERSION,
        )
        self._info = info
        self._log(f"Conectado a {info.name} (iOS {info.ios_version}).", "ok")

        if info.developer_mode is False:
            raise DeviceError(
                "Modo Desenvolvedor desativado. No iPhone: Ajustes > Privacidade e "
                "Seguranca > Modo Desenvolvedor. Ative e reinicie o aparelho."
            )

        await self._ensure_developer_image()

        if info.uses_tunnel:
            self._simulation = await self._open_dvt_simulation(info.udid)
        else:
            from pymobiledevice3.services.simulate_location import DtSimulateLocation

            self._simulation = DtSimulateLocation(self._lockdown)

        self._emit_state(STATE_CONNECTED)
        self._log("Servico de simulacao de localizacao pronto.", "ok")

    async def _do_disconnect(self) -> None:
        await self._close_session()
        self._emit_state(STATE_DISCONNECTED)

    async def _do_set_location(self) -> None:
        target = self._pending_target
        self._pending_target = None
        if target is None:
            return
        if self._simulation is None:
            raise DeviceError("Conecte um iPhone antes de aplicar a localizacao.")

        latitude, longitude = target
        try:
            await self._limited(
                self._simulation.set(latitude, longitude),
                TIMEOUT_LOCATION,
                "O envio da localizacao",
            )
        except DeviceError:
            raise
        except Exception as exc:  # noqa: BLE001
            if not self._looks_like_missing_image(exc):
                raise
            self._log("Servico de desenvolvedor indisponivel. Montando a imagem...", "warn")
            await self._ensure_developer_image(force=True)
            await self._limited(
                self._simulation.set(latitude, longitude),
                TIMEOUT_LOCATION,
                "O envio da localizacao",
            )

        self._active_target = target
        self._emit("location", lat=latitude, lon=longitude, active=True)

    async def _do_clear_location(self) -> None:
        if self._simulation is None:
            self._active_target = None
            self._emit("location", lat=None, lon=None, active=False)
            return
        await self._limited(
            self._simulation.clear(), TIMEOUT_LOCATION, "A remocao da localizacao simulada"
        )
        self._active_target = None
        self._emit("location", lat=None, lon=None, active=False)
        self._log("Simulacao removida: o iPhone voltou ao GPS real.", "ok")

    async def _do_mount_image(self) -> None:
        if self._lockdown is None:
            raise DeviceError("Conecte um iPhone antes de montar a imagem de desenvolvedor.")
        await self._ensure_developer_image(force=True)

    # ---------------------------------------------------------------- infra interna
    async def _developer_mode(self) -> bool | None:
        try:
            status = await asyncio.wait_for(
                self._lockdown.get_developer_mode_status(), timeout=TIMEOUT_QUERY
            )
            return bool(status)
        except Exception:  # noqa: BLE001 - iOS < 16 nao expoe esse valor
            return None

    async def _ensure_developer_image(self, force: bool = False) -> None:
        """Monta a Developer Disk Image / imagem personalizada, se ainda nao estiver."""
        from pymobiledevice3.services.mobile_image_mounter import (
            AlreadyMountedError,
            DeveloperDiskImageMounter,
            PersonalizedImageMounter,
            auto_mount,
        )

        version = self._info.ios_version if self._info else "0"
        mounter_class = (
            PersonalizedImageMounter
            if major_version(version) >= DVT_MIN_VERSION
            else DeveloperDiskImageMounter
        )
        mounter = mounter_class(lockdown=self._lockdown)
        try:
            mounted = await asyncio.wait_for(
                mounter.is_image_mounted(mounter_class.IMAGE_TYPE), timeout=TIMEOUT_QUERY
            )
            if mounted:
                return
        except asyncio.TimeoutError as exc:
            raise DeviceError(
                "O servico de imagens do iPhone travou (comum quando uma montagem anterior "
                "foi interrompida). Desconecte o cabo, reinicie o iPhone e tente de novo."
            ) from exc
        except Exception:  # noqa: BLE001 - sem resposta clara, tenta montar de todo jeito
            pass
        finally:
            await self._safe_close(mounter)

        self._log("Preparando a imagem de desenvolvedor (pode baixar alguns MB)...", "info")
        try:
            await self._limited(
                auto_mount(self._lockdown), TIMEOUT_MOUNT, "A montagem da imagem de desenvolvedor"
            )
            self._log("Imagem de desenvolvedor montada.", "ok")
        except AlreadyMountedError:
            pass
        except DeviceError:
            raise
        except Exception as exc:  # noqa: BLE001
            message = (
                "Nao foi possivel montar a imagem de desenvolvedor automaticamente "
                f"({type(exc).__name__}). Se a simulacao falhar, rode: "
                "python -m pymobiledevice3 mounter auto-mount"
            )
            if force:
                raise DeviceError(message) from exc
            self._log(message, "warn")

    async def _open_dvt_simulation(self, udid: str) -> Any:
        """Abre o canal DVT sobre o tunel RSD (iOS 17+) e mantem a sessao viva."""
        from pymobiledevice3.exceptions import TunneldConnectionError
        from pymobiledevice3.services.dvt.instruments.dvt_provider import DvtProvider
        from pymobiledevice3.services.dvt.instruments.location_simulation import LocationSimulation
        from pymobiledevice3.tunneld.api import get_tunneld_device_by_udid, get_tunneld_devices

        try:
            rsd = (
                await asyncio.wait_for(get_tunneld_device_by_udid(udid), timeout=TIMEOUT_TUNNEL)
                if udid
                else None
            )
            if rsd is None:
                candidates = await asyncio.wait_for(get_tunneld_devices(), timeout=TIMEOUT_TUNNEL)
                rsd = candidates[0] if candidates else None
        except asyncio.TimeoutError as exc:
            self._emit("tunnel_required")
            raise DeviceError(
                "O tunel RSD nao respondeu. Pare o tunel, reconecte o cabo e inicie de novo "
                "com 'Iniciar tunel RSD (admin)'."
            ) from exc
        except TunneldConnectionError as exc:
            self._emit("tunnel_required")
            raise DeviceError(
                "iOS 17 ou superior exige o tunel RSD. Clique em 'Iniciar tunel RSD (admin)' "
                "ou rode em um terminal como administrador: "
                "python -m pymobiledevice3 remote tunneld"
            ) from exc

        if rsd is None:
            raise DeviceError(
                "O tunel esta ativo, mas nao publicou este aparelho. Reconecte o cabo USB e "
                "reinicie o tunel."
            )

        self._rsd = rsd
        self._log("Tunel RSD conectado.", "ok")

        try:
            return await self._open_dvt_channel(DvtProvider, LocationSimulation)
        except DeviceError:
            raise
        except Exception as exc:  # noqa: BLE001
            if not self._looks_like_missing_image(exc):
                raise
            # o dtservicehub so aparece no RSD depois que a imagem esta montada
            self._log("Canal DVT indisponivel. Montando a imagem e tentando de novo...", "warn")
            await self._ensure_developer_image(force=True)
            return await self._open_dvt_channel(DvtProvider, LocationSimulation)

    async def _open_dvt_channel(self, provider_class: Any, simulation_class: Any) -> Any:
        if self._dvt is not None:
            await self._safe_close(self._dvt)
        self._dvt = provider_class(self._rsd)
        await self._limited(self._dvt.connect(), TIMEOUT_TUNNEL, "O canal DVT do iPhone")
        simulation = simulation_class(self._dvt)
        await self._limited(
            simulation.connect(), TIMEOUT_TUNNEL, "O servico de simulacao de localizacao"
        )
        return simulation

    @staticmethod
    def _looks_like_missing_image(exc: Exception) -> bool:
        text = f"{type(exc).__name__} {exc}".lower()
        return any(
            keyword in text
            for keyword in ("startservice", "invalidservice", "developerdiskimage", "not mounted")
        )

    async def _safe_close(self, resource: Any) -> None:
        closer = getattr(resource, "close", None)
        if not callable(closer):
            return
        try:
            result = closer()
            if asyncio.iscoroutine(result):
                await result
        except Exception:  # noqa: BLE001
            pass

    async def _close_session(self, silent: bool = False) -> None:
        if self._simulation is not None and self._active_target is not None:
            try:
                await self._simulation.clear()
            except Exception:  # noqa: BLE001
                pass
        for attribute in ("_simulation", "_dvt", "_rsd", "_lockdown"):
            resource = getattr(self, attribute)
            if resource is not None:
                await self._safe_close(resource)
                setattr(self, attribute, None)
        self._active_target = None
        self._pending_target = None
        self._info = None
        if not silent:
            self._log("Sessao encerrada.", "info")

    @staticmethod
    def _humanize(exc: Exception) -> str:
        name = type(exc).__name__
        text = str(exc) or name
        lowered = f"{name} {text}".lower()
        if "mux" in lowered or "connection refused" in lowered or "usbmuxd" in lowered:
            return (
                "O suporte a dispositivos Apple nao esta ativo. Clique em "
                "'Instalar driver Apple (sem iTunes)' no cartao Aparelho, ou instale o app "
                "'Apple Devices' da Microsoft Store, e reconecte o cabo."
            )
        if "passwordrequired" in lowered or "notpaired" in lowered or "pairing" in lowered:
            return "Desbloqueie o iPhone e toque em 'Confiar neste computador' para pareamento."
        if "developermode" in lowered:
            return (
                "Modo Desenvolvedor desativado. Ative em Ajustes > Privacidade e Seguranca > "
                "Modo Desenvolvedor e reinicie o iPhone."
            )
        if "tunneld" in lowered:
            return (
                "O tunel RSD nao esta rodando. Use o botao 'Iniciar tunel RSD (admin)' e "
                "conecte de novo."
            )
        if "timeout" in lowered:
            return "Tempo esgotado falando com o aparelho. Reconecte o cabo e tente de novo."
        if "startservice" in lowered or "invalidservice" in lowered:
            return (
                "O servico de desenvolvedor nao respondeu. Confirme o Modo Desenvolvedor e "
                "monte a imagem com: python -m pymobiledevice3 mounter auto-mount"
            )
        return f"{name}: {text}"


# ----------------------------------------------------------------------- tunneld
def start_tunneld_elevated() -> str:
    """Sobe `pymobiledevice3 remote tunneld` com privilegios elevados.

    Retorna a mensagem que deve ser mostrada ao usuario.
    """
    if tunneld_is_running():
        return "O tunel RSD ja esta ativo."

    executable = sys.executable or "python"
    if os.name == "nt":
        arguments = "'-m','pymobiledevice3','remote','tunneld'"
        command = f"Start-Process -Verb RunAs -FilePath '{executable}' -ArgumentList {arguments}"
        try:
            subprocess.Popen(
                ["powershell", "-NoProfile", "-Command", command],
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError as exc:
            return f"Falha ao iniciar o tunel: {exc}"
        return (
            "Aceite o pedido de administrador do Windows. Quando a janela do tunel abrir, "
            "clique em Conectar novamente."
        )

    return f"Abra um terminal e rode: sudo {executable} -m pymobiledevice3 remote tunneld"


def tunneld_is_running() -> bool:
    try:
        requests.get(TUNNELD_URL, timeout=2).raise_for_status()
        return True
    except requests.RequestException:
        return False
