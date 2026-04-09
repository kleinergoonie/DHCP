"""IP lease manager – tracks assigned IP addresses and their expiry times."""

import ipaddress
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Callable


@dataclass
class Lease:
    ip: str
    mac: str
    hostname: str
    assigned_at: float = field(default_factory=time.time)
    expires_at: float  = 0.0   # absolute UNIX timestamp

    @property
    def is_expired(self) -> bool:
        return time.time() > self.expires_at

    @property
    def remaining(self) -> int:
        """Remaining lease time in seconds (0 if expired)."""
        return max(0, int(self.expires_at - time.time()))

    @property
    def ttl_str(self) -> str:
        s = self.remaining
        if s <= 0:
            return 'Abgelaufen'
        h, rem = divmod(s, 3600)
        m, sec = divmod(rem, 60)
        return f'{h:02d}:{m:02d}:{sec:02d}'


class LeaseManager:
    """Thread-safe pool of IP addresses with lease tracking."""

    def __init__(
        self,
        pool_start: str,
        pool_end: str,
        lease_time: int = 86400,
        on_change: Callable | None = None,
    ):
        self._lock        = threading.Lock()
        self.lease_time   = lease_time     # seconds
        self.on_change    = on_change      # called whenever leases change

        # Build the ordered list of all IPs in the pool
        self._pool: list[str] = self._build_pool(pool_start, pool_end)

        # mac  -> Lease
        self._leases: dict[str, Lease] = {}

        # ip -> mac (reverse lookup)
        self._ip_to_mac: dict[str, str] = {}

        # Static reservations: mac -> ip
        self._reservations: dict[str, str] = {}

    # ------------------------------------------------------------------
    # Pool management
    # ------------------------------------------------------------------

    @staticmethod
    def _build_pool(start: str, end: str) -> list[str]:
        s = int(ipaddress.IPv4Address(start))
        e = int(ipaddress.IPv4Address(end))
        if s > e:
            raise ValueError(f'Pool start {start} is after pool end {end}')
        return [str(ipaddress.IPv4Address(i)) for i in range(s, e + 1)]

    def update_pool(self, pool_start: str, pool_end: str) -> None:
        with self._lock:
            self._pool = self._build_pool(pool_start, pool_end)
            # Remove leases for IPs no longer in the pool
            pool_set = set(self._pool)
            for mac in list(self._leases):
                if self._leases[mac].ip not in pool_set:
                    del self._ip_to_mac[self._leases[mac].ip]
                    del self._leases[mac]

    # ------------------------------------------------------------------
    # Reservations
    # ------------------------------------------------------------------

    def set_reservation(self, mac: str, ip: str) -> None:
        with self._lock:
            self._reservations[mac.lower()] = ip

    def remove_reservation(self, mac: str) -> None:
        with self._lock:
            self._reservations.pop(mac.lower(), None)

    def get_reservations(self) -> dict[str, str]:
        with self._lock:
            return dict(self._reservations)

    # ------------------------------------------------------------------
    # Offer / assign
    # ------------------------------------------------------------------

    def offer_ip(self, mac: str, requested_ip: str | None = None) -> str | None:
        """Return an IP to offer without actually assigning it."""
        with self._lock:
            mac = mac.lower()
            # Static reservation takes priority
            if mac in self._reservations:
                return self._reservations[mac]
            # Client already has a valid lease
            if mac in self._leases and not self._leases[mac].is_expired:
                return self._leases[mac].ip
            # Honour requested IP if it is free
            if requested_ip and self._is_available(requested_ip):
                return requested_ip
            # Find the first free IP
            return self._next_free()

    def assign_ip(
        self,
        mac: str,
        hostname: str = '',
        requested_ip: str | None = None,
    ) -> str | None:
        """Assign (or renew) a lease and return the assigned IP."""
        with self._lock:
            mac = mac.lower()

            # Static reservation
            if mac in self._reservations:
                ip = self._reservations[mac]
                self._create_lease(mac, ip, hostname)
                return ip

            # Renew existing lease
            if mac in self._leases:
                lease = self._leases[mac]
                if not lease.is_expired or self._is_available(lease.ip):
                    self._create_lease(mac, lease.ip, hostname or lease.hostname)
                    return lease.ip

            # Honour requested IP
            if requested_ip and self._is_available(requested_ip):
                self._create_lease(mac, requested_ip, hostname)
                return requested_ip

            # Pick next free
            ip = self._next_free()
            if ip:
                self._create_lease(mac, ip, hostname)
            return ip

    def release_ip(self, mac: str) -> None:
        with self._lock:
            mac = mac.lower()
            if mac in self._leases:
                ip = self._leases[mac].ip
                self._ip_to_mac.pop(ip, None)
                del self._leases[mac]
                self._notify()

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def get_lease(self, mac: str) -> Lease | None:
        with self._lock:
            return self._leases.get(mac.lower())

    def get_all_leases(self) -> list[Lease]:
        with self._lock:
            return list(self._leases.values())

    def purge_expired(self) -> int:
        """Remove expired leases; returns the number removed."""
        with self._lock:
            expired = [
                mac for mac, lease in self._leases.items() if lease.is_expired
            ]
            for mac in expired:
                self._ip_to_mac.pop(self._leases[mac].ip, None)
                del self._leases[mac]
            if expired:
                self._notify()
            return len(expired)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _is_available(self, ip: str) -> bool:
        if ip not in self._pool:
            return False
        if ip not in self._ip_to_mac:
            return True
        mac = self._ip_to_mac[ip]
        return self._leases.get(mac, None) is None or self._leases[mac].is_expired

    def _next_free(self) -> str | None:
        for ip in self._pool:
            if self._is_available(ip):
                return ip
        return None

    def _create_lease(self, mac: str, ip: str, hostname: str) -> None:
        # Release any old lease for this MAC
        old_lease = self._leases.get(mac)
        if old_lease:
            self._ip_to_mac.pop(old_lease.ip, None)

        now = time.time()
        lease = Lease(
            ip          = ip,
            mac         = mac,
            hostname    = hostname,
            assigned_at = now,
            expires_at  = now + self.lease_time,
        )
        self._leases[mac]    = lease
        self._ip_to_mac[ip]  = mac
        self._notify()

    def _notify(self) -> None:
        if self.on_change:
            try:
                self.on_change()
            except Exception:
                pass
