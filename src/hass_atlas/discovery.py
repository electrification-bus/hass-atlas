"""mDNS discovery of Home Assistant instances."""

from __future__ import annotations

import time
from dataclasses import dataclass

from zeroconf import IPVersion, ServiceBrowser, ServiceListener, Zeroconf

HA_SERVICE_TYPE = "_home-assistant._tcp.local."
DEFAULT_TIMEOUT = 5.0


@dataclass
class HAInstance:
    """A discovered Home Assistant instance."""

    name: str
    host: str
    ip: str
    port: int
    version: str | None = None
    location_name: str | None = None
    uuid: str | None = None

    @property
    def url(self) -> str:
        return f"http://{self.ip}:{self.port}"


class _HAListener(ServiceListener):
    def __init__(self) -> None:
        self.instances: list[HAInstance] = []

    def add_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        info = zc.get_service_info(type_, name)
        if info is None:
            return
        addresses = info.parsed_addresses(IPVersion.V4Only)
        if not addresses:
            return
        props = info.decoded_properties or {}
        self.instances.append(HAInstance(
            name=name,
            host=info.server or "",
            ip=addresses[0],
            port=info.port or 8123,
            version=props.get("version"),
            location_name=props.get("location_name"),
            uuid=props.get("uuid"),
        ))

    def remove_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        pass

    def update_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        pass


def discover_ha(timeout: float = DEFAULT_TIMEOUT) -> list[HAInstance]:
    """Discover Home Assistant instances on the local network via mDNS.

    Browses for ``_home-assistant._tcp`` services for *timeout* seconds.
    Returns a list of discovered instances (may be empty).
    """
    try:
        zc = Zeroconf()
    except OSError as exc:
        raise RuntimeError(f"mDNS discovery failed: {exc}") from None
    listener = _HAListener()
    browser = ServiceBrowser(zc, HA_SERVICE_TYPE, listener)
    try:
        time.sleep(timeout)
    finally:
        browser.cancel()
        zc.close()
    return listener.instances
