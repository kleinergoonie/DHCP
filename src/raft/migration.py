"""Migration helpers – import existing in-memory / JSON leases into the Raft cluster.

Typical usage
-------------
First startup with an existing ``config.json`` lease database (if any):

.. code-block:: python

    from src.lease_manager import LeaseManager
    from src.raft.migration import migrate_from_lease_manager, migrate_from_json

    # Option A – migrate from a running LeaseManager (in-process)
    migrate_from_lease_manager(old_lm, raft_lm, timeout=30)

    # Option B – migrate from a JSON lease dump file
    migrate_from_json('/var/lib/dhcp/leases.json', raft_lm, timeout=30)

The migration is idempotent: already-committed leases are overwritten with
the same data, and already-expired leases are skipped automatically.

JSON lease dump format
----------------------
A JSON file containing a top-level object with two optional keys::

    {
        "leases": [
            {
                "ip": "192.168.1.101",
                "mac": "aa:bb:cc:dd:ee:ff",
                "hostname": "myhost",
                "assigned_at": 1700000000.0,
                "expires_at": 1700086400.0
            }
        ],
        "reservations": {
            "aa:bb:cc:dd:ee:ff": "192.168.1.101"
        }
    }
"""

from __future__ import annotations

import json
import logging
import time
from typing import TYPE_CHECKING

from pysyncobj import SyncObjException

if TYPE_CHECKING:
    from ..lease_manager import LeaseManager
    from .lease_manager import RaftLeaseManager

logger = logging.getLogger(__name__)


def migrate_from_lease_manager(
    source: 'LeaseManager',
    target: 'RaftLeaseManager',
    timeout: float = 30.0,
) -> tuple[int, int]:
    """Copy all active leases and reservations from *source* into *target*.

    Parameters
    ----------
    source:
        A running in-memory :class:`~src.lease_manager.LeaseManager`.
    target:
        A running :class:`~src.raft.lease_manager.RaftLeaseManager` whose
        Raft cluster must already have a leader.
    timeout:
        Wait up to this many seconds for the Raft cluster to become ready
        before aborting.

    Returns
    -------
    (leases_imported, reservations_imported)
    """
    if not target.wait_ready(timeout):
        raise TimeoutError(
            f'Raft cluster did not become ready within {timeout}s'
        )
    if not target.is_leader:
        raise RuntimeError(
            'migrate_from_lease_manager must be called on the Raft leader'
        )

    leases = [
        {
            'ip': lease.ip,
            'mac': lease.mac,
            'hostname': lease.hostname,
            'assigned_at': lease.assigned_at,
            'expires_at': lease.expires_at,
        }
        for lease in source.get_all_leases()
    ]

    reservations = source.get_reservations()

    return _do_import(target, leases, reservations)


def migrate_from_json(
    path: str,
    target: 'RaftLeaseManager',
    timeout: float = 30.0,
) -> tuple[int, int]:
    """Load leases and reservations from a JSON dump file and import into *target*.

    Parameters
    ----------
    path:
        Path to the JSON dump file (see module docstring for schema).
    target:
        A running :class:`~src.raft.lease_manager.RaftLeaseManager`.
    timeout:
        Raft readiness timeout.

    Returns
    -------
    (leases_imported, reservations_imported)
    """
    with open(path, encoding='utf-8') as fh:
        data = json.load(fh)

    leases: list[dict] = data.get('leases', [])
    reservations: dict[str, str] = data.get('reservations', {})

    if not target.wait_ready(timeout):
        raise TimeoutError(
            f'Raft cluster did not become ready within {timeout}s'
        )
    if not target.is_leader:
        raise RuntimeError(
            'migrate_from_json must be called on the Raft leader'
        )

    return _do_import(target, leases, reservations)


def dump_leases_to_json(
    source: 'LeaseManager',
    path: str,
) -> None:
    """Dump current leases and reservations from *source* to a JSON file.

    This can be used before switching to Raft mode to create a snapshot
    that :func:`migrate_from_json` can later import.
    """
    now = time.time()
    leases = [
        {
            'ip': lease.ip,
            'mac': lease.mac,
            'hostname': lease.hostname,
            'assigned_at': lease.assigned_at,
            'expires_at': lease.expires_at,
        }
        for lease in source.get_all_leases()
        if lease.expires_at > now
    ]
    reservations = source.get_reservations()

    with open(path, 'w', encoding='utf-8') as fh:
        json.dump({'leases': leases, 'reservations': reservations}, fh, indent=2)

    logger.info('Dumped %d leases and %d reservations to %s',
                len(leases), len(reservations), path)


# ------------------------------------------------------------------
# Internal helpers
# ------------------------------------------------------------------

def _do_import(
    target: 'RaftLeaseManager',
    leases: list[dict],
    reservations: dict[str, str],
) -> tuple[int, int]:
    sm = target._sm  # noqa: SLF001

    n_leases = 0
    n_res = 0

    try:
        n_leases = sm.import_leases(
            leases,
            sync=True,
            timeout=30.0,
        ) or 0
        logger.info('Imported %d leases via Raft', n_leases)
    except SyncObjException as exc:
        logger.error('Failed to import leases: %s', exc)

    if reservations:
        try:
            sm.import_reservations(
                reservations,
                sync=True,
                timeout=30.0,
            )
            n_res = len(reservations)
            logger.info('Imported %d reservations via Raft', n_res)
        except SyncObjException as exc:
            logger.error('Failed to import reservations: %s', exc)

    return n_leases, n_res
