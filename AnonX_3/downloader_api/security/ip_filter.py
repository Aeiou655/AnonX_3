"""IP address filtering."""

import logging
from typing import Set, Optional
from ipaddress import ip_address, ip_network

logger = logging.getLogger(__name__)


class IPFilter:
    def __init__(self):
        self._whitelist: Set[str] = set()
        self._blacklist: Set[str] = set()
        self._enabled = False

    def enable(self) -> None:
        self._enabled = True

    def disable(self) -> None:
        self._enabled = False

    def add_to_whitelist(self, ip: str) -> None:
        self._whitelist.add(ip)

    def add_to_blacklist(self, ip: str) -> None:
        self._blacklist.add(ip)

    def remove_from_whitelist(self, ip: str) -> None:
        self._whitelist.discard(ip)

    def remove_from_blacklist(self, ip: str) -> None:
        self._blacklist.discard(ip)

    def is_allowed(self, ip: str) -> bool:
        if not self._enabled:
            return True

        if ip in self._blacklist:
            return False

        if self._whitelist and ip not in self._whitelist:
            return False

        return True

    def is_private_ip(self, ip: str) -> bool:
        try:
            addr = ip_address(ip)
            return addr.is_private
        except ValueError:
            return False

    def is_localhost(self, ip: str) -> bool:
        return ip in ("127.0.0.1", "::1", "localhost")


ip_filter = IPFilter()
