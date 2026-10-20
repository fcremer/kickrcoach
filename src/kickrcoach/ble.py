"""Bleak kept behind a small M0 transport boundary."""
from typing import Any
from .domain import GattCharacteristic
from .transport import Lost, Notify, TransportFactory
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


class BleakTransport:
    """M1 transport wrapper. Constructing it does not connect or command load."""
    def __init__(self, device: Any, on_disconnect: Lost) -> None:
        self._client = make_client(device, lambda _: on_disconnect())

    @property
    def is_connected(self) -> bool:
        return bool(self._client.is_connected)

    @property
    def characteristics(self) -> tuple[GattCharacteristic, ...]:
        return tuple(GattCharacteristic(service.uuid.lower(), char.uuid.lower(),
                                         tuple(char.properties))
                     for service in self._client.services for char in service.characteristics)

    async def connect(self) -> None:
        await self._client.connect()

    async def disconnect(self) -> None:
        await self._client.disconnect()

    async def read_gatt_char(self, characteristic: str) -> bytes:
        return bytes(await self._client.read_gatt_char(characteristic))

    async def subscribe(self, characteristic: str, callback: Notify) -> None:
        await self._client.start_notify(characteristic, lambda _, data: callback(bytes(data)))

    async def write_gatt_char(self, characteristic: str, data: bytes,
                              response: bool = True) -> None:
        await self._client.write_gatt_char(characteristic, data, response=response)


def transport_factory(device: Any) -> TransportFactory:
    def factory(on_disconnect: Lost) -> BleakTransport:
        return BleakTransport(device, on_disconnect)
    return factory
