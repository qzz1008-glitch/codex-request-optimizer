import concurrent.futures
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from packet_inspector.sharing_auth import SharingError
from packet_inspector.sharing_credentials import CredentialVault, expiration, week_start
from packet_inspector.sharing_pricing import budget_nanos, reference_cost


class ReferencePricingTests(unittest.TestCase):
    def test_cache_is_subtracted_once_and_reasoning_is_already_output(self):
        values = dict(model="gpt-6.1-sol", input_tokens=1000, cached_input_tokens=500,
                      cache_write_30m_tokens=100, cache_write_12h_tokens=100, output_tokens=100)
        self.assertEqual(reference_cost(values), 2_400_000)
        self.assertEqual(reference_cost({**values, "service_tier": "priority"}), 4_800_000)
        self.assertIsNone(reference_cost({**values, "cache_write_unknown": True}))

    def test_long_context_unknown_models_and_invalid_counts(self):
        self.assertEqual(reference_cost(dict(model="gpt-6.1-sol-2026-10-01", input_tokens=300000, output_tokens=1000)), 1_215_000_000)
        for values in [dict(model="future-model"), dict(model="gpt-6.1-sol", cached_input_tokens=100),
                       dict(model="gpt-6.1-sol", input_tokens=-1), dict(model="gpt-6.1-sol", service_tier="future-tier")]:
            self.assertIsNone(reference_cost(values))
        for value in ("nan", "inf", -1, "bad", 1_000_001):
            with self.assertRaises(ValueError):
                budget_nanos(value)
        self.assertEqual(budget_nanos("0.01"), 10_000_000)


class CredentialVaultTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.vault = CredentialVault(self.root)

    def grant(self, **kwargs):
        identifier, token = self.vault.issue("quota", "colleague", **kwargs)
        return identifier, "Bearer " + token

    def test_one_client_binding_is_atomic_and_not_an_ip_binding(self):
        identifier, token = self.grant()
        def attempt(client):
            try:
                self.vault.authenticate("quota", token, client)
                return client
            except SharingError:
                return None
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            accepted = list(pool.map(attempt, ["a" * 64, "b" * 64]))
        self.assertEqual(sum(value is not None for value in accepted), 1)
        winner = next(value for value in accepted if value)
        self.vault.authenticate("quota", token, winner)
        self.assertTrue(self.vault.rows()[0]["bound"])
        self.assertNotIn("client_hash", json.dumps(self.vault.rows()))
        for client in ("", "ip:192.168.1.2", "x" * 64):
            with self.assertRaises(SharingError):
                self.vault.authenticate("quota", token, client)

    def test_budget_live_edits_manual_reset_and_late_completion(self):
        identifier, token = self.grant(budget="0.01")
        reservation = self.vault.authenticate("quota", token, "a" * 64, spend=True)
        values = dict(model="gpt-6.1-sol", input_tokens=5000, output_tokens=1000, total_tokens=6000)
        self.vault.record(reservation, values)
        with self.assertRaises(SharingError):
            self.vault.authenticate("quota", token, "a" * 64, spend=True)
        self.vault.update(identifier, {}, reset=True)
        self.vault.record(reservation, values)  # Earlier period: lifetime only.
        row = self.vault.rows()[0]
        self.assertEqual(row["period_cost_nanos"], 0)
        self.assertEqual(row["totals"]["cost_nanos"], 40_000_000)
        self.assertEqual(row["week_cost_nanos"], 40_000_000)
        next_request = self.vault.authenticate("quota", token, "a" * 64, spend=True)
        self.vault.record(next_request, dict(input_tokens=1, output_tokens=0, total_tokens=1, model="unknown"))
        self.vault.record(next_request, missing=True)
        self.assertEqual(self.vault.rows()[0]["totals"]["unpriced_responses"], 1)
        self.assertEqual(self.vault.rows()[0]["totals"]["missing_responses"], 1)
        self.vault.update(identifier, {"enabled": False})
        with self.assertRaises(SharingError):
            self.vault.authenticate("quota", token, "a" * 64)

    def test_weekly_reset_keeps_history_and_does_not_recharge_old_period(self):
        identifier, token = self.grant(weekly_reset=True)
        reservation = self.vault.authenticate("quota", token, "a" * 64)
        self.vault.record(reservation, dict(model="gpt-6.1-sol", input_tokens=10))
        next_week = week_start() + 8 * 86400
        with patch("packet_inspector.sharing_credentials.time.time", return_value=next_week):
            row = self.vault.rows()[0]
            self.assertEqual(row["period_cost_nanos"], 0)
            self.assertEqual(row["week_cost_nanos"], 0)
            self.vault.record(reservation, dict(model="gpt-6.1-sol", input_tokens=10))
            self.assertEqual(self.vault.rows()[0]["period_cost_nanos"], 0)
        self.assertEqual(self.vault.rows()[0]["totals"]["cost_nanos"], 40_000)

    def test_persistence_secret_protection_expiry_and_incoming_history(self):
        identifier, token = self.grant(expires_at=time.time() + 60)
        second = CredentialVault(self.root)
        self.assertEqual(second.client_id, self.vault.client_id)
        self.assertEqual(second.get_secret(identifier), token[7:])
        if os.name == "nt":
            self.assertNotIn(token[7:].encode(), self.vault.path.read_bytes())
        with patch("packet_inspector.sharing_credentials.time.time", return_value=time.time() + 120):
            with self.assertRaises(SharingError):
                second.authenticate("quota", token, "a" * 64)
        incoming = self.vault.import_link("quota", "codex-share://127.0.0.1:1/#grant.fingerprint", "owner", {"totals": {"cost_nanos": 1}})
        self.assertEqual(incoming, self.vault.import_link("quota", "codex-share://127.0.0.2:2/#grant.fingerprint"))
        self.assertEqual(len(self.vault.rows()), 2)
        with self.assertRaises(SharingError):
            self.vault.update(incoming, {"budget": 100})
        self.assertIsNone(expiration(None))
        for value in (False, -1, "tomorrow"):
            with self.assertRaises(SharingError):
                expiration(value)

    def test_multiple_distinct_grants_and_exit_stats(self):
        first, token1 = self.grant()
        second, token2 = self.grant()
        a = self.vault.authenticate("quota", token1, "a" * 64)
        b = self.vault.authenticate("quota", token2, "b" * 64)
        self.vault.record(a, dict(model="gpt-6.1-sol", input_tokens=10))
        self.vault.record(b, dict(model="gpt-6.1-sol", input_tokens=20))
        self.assertNotEqual(self.vault.snapshot(a)["totals"], self.vault.snapshot(b)["totals"])
        identifier, token = self.vault.issue("exit", "network")
        reservation = self.vault.authenticate("exit", "Bearer " + token, "c" * 64, spend=True)
        self.vault.record(reservation, sent=100, received=200)
        self.assertEqual(self.vault.snapshot(reservation)["totals"]["sent_bytes"], 100)
