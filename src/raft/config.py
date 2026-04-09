"""Configuration dataclass for the Raft subsystem."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass
class RaftConfig:
    """All tunables for the Raft consensus layer.

    Every field maps to a key in the server's ``config.json`` under the
    ``raft`` namespace, or can be supplied as a flat dict produced by
    :meth:`from_dict`.
    """

    # ── Identity & network ────────────────────────────────────────────
    enabled: bool = False
    """Set to True to activate Raft mode."""

    node_id: str = ''
    """Unique node identifier (used for logging; must be unique in cluster)."""

    bind_address: str = '0.0.0.0:4321'
    """Address:port this Raft node advertises to peers (e.g. ``192.168.1.1:4321``)."""

    peers: list[str] = field(default_factory=list)
    """List of partner node addresses (e.g. ``['192.168.1.2:4321', '192.168.1.3:4321']``)."""

    # ── Storage ───────────────────────────────────────────────────────
    data_dir: str = './raft_data'
    """Directory for Raft snapshot and journal files."""

    # ── Timing ────────────────────────────────────────────────────────
    heartbeat_period: float = 0.1
    """Seconds between heartbeat (appendEntries) messages. Must be < min_election_timeout / 3."""

    min_election_timeout: float = 0.5
    """Minimum leader election timeout in seconds (must be > heartbeat_period * 3)."""

    max_election_timeout: float = 1.4
    """Maximum leader election timeout in seconds (must be > min_election_timeout)."""

    # ── Log compaction ────────────────────────────────────────────────
    snapshot_min_entries: int = 5000
    """Trigger log compaction after this many uncommitted entries."""

    snapshot_min_time: int = 300
    """Trigger log compaction at most once every N seconds."""

    # ── Encryption ────────────────────────────────────────────────────
    password: str = ''
    """Session encryption password (requires ``cryptography`` package).
    Leave empty to disable encryption."""

    # ── Behaviour ─────────────────────────────────────────────────────
    allow_follower_renewals: bool = False
    """If True, followers may serve DHCPREQUEST renewal for *existing* leases.

    **Trade-off**: improves availability during leader-less periods, but
    a split-brain could theoretically allow two nodes to renew the same
    lease with different expiry times.  Use only if you understand the
    risk and need higher renewal availability.
    """

    commands_wait_leader: bool = True
    """If True, commands queue until a leader is elected (safer).
    If False, commands fail immediately when no leader is available."""

    # ── Metrics ───────────────────────────────────────────────────────
    metrics_port: int = 9090
    """TCP port for the Prometheus-compatible HTTP metrics endpoint.
    Set to 0 to disable."""

    # ── Dynamic membership ────────────────────────────────────────────
    dynamic_membership: bool = False
    """Enable runtime cluster membership changes (addNode / removeNode)."""

    # ── Read timeout for sync Raft calls ─────────────────────────────
    sync_timeout: float = 5.0
    """Seconds to wait for a synchronous Raft commit before giving up."""

    # ------------------------------------------------------------------

    @classmethod
    def from_dict(cls, d: dict) -> 'RaftConfig':
        """Build a :class:`RaftConfig` from (a subset of) a config dict.

        Keys are looked up with a ``raft_`` prefix first, then without.
        For example ``{'raft_bind_address': '1.2.3.4:4321'}`` maps to
        :attr:`bind_address`.
        """

        def get(key: str, default=None):
            return d.get(f'raft_{key}', d.get(key, default))

        cfg = cls()
        cfg.enabled = bool(get('enabled', False))
        cfg.node_id = str(get('node_id', '') or '')
        cfg.bind_address = str(get('bind_address', cfg.bind_address))
        raw_peers = get('peers', [])
        if isinstance(raw_peers, str):
            raw_peers = [p.strip() for p in raw_peers.split(',') if p.strip()]
        cfg.peers = list(raw_peers)
        cfg.data_dir = str(get('data_dir', cfg.data_dir))
        cfg.heartbeat_period = float(get('heartbeat_period', cfg.heartbeat_period))
        cfg.min_election_timeout = float(get('min_election_timeout', cfg.min_election_timeout))
        cfg.max_election_timeout = float(get('max_election_timeout', cfg.max_election_timeout))
        cfg.snapshot_min_entries = int(get('snapshot_min_entries', cfg.snapshot_min_entries))
        cfg.snapshot_min_time = int(get('snapshot_min_time', cfg.snapshot_min_time))
        cfg.password = str(get('password', '') or '')
        cfg.allow_follower_renewals = bool(get('allow_follower_renewals', False))
        cfg.commands_wait_leader = bool(get('commands_wait_leader', True))
        cfg.metrics_port = int(get('metrics_port', cfg.metrics_port))
        cfg.dynamic_membership = bool(get('dynamic_membership', False))
        cfg.sync_timeout = float(get('sync_timeout', cfg.sync_timeout))
        return cfg

    def pysyncobj_conf(self):
        """Return a :class:`pysyncobj.SyncObjConf` built from this config."""
        from pysyncobj import SyncObjConf

        os.makedirs(self.data_dir, exist_ok=True)

        kwargs: dict = {
            'appendEntriesPeriod': self.heartbeat_period,
            'raftMinTimeout': self.min_election_timeout,
            'raftMaxTimeout': self.max_election_timeout,
            # connectionTimeout must be >= raftMaxTimeout (pysyncobj requirement)
            'connectionTimeout': max(self.max_election_timeout + 2.0, 3.5),
            'logCompactionMinEntries': self.snapshot_min_entries,
            'logCompactionMinTime': self.snapshot_min_time,
            'fullDumpFile': os.path.join(self.data_dir, 'snapshot.bin'),
            'journalFile': os.path.join(self.data_dir, 'journal.bin'),
            'commandsWaitLeader': self.commands_wait_leader,
            'dynamicMembershipChange': self.dynamic_membership,
        }
        if self.password:
            kwargs['password'] = self.password.encode()

        return SyncObjConf(**kwargs)
