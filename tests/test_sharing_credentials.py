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
from packet_inspector.sharing_credentials import CredentialVault, ExitMachineMismatch, expiration, week_start
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
        for value in ("nan", "inf", -1, 0, "0.99", "bad", 1_000_001):
            with self.assertRaises(ValueError):
                budget_nanos(value)
        self.assertEqual(budget_nanos("1"), 1_000_000_000)


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
        identifier, token = self.grant(budget="1")
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            reservations = list(pool.map(lambda client: self.vault.authenticate("quota", token, client, spend=True),
                                         ["a" * 64, "b" * 64]))
            self.assertEqual({value["credential_id"] for value in reservations}, {identifier})
            list(pool.map(lambda value: self.vault.record(value, dict(model="gpt-6.1-sol", input_tokens=250000)), reservations))
        row = self.vault.rows()[0]
        self.assertEqual(row["period_cost_nanos"], 1_000_000_000)
        self.assertEqual(row["totals"]["input_tokens"], 500000)
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

    def test_exit_binding_is_atomic_persistent_and_private(self):
        identifier, token = self.vault.issue("exit", "one computer")
        bearer = "Bearer " + token
        for invalid in ("", "ip:192.168.1.2", "x" * 64):
            with self.assertRaises(SharingError):
                self.vault.authenticate("exit", bearer, invalid)
        def claim(client):
            try:
                self.vault.authenticate("exit", bearer, client)
                return client
            except ExitMachineMismatch:
                return None
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            accepted = list(pool.map(claim, ["a" * 64, "b" * 64]))
        self.assertEqual(sum(client is not None for client in accepted), 1)
        winner = next(client for client in accepted if client)
        loser = "b" * 64 if winner == "a" * 64 else "a" * 64
        upgraded = CredentialVault(self.root)
        reservation = upgraded.authenticate("exit", bearer, winner, spend=True)
        upgraded.record(reservation, sent=100, received=200)
        upgraded.update(identifier, {"label": "renamed", "enabled": False})
        with self.assertRaises(SharingError):
            upgraded.authenticate("exit", bearer, winner)
        upgraded.update(identifier, {"enabled": True})
        with self.assertRaises(ExitMachineMismatch):
            upgraded.authenticate("exit", bearer, loser)
        self.assertEqual(upgraded.snapshot(reservation)["totals"]["sent_bytes"], 100)
        self.assertNotIn("exit_client_hash", json.dumps(upgraded.rows()))
        self.assertNotIn("exit_client_hash", upgraded.snapshot(reservation))
        other_id, other_token = upgraded.issue("exit", "another computer")
        self.assertNotEqual(other_id, identifier)
        upgraded.authenticate("exit", "Bearer " + other_token, loser)

    def test_internal_exit_callback_is_bound_to_the_borrowing_machine(self):
        _, token = self.vault.issue("exit", internal=True)
        with self.assertRaises(ExitMachineMismatch):
            self.vault.authenticate("exit", "Bearer " + token, "a" * 64)
        self.vault.authenticate("exit", "Bearer " + token, self.vault.client_id)

    def test_legacy_binding_is_removed_without_changing_grants_or_usage(self):
        identifier, token = self.grant(budget="10", expires_at=time.time() + 86401)
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
        identifier, token = self.grant(budget="1")
        reservation = self.vault.authenticate("quota", token, "a" * 64, spend=True)
        values = dict(model="gpt-6.1-sol", input_tokens=5000, output_tokens=100000, total_tokens=105000)
        self.vault.record(reservation, values)
        with self.assertRaises(SharingError):
            self.vault.authenticate("quota", token, "a" * 64, spend=True)
        self.vault.update(identifier, {}, reset=True)
        self.vault.record(reservation, values)  # Earlier period: lifetime only.
        row = self.vault.rows()[0]
        self.assertEqual(row["period_cost_nanos"], 0)
        self.assertEqual(row["totals"]["cost_nanos"], 2_020_000_000)
        self.assertEqual(row["week_cost_nanos"], 2_020_000_000)
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
        identifier, token = self.grant(expires_at=time.time() + 86460)
        second = CredentialVault(self.root)
        self.assertEqual(second.client_id, self.vault.client_id)
        self.assertEqual(second.get_secret(identifier), token[7:])
        if os.name == "nt":
            self.assertNotIn(token[7:].encode(), self.vault.path.read_bytes())
        with patch("packet_inspector.sharing_credentials.time.time", return_value=time.time() + 2 * 86400):
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

    def test_new_grants_require_a_day_and_one_dollar_without_changing_legacy_values(self):
        now = 1_800_000_000
        with patch("packet_inspector.sharing_credentials.time.time", return_value=now):
            for kind in ("quota", "exit"):
                for delta in (1, 3600, 86399):
                    with self.subTest(kind=kind, delta=delta), self.assertRaises(SharingError):
                        self.vault.issue(kind, expires_at=now + delta)
                self.vault.issue(kind, expires_at=now + 86400)
            identifier, _ = self.grant(budget=10)
            with self.vault.connection() as db:
                legacy = json.loads(db.execute("SELECT data FROM credentials WHERE id=?", (identifier,)).fetchone()[0])
                legacy.update(budget_nanos=10_000_000, expires_at=now + 60)
                self.vault._save(db, identifier, legacy)
            upgraded = CredentialVault(self.root)
            row = upgraded.update(identifier, {"label": "renamed", "budget": "0.01", "expires_at": now + 60})
            self.assertEqual(row["budget_nanos"], 10_000_000)
            self.assertEqual(row["expires_at"], now + 60)
            with self.assertRaises(ValueError):
                upgraded.update(identifier, {"budget": "0.50"})
            with self.assertRaises(SharingError):
                upgraded.update(identifier, {"expires_at": now + 3600})
            upgraded.update(identifier, {"budget": "1", "expires_at": now + 86400})

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
