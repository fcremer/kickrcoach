"""Bleak kept behind a small M0 transport boundary."""
from typing import Any
from .protocol import FTMS, HRS


async def discover(timeout: float = 10) -> dict[str, tuple[Any, Any]]:
    from bleak import BleakScanner
    return await BleakScanner.discover(timeout=timeout, return_adv=True,
                                      service_uuids=[FTMS, HRS])


def select(found: dict[str, tuple[Any, Any]], identifier: str, service: str) -> Any:
    # Require an explicit platform identifier, not a fuzzy device-name match.
    for device, advert in found.values():
        if device.address == identifier:
            if service not in [u.lower() for u in advert.service_uuids]:
                raise ValueError("selected device did not advertise required service")
            return device
    raise ValueError("selected identifier not found; scan again (macOS uses UUIDs)")


def make_client(device: Any, callback: Any) -> Any:
    from bleak import BleakClient
    return BleakClient(device, disconnected_callback=callback, timeout=20)
