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

    def test_multiple_clients_share_one_budget_and_atomic_usage_totals(self):
        identifier, token = self.grant(budget="0.01")
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            reservations = list(pool.map(lambda client: self.vault.authenticate("quota", token, client, spend=True),
                                         ["a" * 64, "b" * 64]))
            self.assertEqual({value["credential_id"] for value in reservations}, {identifier})
            list(pool.map(lambda value: self.vault.record(value, dict(model="gpt-6.1-sol", input_tokens=2500)), reservations))
        row = self.vault.rows()[0]
        self.assertEqual(row["period_cost_nanos"], 10_000_000)
        self.assertEqual(row["totals"]["input_tokens"], 5000)
        self.assertEqual(row["totals"]["requests"], 2)
        self.assertTrue(row["exhausted"])
        self.assertNotIn("bound", row)
        self.assertNotIn("client_hash", row)
        self.vault.authenticate("quota", token)  # A client ID is no longer required.
        for client in ("", "ip:192.168.1.2", "x" * 64):
            self.vault.authenticate("quota", token, client)
            with self.assertRaisesRegex(SharingError, "本期额度已用完"):
                self.vault.authenticate("quota", token, client, spend=True)
        for invalid in (None, "", "Basic token", "Bearer unknown"):
            with self.assertRaises(SharingError):
                self.vault.authenticate("quota", invalid)

    def test_legacy_binding_is_removed_without_changing_grants_or_usage(self):
        identifier, token = self.grant(budget="10", expires_at=time.time() + 86400)
        reservation = self.vault.authenticate("quota", token, "a" * 64, spend=True)
        self.vault.record(reservation, dict(model="gpt-6.1-sol", input_tokens=2500))
        incoming = self.vault.import_link("quota", "codex-share://127.0.0.1:1/#grant.fingerprint", "saved")
        with self.vault.connection() as db:
            original = json.loads(db.execute("SELECT data FROM credentials WHERE id=?", (identifier,)).fetchone()[0])
            self.vault._save(db, identifier, {**original, "bound": True, "client_hash": "a" * 64})
            saved = json.loads(db.execute("SELECT data FROM credentials WHERE id=?", (incoming,)).fetchone()[0])
            saved["snapshot"] = {"bound": True, "client_hash": "b" * 64, "budget_nanos": 123}
            self.vault._save(db, incoming, saved)
        upgraded = CredentialVault(self.root)
        with upgraded.connection() as db:
            self.assertEqual(json.loads(db.execute("SELECT data FROM credentials WHERE id=?", (identifier,)).fetchone()[0]), original)
            snapshot = json.loads(db.execute("SELECT data FROM credentials WHERE id=?", (incoming,)).fetchone()[0])["snapshot"]
            self.assertEqual(snapshot, {"budget_nanos": 123})
        self.assertEqual(upgraded.get_secret(identifier), token[7:])
        self.assertEqual(upgraded.get_secret(incoming), "codex-share://127.0.0.1:1/#grant.fingerprint")
        upgraded.authenticate("quota", token, "b" * 64, spend=True)
        upgraded.authenticate("quota", token, spend=True)

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
