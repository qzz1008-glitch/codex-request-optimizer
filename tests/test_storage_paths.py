"""Storage grouping must not silently relocate a live or historical database."""
import json
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest

from packet_inspector.server import InspectorServer
from packet_inspector.startup_diagnostics import log_directory
from packet_inspector.storage_paths import grouped_paths, path_settings


class StoragePathTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.data = self.root / "data"
        self.data.mkdir()

    def test_one_root_creates_only_grouped_directories(self):
        target = self.root / "new data"
        result = path_settings(self.data, {"storage_dir": str(target)})
        self.assertEqual(result["storage_dir"], str(target))
        for key, child in (("log_dir", "logs"), ("cache_dir", "cache"), ("image_save_dir", "images")):
            self.assertEqual(Path(result[key]), target / child)
            self.assertTrue((target / child).is_dir())
        self.assertEqual(grouped_paths(self.data)["storage_dir"], "")

    def test_invalid_and_conflicting_roots_are_rejected(self):
        for value in ("relative", 42, None, "bad\x00path"):
            with self.subTest(value=repr(value)), self.assertRaises(ValueError):
                path_settings(self.data, {"storage_dir": value})
        for key in ("log_dir", "cache_dir", "image_save_dir"):
            with self.subTest(key=key), self.assertRaises(ValueError):
                path_settings(self.data, {"storage_dir": "", key: ""})

    def test_legacy_directory_api_stays_supported(self):
        result = path_settings(self.data, {"log_dir": str(self.root / "old logs")})
        self.assertEqual(result, {"log_dir": str(self.root / "old logs"), "storage_dir": None})

    def test_startup_logs_follow_new_and_legacy_layouts(self):
        self.assertEqual(log_directory(self.data), self.data / "logs")
        settings = self.data / "settings.json"
        settings.write_text(json.dumps({"log_dir": ""}), encoding="utf8")
        self.assertEqual(log_directory(self.data), self.data)
        settings.write_text(json.dumps({"log_dir": "old logs"}), encoding="utf8")
        self.assertEqual(log_directory(self.data), self.data / "old logs")
        settings.write_text(json.dumps({"storage_dir": str(self.root / "custom"), "log_dir": "old logs"}), encoding="utf8")
        self.assertEqual(log_directory(self.data), self.root / "custom" / "logs")

    def test_saved_root_is_authoritative_for_all_children(self):
        target = self.root / "custom"
        (self.data / "settings.json").write_text(json.dumps({"storage_dir": str(target),
                                                            "cache_dir": "", "log_dir": ""}), encoding="utf8")
        config = self.root / "config.toml"
        config.write_text('model_provider="test"\n[model_providers.test]\nname="Test"\n'
                          'wire_api="responses"\nbase_url="http://127.0.0.1:1"\n', encoding="utf8")
        with InspectorServer(("127.0.0.1", 0), self.data, config, "http://127.0.0.1:1") as server:
            self.assertEqual(server.cache_dir, target / "cache")
            self.assertEqual(server.log_dir, target / "logs")
            self.assertEqual(server.image_isolation.directory, target / "images")
            self.assertFalse(server.status()["storage_paths_pending"])

    def test_upgrade_reuses_legacy_database_and_image_directory(self):
        images = self.root / "old images"
        images.mkdir()
        marker = images / "retained.txt"
        marker.write_text("historical image location", encoding="utf8")
        (self.data / "settings.json").write_text(json.dumps({"image_save_dir": str(images)}), encoding="utf8")
        config = self.root / "config.toml"
        config.write_text('model_provider="test"\n[model_providers.test]\nname="Test"\n'
                          'wire_api="responses"\nbase_url="http://127.0.0.1:1"\n', encoding="utf8")
        with InspectorServer(("127.0.0.1", 0), self.data, config, "http://127.0.0.1:1") as old:
            with closing(sqlite3.connect(old.store.path)) as db, db:
                db.execute("CREATE TABLE legacy_marker (value TEXT)")
                db.execute("INSERT INTO legacy_marker VALUES ('retained')")
        with InspectorServer(("127.0.0.1", 0), self.data, config, "http://127.0.0.1:1") as upgraded:
            self.assertEqual(upgraded.store.path.parent, self.data)
            self.assertEqual(upgraded.log_dir, self.data)
            self.assertEqual(upgraded.image_isolation.directory, images)
            self.assertEqual(upgraded.status()["storage_layout"], "legacy")
            self.assertFalse(upgraded.status()["storage_paths_pending"])
            with closing(sqlite3.connect(upgraded.store.path)) as db:
                self.assertEqual(db.execute("SELECT value FROM legacy_marker").fetchone()[0], "retained")
        self.assertTrue(marker.is_file())
        self.assertFalse((self.data / "cache" / "packets.sqlite").exists())


if __name__ == "__main__":
    unittest.main()
