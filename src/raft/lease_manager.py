"""Raft-backed lease manager – drop-in replacement for LeaseManager.

This class wraps DHCPStateMachine and exposes the same public API as the
original in-memory LeaseManager, making it a transparent swap.

Design notes
------------
* Writes (assign, release, reservation ops, purge, update_pool) go through
  the Raft log via sync=True calls.  Only the leader will actually commit;
  followers raise SyncObjException which is caught and returned as None/False.
* Reads (offer_ip, get_lease, get_all_leases, get_reservations) are served
  from node-local in-memory state – no Raft round-trip.
* The ``on_change`` callback is invoked whenever the background tick detects
  a new commit index (covers both local commits and follower replication).
"""

from __future__ import annotations

import ipaddress
import logging
import threading
import time
from typing import Callable

from pysyncobj import SyncObjException

from ..lease_manager import Lease
from .config import RaftConfig
from .state_machine import DHCPStateMachine

logger = logging.getLogger(__name__)


def _dict_to_lease(d: dict) -> Lease:
    """Convert a lease dict stored in the state machine to a Lease dataclass."""
    return Lease(
        ip=d['ip'],
        mac=d['mac'],
        hostname=d.get('hostname', ''),
        assigned_at=d.get('assigned_at', 0.0),
        expires_at=d.get('expires_at', 0.0),
    )


class RaftLeaseManager:
    """Thread-safe, Raft-backed lease manager.

    Parameters
    ----------
    pool_start, pool_end:
        IPv4 range for dynamic allocation.
    lease_time:
        Default lease duration in seconds.
    raft_cfg:
        Raft configuration.
    on_change:
        Optional callback invoked whenever the lease table changes
        (called on *all* cluster members after each committed write).
    """

    def __init__(
        self,
        pool_start: str,
        pool_end: str,
        lease_time: int,
        raft_cfg: RaftConfig,
        on_change: Callable | None = None,
    ) -> None:
        self._pool_start = pool_start
        self._pool_end = pool_end
        self._pool: list[str] = self._build_pool(pool_start, pool_end)
        self.lease_time = lease_time
        self._raft_cfg = raft_cfg
        self._on_change = on_change
        self._lock = threading.Lock()

        # Track commit index to detect remote commits on followers
        self._last_notified_idx: int = -1

        sm_conf = raft_cfg.pysyncobj_conf()

        self._sm = DHCPStateMachine(
            self_addr=raft_cfg.bind_address,
            partners=list(raft_cfg.peers),
            conf=sm_conf,
        )

        # Poll for committed changes every Raft tick
        self._sm.addOnTickCallback(self._tick_check)

    # ------------------------------------------------------------------
    # Cluster status helpers (delegates to pysyncobj APIs)
    # ------------------------------------------------------------------

    @property
    def is_leader(self) -> bool:
        """True when this node is the current Raft leader."""
        try:
            st = self._sm.getStatus()
            return st.get('leader') == self._sm.selfNode
        except Exception:
            return False

    @property
    def is_ready(self) -> bool:
        """True when this node has synced with the cluster."""
        return self._sm.isReady()

    def get_cluster_status(self) -> dict:
        """Return a JSON-serialisable status dict (for metrics / health)."""
        try:
            st = self._sm.getStatus()
        except Exception:
            st = {}
        is_ldr = self.is_leader
        return {
            'node': str(self._sm.selfNode),
            'leader': str(st.get('leader', '')),
            'is_leader': is_ldr,
            'is_ready': self._sm.isReady(),
            'raft_term': int(self._sm.raftCurrentTerm),
            'commit_index': int(self._sm.raftCommitIndex),
            'last_applied': int(self._sm.raftLastApplied),
            'has_quorum': bool(self._sm.hasQuorum),
            'partner_count': int(st.get('partner_nodes_count', 0)),
            'lease_count': len(self._sm._leases),
        }

    def wait_ready(self, timeout: float = 30.0) -> bool:
        """Block until the node is ready (synced) or *timeout* expires."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._sm.isReady():
                return True
            time.sleep(0.1)
        return False

    def stop(self) -> None:
        """Gracefully stop the Raft engine."""
        try:
            self._sm.destroy()
        except Exception:
            pass

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
        """Replicate a pool change across the cluster."""
        with self._lock:
            self._pool_start = pool_start
            self._pool_end = pool_end
            self._pool = self._build_pool(pool_start, pool_end)
        self._call_replicated(
            self._sm.update_pool, pool_start, pool_end
        )

    # ------------------------------------------------------------------
    # Reservations
    # ------------------------------------------------------------------

    def set_reservation(self, mac: str, ip: str) -> None:
        self._call_replicated(self._sm.set_reservation, mac.lower(), ip)

    def remove_reservation(self, mac: str) -> None:
        self._call_replicated(self._sm.remove_reservation, mac.lower())

    def get_reservations(self) -> dict[str, str]:
        return self._sm.get_reservations()

    # ------------------------------------------------------------------
    # Offer / assign
    # ------------------------------------------------------------------

    def offer_ip(self, mac: str, requested_ip: str | None = None) -> str | None:
        """Return a candidate IP to offer (local read, no Raft commit)."""
        return self._sm.offer_ip(mac.lower(), requested_ip, list(self._pool))

    def assign_ip(
        self,
        mac: str,
        hostname: str = '',
        requested_ip: str | None = None,
    ) -> str | None:
        """Atomically assign (or renew) a lease via the Raft log.

        Returns the assigned IP on success, or None if the pool is
        exhausted, this node cannot commit, or the call times out.
        """
        try:
            result = self._sm.try_assign_ip(
                mac.lower(),
                hostname,
                requested_ip,
                self._pool_start,
                self._pool_end,
                self.lease_time,
                sync=True,
                timeout=self._raft_cfg.sync_timeout,
            )
            return result
        except SyncObjException as exc:
            logger.warning('Raft assign_ip failed: %s', exc)
            return None

    def release_ip(self, mac: str) -> None:
        self._call_replicated(self._sm.release_ip, mac.lower())

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def get_lease(self, mac: str) -> Lease | None:
        d = self._sm.get_lease(mac.lower())
        return _dict_to_lease(d) if d else None

    def get_all_leases(self) -> list[Lease]:
        return [_dict_to_lease(d) for d in self._sm.get_all_leases()]

    def purge_expired(self) -> int:
        """Remove expired leases; returns the number removed."""
        try:
            result = self._sm.purge_expired(
                sync=True, timeout=self._raft_cfg.sync_timeout
            )
            return result or 0
        except SyncObjException as exc:
            logger.warning('Raft purge_expired failed: %s', exc)
            return 0

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _call_replicated(self, method, *args) -> None:
        """Fire-and-forget wrapper for replicated write methods that do not
        need a return value (release, set_reservation, etc.).
        """
        def _cb(result, error):
            if error:
                logger.warning(
                    'Raft %s failed (error=%s)', method.__name__, error
                )

        try:
            method(*args, callback=_cb)
        except SyncObjException as exc:
            logger.warning('Raft call %s failed: %s', method.__name__, exc)

    def _tick_check(self) -> None:
        """Detect commits (local or replicated) via raftLastApplied."""
        current_idx = self._sm.raftLastApplied
        if current_idx != self._last_notified_idx:
            self._last_notified_idx = current_idx
            if self._on_change:
                try:
                    self._on_change()
                except Exception:
                    pass
