"""Provider migration changes configuration metadata, never messages or usage."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import tomllib
import unittest
from unittest.mock import patch

from packet_inspector.config_link import ConfigLink, PROVIDER
from packet_inspector.provider_migration import compatible_aliases
from packet_inspector.provider_migration_worker import migrate
from packet_inspector.providers import ProviderManager


UPSTREAM = "https://chatgpt.com/backend-api/codex"


class ProviderMigrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = self.root / "config.toml"
        self.config.write_text('model_provider="legacy"\nmodel="test"\n'
            '[model_providers.legacy]\nname="Old connection"\nwire_api="responses"\n'
            f'requires_openai_auth=true\nbase_url="{UPSTREAM}"\n', encoding="utf8")
        self.link = ConfigLink(self.config, self.root / "cache" / "link.json")
        self.manager = ProviderManager(self.link, "http://127.0.0.1:47844/relay/test", UPSTREAM)
        self.routes = {}

    def sync(self):
        return self.manager.sync(self.routes, lambda routes: setattr(self, "routes", routes))

    def index(self, name="state_5.sqlite", rows=None):
        path = self.root / name
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("CREATE TABLE threads(id TEXT PRIMARY KEY,model_provider TEXT,title TEXT,"
                       "tokens_used INTEGER,archived INTEGER,updated_at INTEGER,extra_column TEXT)")
            db.executemany("INSERT INTO threads VALUES (?,?,?,?,?,?,?)", rows or [
                ("old", "legacy", "Keep title", 164466, 0, 100, "future metadata"),
                ("archived", "openai", "Archived", 17, 1, 50, "unchanged"),
                ("current", PROVIDER, "Current", 200, 0, 110, "same"),
                ("third", "third_party", "Other", 300, 0, 120, "same"),
            ])
        return path

    def rows(self, path):
        with closing(sqlite3.connect(path)) as db:
            return db.execute("SELECT * FROM threads ORDER BY id").fetchall()

    def test_connect_migrates_equivalent_ids_and_archives_without_touching_other_metadata_or_history(self):
        index = self.index()
        before = self.rows(index)
        history = self.root / "sessions" / "old.jsonl"
        history.parent.mkdir()
        content = b'{"type":"session_meta","payload":{"model_provider":"legacy"}}\n{"message":"keep"}\n'
        history.write_bytes(content)
        stamp = history.stat().st_mtime_ns
        result = self.sync()
        self.assertEqual(result["session_migration"]["changed"], 2)
        after = self.rows(index)
        self.assertEqual([row[:1] + row[2:] for row in before], [row[:1] + row[2:] for row in after])
        self.assertEqual(dict((row[0], row[1]) for row in after),
                         {"archived": PROVIDER, "current": PROVIDER, "old": PROVIDER, "third": "third_party"})
        self.assertEqual(history.read_bytes(), content)
        self.assertEqual(history.stat().st_mtime_ns, stamp)
        backup = json.loads(Path(result["session_migration"]["backups"][0]).read_text(encoding="utf8"))
        self.assertEqual(dict(backup["previous"]), {"archived": "openai", "old": "legacy"})
        self.assertEqual(set(backup), {"database", "target", "previous"})

    def test_upgrade_check_migrates_metadata_even_when_toml_is_already_connected(self):
        self.sync()
        before = self.config.read_bytes()
        self.index()
        result = self.sync()
        self.assertEqual(self.config.read_bytes(), before)
        self.assertTrue(result["changed"])
        self.assertTrue(result["reload_required"])
        self.assertEqual(result["session_migration"]["changed"], 2)
        backups = list((self.root / "cache" / "provider-migration-backups").glob("*.json"))
        again = self.sync()
        self.assertFalse(again["reload_required"])
        self.assertEqual(again["session_migration"]["changed"], 0)
        self.assertEqual(list((self.root / "cache" / "provider-migration-backups").glob("*.json")), backups)

    def test_provider_identity_checks_endpoint_auth_and_unknown_fields(self):
        canonical = {"requires_openai_auth": True, "wire_api": "responses", "base_url": UPSTREAM}
        data = {"model_providers": {PROVIDER: canonical,
            "same": {**canonical, "name": "Different label", "stream_max_retries": 99, "supports_websockets": True},
            "other_url": {**canonical, "base_url": "https://other.invalid/v1"},
            "key": {**canonical, "env_key": "PRIVATE_KEY_NAME"},
            "oauth": {**canonical, "gateway_oauth": {"issuer": "https://issuer.invalid"}},
            "headers": {**canonical, "http_headers": {"X-Route": "different"}},
            "future_auth": {**canonical, "future_auth_mode": "different"},
            "shared": {**canonical, "requires_openai_auth": False},
        }}
        self.assertEqual(compatible_aliases(data, lambda value, _: value, UPSTREAM), {"same", "openai"})

    def test_native_openai_is_excluded_when_it_has_another_route_or_api_key_auth(self):
        data = {"openai_base_url": "https://other.invalid/v1", "model_providers": {
            PROVIDER: {"requires_openai_auth": True, "base_url": UPSTREAM}}}
        self.assertNotIn("openai", compatible_aliases(data, lambda value, _: value, UPSTREAM))
        data.pop("openai_base_url")
        self.assertNotIn("openai", compatible_aliases(data, lambda value, _: value, "https://api.openai.com/v1"))

    def test_conflicting_profile_connection_does_not_authorize_global_migration(self):
        self.sync()
        index = self.index()
        profile = self.root / "work.config.toml"
        profile.write_text(f'model_provider="{PROVIDER}"\n[model_providers.legacy]\n'
                           'base_url="https://different.invalid/codex"\n', encoding="utf8")
        result = self.sync()
        providers = dict((row[0], row[1]) for row in self.rows(index))
        self.assertEqual(providers["old"], "legacy")
        self.assertTrue(any(row["reason"] == "session_provider_ambiguous" for row in result["warnings"]))

    def test_first_connection_with_conflicting_alias_does_not_switch_default_or_write_native_index(self):
        index = self.index()
        before = self.config.read_bytes(), self.rows(index)
        (self.root / "work.config.toml").write_text(
            f'model_provider="{PROVIDER}"\n[model_providers.legacy]\n'
            'base_url="https://different.invalid/codex"\n', encoding="utf8")
        with self.assertRaisesRegex(ValueError, "连接冲突"):
            self.sync()
        self.assertEqual((self.config.read_bytes(), self.rows(index)), before)
        self.assertFalse(self.link.state_path.exists())

    def test_worker_failure_preserves_config_change_and_restart_feedback(self):
        def fail(_):
            raise ValueError("native worker unavailable")
        result = self.manager.sync(self.routes, lambda _: None, session_migrator=fail)
        self.assertTrue(result["reload_required"])
        self.assertEqual(tomllib.loads(self.config.read_text())["model_provider"], PROVIDER)
        self.assertEqual(result["session_migration"]["changed"], 0)
        self.assertEqual(result["warnings"][0]["reason"], "session_provider_migration_failed")

    def test_equivalent_embedded_profile_selection_follows_the_migrated_identity(self):
        self.config.write_text(self.config.read_text() + '[profiles.work]\nmodel_provider="legacy"\n'
                               '[profiles.api]\nmodel_provider="third_party"\n')
        self.index()
        self.sync()
        data = tomllib.loads(self.config.read_text())
        self.assertEqual(data["profiles"]["work"]["model_provider"], PROVIDER)
        self.assertEqual(data["profiles"]["api"]["model_provider"], "third_party")
        self.link.restore()
        self.assertEqual(tomllib.loads(self.config.read_text())["profiles"]["work"]["model_provider"], PROVIDER)

    def test_independent_profile_does_not_veto_main_routes_that_it_does_not_redefine(self):
        self.sync()
        self.index()
        profile = self.root / "vision.config.toml"
        profile.write_text(f'model_provider="{PROVIDER}"\n[model_providers.{PROVIDER}]\n'
            f'name="OpenAI"\nrequires_openai_auth=true\nbase_url="{UPSTREAM}"\n'
            'http_headers={"X-Private-Tool"="1"}\n', encoding="utf8")
        result = self.sync()
        self.assertEqual(result["session_migration"]["changed"], 2)
        self.assertFalse(result["warnings"])

    def test_embedded_profile_with_another_native_route_is_not_migrated(self):
        self.sync()
        index = self.index()
        self.config.write_text(self.config.read_text() + '[profiles.api]\nmodel_provider="openai"\n'
                               'openai_base_url="https://different.invalid/v1"\n')
        result = self.sync()
        self.assertEqual(dict((row[0], row[1]) for row in self.rows(index))["archived"], "openai")
        self.assertEqual(tomllib.loads(self.config.read_text())["profiles"]["api"]["model_provider"], "openai")
        self.assertTrue(any(row["reason"] == "session_provider_ambiguous" for row in result["warnings"]))

    def test_embedded_profile_selection_requires_its_own_canonical_connection_to_match(self):
        self.sync()
        index = self.index()
        self.config.write_text(self.config.read_text() + '[profiles.private]\nmodel_provider="legacy"\n'
            f'[profiles.private.model_providers.{PROVIDER}]\nhttp_headers={{"X-Private"="1"}}\n')
        result = self.sync()
        self.assertEqual(dict((row[0], row[1]) for row in self.rows(index))["old"], "legacy")
        self.assertEqual(tomllib.loads(self.config.read_text())["profiles"]["private"]["model_provider"], "legacy")
        self.assertTrue(any(row["reason"] == "session_provider_ambiguous" for row in result["warnings"]))

    def test_metadata_backup_failure_leaves_native_database_unchanged(self):
        index = self.index()
        before = self.rows(index)
        with patch("packet_inspector.provider_migration_worker.os.replace", side_effect=OSError("backup failed")):
            result = migrate(self.root, self.root / "backups", ["legacy"], PROVIDER)
        self.assertEqual(result["changed"], 0)
        self.assertEqual(self.rows(index), before)
        self.assertEqual(result["warnings"][0]["reason"], "session_provider_migration_failed")
        self.assertFalse(list((self.root / "backups").glob(".provider-migration-*")))

    def test_native_database_error_rolls_back_its_transaction(self):
        index = self.index()
        before = self.rows(index)
        with closing(sqlite3.connect(index)) as db, db:
            db.execute("CREATE TRIGGER reject_provider BEFORE UPDATE OF model_provider ON threads "
                       "WHEN old.id='old' BEGIN SELECT RAISE(ABORT,'simulated write failure'); END")
        result = migrate(self.root, self.root / "backups", ["legacy", "openai"], PROVIDER)
        self.assertEqual(result["changed"], 0)
        self.assertEqual(self.rows(index), before)
        self.assertTrue(result["warnings"])
        self.assertEqual(len(list((self.root / "backups").glob("*.json"))), 1)

    def test_corrupt_recovery_copy_prevents_a_repeated_migration(self):
        index = self.index()
        before = self.rows(index)
        result = migrate(self.root, self.root / "backups", ["legacy"], PROVIDER)
        Path(result["backups"][0]).write_bytes(b"corrupt")
        with closing(sqlite3.connect(index)) as db, db:
            db.execute("UPDATE threads SET model_provider='legacy' WHERE id='old'")
        result = migrate(self.root, self.root / "backups", ["legacy"], PROVIDER)
        self.assertEqual(result["changed"], 0)
        self.assertEqual(self.rows(index), before)
        self.assertIn("does not match", result["warnings"][0]["detail"])

    def test_versioned_indexes_are_discovered_and_missing_indexes_are_not_created(self):
        indexes = [self.index("state_3.sqlite"), self.index("state_99.sqlite")]
        result = migrate(self.root, self.root / "backups", ["legacy"], PROVIDER)
        self.assertEqual(result["databases"], 2)
        self.assertEqual(result["changed"], 2)
        for index in indexes:
            self.assertEqual(dict((r[0], r[1]) for r in self.rows(index))["old"], PROVIDER)
        empty = self.root / "empty"
        empty.mkdir()
        migrate(empty, empty / "backups", ["legacy"], PROVIDER)
        self.assertFalse(list(empty.iterdir()))

    def test_unsupported_or_unavailable_schema_is_reported_without_recreating_it(self):
        index = self.root / "state_200.sqlite"
        with closing(sqlite3.connect(index)) as db, db:
            db.execute("CREATE TABLE threads(id TEXT, future_provider_id TEXT)")
        before = index.read_bytes()
        result = migrate(self.root, self.root / "backups", ["legacy"], PROVIDER)
        self.assertTrue(result["warnings"])
        self.assertEqual(index.read_bytes(), before)

    def test_recovery_snapshots_have_a_bounded_count(self):
        self.index()
        backups = self.root / "backups"
        backups.mkdir()
        for n in range(40):
            (backups / f"provider-migration-old-{n}.json").write_text("{}")
        result = migrate(self.root, backups, ["legacy"], PROVIDER)
        self.assertEqual(result["changed"], 1)
        self.assertEqual(len(list(backups.glob("*.json"))), 32)
        self.assertTrue(Path(result["backups"][0]).is_file())


if __name__ == "__main__":
    unittest.main()
