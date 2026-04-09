"""Unit tests for RaftLeaseManager (single-node Raft cluster).

These tests verify that the RaftLeaseManager presents the same public
interface as the standard LeaseManager and correctly proxies operations
through the Raft state machine.
"""

from __future__ import annotations

import os
import tempfile
import time
import unittest

from src.raft.config import RaftConfig
from src.raft.lease_manager import RaftLeaseManager, _dict_to_lease


def _make_raft_lm(tmpdir: str, port: int = 14400) -> RaftLeaseManager:
    """Build a single-node RaftLeaseManager for testing."""
    cfg = RaftConfig(
        enabled=True,
        bind_address=f'localhost:{port}',
        peers=[],
        data_dir=tmpdir,
        heartbeat_period=0.05,
        min_election_timeout=0.2,
        max_election_timeout=0.5,
        sync_timeout=5.0,
        commands_wait_leader=False,
    )
    lm = RaftLeaseManager(
        pool_start='10.1.0.100',
        pool_end='10.1.0.110',
        lease_time=300,
        raft_cfg=cfg,
    )
    return lm


def _wait_ready(lm: RaftLeaseManager, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if lm.is_leader:
            return
        time.sleep(0.05)
    raise TimeoutError('RaftLeaseManager did not become leader within timeout')


class TestRaftLeaseManagerInterface(unittest.TestCase):
    """Verify the public interface matches LeaseManager."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.lm = _make_raft_lm(self.tmpdir, port=14400)
        _wait_ready(self.lm)

    def tearDown(self):
        self.lm.stop()

    # ── offer_ip ─────────────────────────────────────────────────────

    def test_offer_ip_returns_string(self):
        ip = self.lm.offer_ip('aa:00:00:00:00:01')
        self.assertIsInstance(ip, str)

    def test_offer_ip_none_when_mac_known(self):
        mac = 'aa:00:00:00:01:01'
        self.lm.assign_ip(mac, 'h1')
        # Offer should return the already-assigned IP
        ip_offer = self.lm.offer_ip(mac)
        ip_lease = self.lm.get_lease(mac)
        self.assertIsNotNone(ip_lease)
        self.assertEqual(ip_offer, ip_lease.ip)

    # ── assign_ip ────────────────────────────────────────────────────

    def test_assign_ip_returns_ip(self):
        ip = self.lm.assign_ip('bb:00:00:00:00:01')
        self.assertIsNotNone(ip)

    def test_assign_ip_idempotent_for_same_mac(self):
        mac = 'bb:00:00:00:01:01'
        ip1 = self.lm.assign_ip(mac, 'h')
        ip2 = self.lm.assign_ip(mac, 'h')
        self.assertEqual(ip1, ip2)

    def test_assign_ip_different_macs_different_ips(self):
        ip1 = self.lm.assign_ip('cc:00:00:00:00:01')
        ip2 = self.lm.assign_ip('cc:00:00:00:00:02')
        self.assertNotEqual(ip1, ip2)

    # ── get_lease ────────────────────────────────────────────────────

    def test_get_lease_returns_lease_object(self):
        mac = 'dd:00:00:00:00:01'
        ip = self.lm.assign_ip(mac, 'myhost')
        lease = self.lm.get_lease(mac)
        self.assertIsNotNone(lease)
        self.assertEqual(lease.ip, ip)
        self.assertEqual(lease.mac, mac)
        self.assertEqual(lease.hostname, 'myhost')
        self.assertFalse(lease.is_expired)

    def test_get_lease_none_for_unknown_mac(self):
        self.assertIsNone(self.lm.get_lease('ff:ff:ff:ff:ff:ff'))

    # ── get_all_leases ───────────────────────────────────────────────

    def test_get_all_leases_empty_initially(self):
        self.assertEqual(self.lm.get_all_leases(), [])

    def test_get_all_leases_after_assign(self):
        self.lm.assign_ip('ee:00:00:00:00:01', 'h1')
        self.lm.assign_ip('ee:00:00:00:00:02', 'h2')
        leases = self.lm.get_all_leases()
        self.assertEqual(len(leases), 2)

    # ── release_ip ───────────────────────────────────────────────────

    def test_release_ip_removes_lease(self):
        mac = 'ff:00:00:00:00:01'
        self.lm.assign_ip(mac)
        time.sleep(0.2)  # allow async release to commit
        self.lm.release_ip(mac)
        time.sleep(0.3)
        self.assertIsNone(self.lm.get_lease(mac))

    # ── reservations ─────────────────────────────────────────────────

    def test_set_reservation_honoured_on_assign(self):
        mac = 'aa:11:00:00:00:01'
        self.lm.set_reservation(mac, '10.1.0.105')
        time.sleep(0.2)
        ip = self.lm.assign_ip(mac)
        self.assertEqual(ip, '10.1.0.105')

    def test_remove_reservation(self):
        mac = 'aa:11:00:00:00:02'
        self.lm.set_reservation(mac, '10.1.0.106')
        time.sleep(0.2)
        self.lm.remove_reservation(mac)
        time.sleep(0.2)
        self.assertNotIn(mac, self.lm.get_reservations())

    def test_get_reservations_returns_dict(self):
        self.assertIsInstance(self.lm.get_reservations(), dict)

    # ── purge_expired ────────────────────────────────────────────────

    def test_purge_expired_removes_stale_leases(self):
        mac = 'aa:22:00:00:00:01'
        cfg = RaftConfig(
            enabled=True,
            bind_address='localhost:14401',
            peers=[],
            data_dir=self.tmpdir + '_2',
            heartbeat_period=0.05,
            min_election_timeout=0.2,
            max_election_timeout=0.5,
            sync_timeout=5.0,
            commands_wait_leader=False,
        )
        os.makedirs(cfg.data_dir, exist_ok=True)
        lm2 = RaftLeaseManager(
            pool_start='10.1.1.100',
            pool_end='10.1.1.110',
            lease_time=1,   # 1-second leases
            raft_cfg=cfg,
        )
        try:
            _wait_ready(lm2)
            lm2.assign_ip(mac)
            time.sleep(1.5)
            removed = lm2.purge_expired()
            self.assertGreaterEqual(removed, 1)
            self.assertIsNone(lm2.get_lease(mac))
        finally:
            lm2.stop()

    # ── update_pool ──────────────────────────────────────────────────

    def test_update_pool_changes_range(self):
        self.lm.update_pool('10.1.0.200', '10.1.0.210')
        time.sleep(0.3)
        # Assign should now be in the new range
        ip = self.lm.assign_ip('aa:33:00:00:00:01')
        self.assertIsNotNone(ip)
        self.assertTrue(ip.startswith('10.1.0.2'))

    # ── cluster status ───────────────────────────────────────────────

    def test_is_leader(self):
        self.assertTrue(self.lm.is_leader)

    def test_is_ready(self):
        self.assertTrue(self.lm.is_ready)

    def test_get_cluster_status_keys(self):
        st = self.lm.get_cluster_status()
        for key in ('node', 'leader', 'is_leader', 'is_ready',
                    'raft_term', 'commit_index', 'last_applied'):
            self.assertIn(key, st)


class TestDictToLease(unittest.TestCase):
    """Verify the _dict_to_lease helper."""

    def test_basic_conversion(self):
        now = time.time()
        d = {
            'ip': '192.168.1.100',
            'mac': 'aa:bb:cc:dd:ee:ff',
            'hostname': 'myhost',
            'assigned_at': now - 60,
            'expires_at': now + 3600,
        }
        lease = _dict_to_lease(d)
        self.assertEqual(lease.ip, '192.168.1.100')
        self.assertEqual(lease.mac, 'aa:bb:cc:dd:ee:ff')
        self.assertEqual(lease.hostname, 'myhost')
        self.assertFalse(lease.is_expired)

    def test_expired_lease(self):
        now = time.time()
        d = {
            'ip': '192.168.1.101',
            'mac': 'aa:bb:cc:dd:ee:00',
            'hostname': '',
            'assigned_at': now - 7200,
            'expires_at': now - 3600,
        }
        lease = _dict_to_lease(d)
        self.assertTrue(lease.is_expired)


if __name__ == '__main__':
    unittest.main()
