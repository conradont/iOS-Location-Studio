"""Diagnostico rapido da conexao com o iPhone.

Roda cada verificacao com tempo limite, para nunca ficar pendurado quando um
servico do aparelho travar. Uso: python diagnostico.py
"""

from __future__ import annotations

import asyncio
import sys

OK = "[ok]  "
FAIL = "[--]  "
WARN = "[??]  "


def say(mark: str, text: str) -> None:
    print(f"{mark}{text}", flush=True)


async def step(label: str, awaitable, timeout: float):
    try:
        return True, await asyncio.wait_for(awaitable, timeout=timeout)
    except asyncio.TimeoutError:
        say(FAIL, f"{label}: sem resposta em {int(timeout)}s (servico travado)")
    except Exception as exc:  # noqa: BLE001
        say(FAIL, f"{label}: {type(exc).__name__}: {exc}")
    return False, None


async def main() -> int:
    from app import driver

    if driver.is_supported_platform():
        if driver.usbmux_available():
            say(OK, "driver Apple (usbmuxd) respondendo na porta 27015")
        else:
            say(FAIL, "driver Apple ausente - use 'Instalar driver Apple' no app")
            return 1

    from pymobiledevice3 import usbmux
    from pymobiledevice3.lockdown import create_using_usbmux

    ok, entries = await step("lista de aparelhos", usbmux.list_devices(), 10)
    if not ok:
        return 1
    if not entries:
        say(FAIL, "nenhum iPhone conectado - ligue o cabo e toque em 'Confiar'")
        return 1
    say(OK, f"aparelhos conectados: {len(entries)}")

    serial = entries[0].serial
    ok, lockdown = await step("pareamento (lockdown)", create_using_usbmux(serial=serial), 30)
    if not ok:
        return 1
    version = lockdown.product_version or "?"
    say(OK, f"{lockdown.short_info.get('DeviceName') or 'iPhone'} - iOS {version}")

    ok, mode = await step("modo desenvolvedor", lockdown.get_developer_mode_status(), 20)
    if ok:
        say(OK if mode else FAIL, f"modo desenvolvedor: {'ativo' if mode else 'desativado'}")

    from pymobiledevice3.services.mobile_image_mounter import (
        DeveloperDiskImageMounter,
        PersonalizedImageMounter,
    )

    major = int((version.split(".") or ["0"])[0] or 0)
    mounter_class = PersonalizedImageMounter if major >= 17 else DeveloperDiskImageMounter
    mounter = mounter_class(lockdown=lockdown)
    ok, mounted = await step(
        "imagem de desenvolvedor", mounter.is_image_mounted(mounter_class.IMAGE_TYPE), 20
    )
    if ok:
        say(OK if mounted else WARN, f"imagem montada: {'sim' if mounted else 'nao (o app monta)'}")
    else:
        say(WARN, "reinicie o iPhone: o servico de imagens ficou preso")

    for closeable in (mounter, lockdown):
        try:
            await asyncio.wait_for(closeable.close(), timeout=5)
        except Exception:  # noqa: BLE001
            pass

    if major >= 17:
        from pymobiledevice3.tunneld.api import get_tunneld_devices

        ok, tunnels = await step("tunel RSD", get_tunneld_devices(), 15)
        if ok:
            say(OK if tunnels else WARN, f"tuneis publicados: {len(tunnels or [])}")
        else:
            say(WARN, "tunel RSD parado - use 'Iniciar tunel RSD (admin)' no app")

    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(130)
