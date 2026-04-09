"""Raft-backed state machine for DHCP leases and reservations.

All write operations are decorated with ``@replicated`` from pysyncobj, which
means they are committed to the Raft log and applied in the same order on
every cluster member, preventing split-brain and IP conflicts.

Read operations (``get_lease``, ``offer_ip``, etc.) access the node-local in-
memory state directly without touching the Raft log, which keeps reads fast.
"""

from __future__ import annotations

import ipaddress
import logging
import time

from pysyncobj import SyncObj, SyncObjConf, replicated

logger = logging.getLogger(__name__)


class DHCPStateMachine(SyncObj):
    """
    Replicated state machine for DHCP leases and static reservations.

    Internals
    ---------
    * ``_leases``       – ``{mac: {ip, mac, hostname, assigned_at, expires_at}}``
    * ``_reservations`` – ``{mac: ip}``

    Both dicts are plain Python dicts stored as instance attributes and are
    serialised automatically by pysyncobj's pickle-based snapshot mechanism
    (triggered by :attr:`~pysyncobj.SyncObjConf.fullDumpFile`).
    """

    def __init__(
        self,
        self_addr: str,
        partners: list[str],
        conf: SyncObjConf | None = None,
    ) -> None:
        self._leases: dict[str, dict] = {}
        self._reservations: dict[str, str] = {}
        super().__init__(self_addr, partners, conf=conf)

    # ------------------------------------------------------------------
    # Read operations (local, no Raft overhead)
    # ------------------------------------------------------------------

    def get_lease(self, mac: str) -> dict | None:
        """Return the lease dict for *mac*, or None."""
        return self._leases.get(mac.lower())

    def get_all_leases(self) -> list[dict]:
        """Return a snapshot of all current lease dicts."""
        return list(self._leases.values())

    def get_reservations(self) -> dict[str, str]:
        """Return a copy of the static reservations map."""
        return dict(self._reservations)

    def offer_ip(
        self,
        mac: str,
        requested_ip: str | None,
        pool: list[str],
    ) -> str | None:
        """Compute the IP to *offer* without committing (DHCPDISCOVER).

        This is a local read and does **not** reserve the IP.  The actual
        assignment happens via :meth:`try_assign_ip`.
        """
        mac = mac.lower()
        now = time.time()

        if mac in self._reservations:
            return self._reservations[mac]

        if mac in self._leases and self._leases[mac].get('expires_at', 0) >= now:
            return self._leases[mac]['ip']

        if requested_ip and self._ip_available(requested_ip, pool, now):
            return requested_ip

        return self._next_free(pool, now)

    # ------------------------------------------------------------------
    # Write operations – all replicated via Raft
    # ------------------------------------------------------------------

    @replicated
    def try_assign_ip(
        self,
        mac: str,
        hostname: str,
        requested_ip: str | None,
        pool_start: str,
        pool_end: str,
        lease_time: int,
    ):
        """Atomically assign (or renew) an IP lease.

        This method runs in the Raft commit order on **all** cluster members,
        so there are no races or double-allocations even under concurrent
        client requests.

        Returns the assigned IP string on success, or ``None`` if the pool
        is exhausted or no suitable IP is available.
        """
        mac = mac.lower()
        now = time.time()
        pool = self._build_pool(pool_start, pool_end)

        # 1. Static reservation takes highest priority
        if mac in self._reservations:
            ip = self._reservations[mac]
            self._write_lease(mac, ip, hostname, now, lease_time)
            return ip

        # 2. Renew an existing lease whose IP is still usable
        if mac in self._leases:
            existing = self._leases[mac]
            ip = existing['ip']
            if existing.get('expires_at', 0) >= now or self._ip_available(ip, pool, now):
                self._write_lease(
                    mac, ip,
                    hostname or existing.get('hostname', ''),
                    now, lease_time,
                )
                return ip

        # 3. Honour the client's requested IP (if free)
        if requested_ip and self._ip_available(requested_ip, pool, now):
            self._write_lease(mac, requested_ip, hostname, now, lease_time)
            return requested_ip

        # 4. Pick the first available IP in the pool
        ip = self._next_free(pool, now)
        if ip:
            self._write_lease(mac, ip, hostname, now, lease_time)
        return ip

    @replicated
    def release_ip(self, mac: str):
        """Release the lease held by *mac*."""
        self._leases.pop(mac.lower(), None)

    @replicated
    def set_reservation(self, mac: str, ip: str):
        """Add or update a static reservation."""
        self._reservations[mac.lower()] = ip

    @replicated
    def remove_reservation(self, mac: str):
        """Remove a static reservation."""
        self._reservations.pop(mac.lower(), None)

    @replicated
    def purge_expired(self):
        """Remove all leases whose expiry time has passed.

        Returns the number of leases removed.
        """
        now = time.time()
        expired = [
            m for m, d in list(self._leases.items())
            if d.get('expires_at', 0) < now
        ]
        for m in expired:
            del self._leases[m]
        return len(expired)

    @replicated
    def update_pool(self, pool_start: str, pool_end: str):
        """Remove leases for IPs that are no longer in the new pool."""
        pool_set = set(self._build_pool(pool_start, pool_end))
        for mac in list(self._leases.keys()):
            if self._leases[mac]['ip'] not in pool_set:
                del self._leases[mac]

    @replicated
    def import_leases(self, leases: list[dict]):
        """Bulk-import lease dicts (used by the migration tool).

        Already-expired leases are silently skipped.
        """
        now = time.time()
        imported = 0
        for lease in leases:
            if lease.get('expires_at', 0) > now:
                mac = lease['mac'].lower()
                self._leases[mac] = dict(lease)
                imported += 1
        return imported

    @replicated
    def import_reservations(self, reservations: dict[str, str]):
        """Bulk-import static reservations (used by the migration tool)."""
        for mac, ip in reservations.items():
            self._reservations[mac.lower()] = ip

    # ------------------------------------------------------------------
    # Internal helpers (called *only* inside @replicated methods)
    # ------------------------------------------------------------------

    @staticmethod
    def _build_pool(pool_start: str, pool_end: str) -> list[str]:
        s = int(ipaddress.IPv4Address(pool_start))
        e = int(ipaddress.IPv4Address(pool_end))
        return [str(ipaddress.IPv4Address(i)) for i in range(s, e + 1)]

    def _ip_available(self, ip: str, pool: list[str], now: float) -> bool:
        if ip not in pool:
            return False
        for lease in self._leases.values():
            if lease['ip'] == ip and lease.get('expires_at', 0) >= now:
                return False
        return True

    def _next_free(self, pool: list[str], now: float) -> str | None:
        for ip in pool:
            if self._ip_available(ip, pool, now):
                return ip
        return None

    def _write_lease(
        self,
        mac: str,
        ip: str,
        hostname: str,
        now: float,
        lease_time: int,
    ) -> None:
        self._leases[mac] = {
            'ip': ip,
            'mac': mac,
            'hostname': hostname,
            'assigned_at': now,
            'expires_at': now + lease_time,
        }
