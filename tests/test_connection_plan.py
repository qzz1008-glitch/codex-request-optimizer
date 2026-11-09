"""Connection planning, stale-input rejection and reversible batch commits."""
from pathlib import Path
import tempfile
import tomllib
import unittest
from unittest.mock import patch

from packet_inspector.config_link import ConfigLink
from packet_inspector.connection_plan import ConfigEdit, ConfigSnapshot, ConfigurationBatch
from packet_inspector.configuration_layers import merge_layers
from packet_inspector.providers import ProviderManager


class ConnectionPlanTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.config = self.root / "config.toml"
        self.config.write_text('model="keep-model"\n', encoding="utf8")
        self.link = ConfigLink(self.config, self.root / "link.json")
        self.manager = ProviderManager(self.link, "http://127.0.0.1:47844/relay/test", "https://unused.invalid")
        self.routes = {}

    def sync(self):
        return self.manager.sync(self.routes, lambda routes: setattr(self, "routes", routes))

    def test_planning_has_no_configuration_recovery_route_or_native_index_side_effects(self):
        original = self.config.read_bytes()
        routes = {"existing": {"provider": "external", "upstream": "https://existing.invalid"}}
        plan = self.manager.plan(routes, retry_limit=15)
        self.assertEqual(self.config.read_bytes(), original)
        self.assertFalse(self.link.state_path.exists())
        self.assertEqual(list(self.root.iterdir()), [self.config])
        self.assertEqual(list(routes), ["existing"])
        self.assertTrue(plan.batch.edits)
        self.assertTrue(plan.routes)
        plan.check()

    def test_changed_unchanged_source_is_checked_before_any_write(self):
        untouched = self.root / "untouched.toml"
        untouched.write_bytes(b"original")
        source = ConfigSnapshot.read(self.config)
        batch = ConfigurationBatch((source, ConfigSnapshot.read(untouched)),
                                   (ConfigEdit(source, 'model="new"\n'),))
        untouched.write_bytes(b"user edit")
        with patch.object(ConfigLink, "_write") as write, self.assertRaisesRegex(ValueError, "发生变化"):
            batch.apply(self.link)
        write.assert_not_called()
        self.assertEqual(self.config.read_bytes(), source.content)

    def test_a_new_empty_file_is_a_concurrent_edit_not_an_absent_source(self):
        path = self.root / "new.config.toml"
        source = ConfigSnapshot.read(path)
        path.write_bytes(b"")
        with self.assertRaisesRegex(ValueError, "发生变化"):
            ConfigurationBatch((source,), (ConfigEdit(source, 'model="new"\n'),)).apply(self.link)
        self.assertEqual(path.read_bytes(), b"")

    def test_changed_configuration_during_route_publication_never_gets_overwritten(self):
        def publish(routes):
            self.config.write_text('model="concurrent"\n', encoding="utf8")
        with patch.object(ConfigLink, "_write") as write, self.assertRaisesRegex(ValueError, "发生变化"):
            self.manager.sync({}, publish)
        write.assert_not_called()
        self.assertFalse(self.link.state_path.exists())
        self.assertEqual(tomllib.loads(self.config.read_text())["model"], "concurrent")

    def test_profile_added_after_discovery_invalidates_the_plan(self):
        plan = self.manager.plan({})
        (self.root / "new.config.toml").write_text('model="new-profile"\n', encoding="utf8")
        with self.assertRaisesRegex(ValueError, "发生变化"):
            plan.check()
        self.assertFalse(self.link.state_path.exists())

    def test_login_mode_changes_invalidate_the_implicit_destination(self):
        auth = self.root / "auth.json"
        auth.write_text('{"auth_mode":"chatgpt"}', encoding="utf8")
        plan = self.manager.plan({})
        auth.write_text('{"auth_mode":"apikey"}', encoding="utf8")
        with self.assertRaisesRegex(ValueError, "发生变化"):
            plan.check()

    def test_token_renewal_does_not_invalidate_the_connection_plan(self):
        auth = self.root / "auth.json"
        auth.write_text('{"auth_mode":"chatgpt","tokens":{"access_token":"old"}}', encoding="utf8")
        plan = self.manager.plan({})
        auth.write_text('{"auth_mode":"chatgpt","tokens":{"access_token":"renewed"}}', encoding="utf8")
        plan.check()

    def test_recovery_changes_invalidate_the_plan(self):
        plan = self.manager.plan({})
        self.link._write_state({"changed_by_another_writer": True})
        with self.assertRaisesRegex(ValueError, "发生变化"):
            plan.check()

    def test_unresolvable_selected_connections_fail_before_publishing_or_writing(self):
        cases = (
            'model_provider="missing"\n',
            'model_provider="other"\n[model_providers.other]\nname="Other"\ngateway_oauth={issuer="https://issuer.invalid"}\n',
            'model_provider="other"\n[model_providers.other]\nname="Other"\nwire_api="chat"\nbase_url="https://example.invalid"\n',
        )
        for text in cases:
            with self.subTest(text=text):
                self.config.write_text(text, encoding="utf8")
                publish = unittest.mock.Mock()
                with self.assertRaisesRegex(ValueError, "config.toml · model_provider"):
                    self.manager.sync({}, publish)
                publish.assert_not_called()
                self.assertEqual(self.config.read_text(), text)
                self.assertFalse(self.link.state_path.exists())

    def test_profiles_can_reference_the_canonical_provider_created_by_the_same_plan(self):
        from packet_inspector.config_link import PROVIDER
        self.config.write_text('model_provider="legacy"\n[model_providers.legacy]\nname="OpenAI"\n'
                               'requires_openai_auth=true\nbase_url="https://upstream.invalid/v1"\n', encoding="utf8")
        profile = self.root / "work.config.toml"
        profile.write_text(f'model_provider="{PROVIDER}"\n', encoding="utf8")
        self.assertFalse(self.sync()["warnings"])
        self.assertEqual(tomllib.loads(profile.read_text())["model_provider"], PROVIDER)
        self.assertFalse(self.sync()["changed"])

    def test_inaccessible_optional_auth_file_retains_the_existing_default_policy(self):
        read_text = Path.read_text
        def deny_auth(path, *args, **kwargs):
            if path.name == "auth.json":
                raise PermissionError("synthetic denied file")
            return read_text(path, *args, **kwargs)
        with patch.object(Path, "read_text", deny_auth):
            plan = self.manager.plan({})
            plan.check()
            self.assertEqual(self.link.profile()["upstream"], "https://chatgpt.com/backend-api/codex")

    def test_implicit_api_auth_keeps_its_identity_credentials_and_default_in_both_states(self):
        self.config.write_text('model_provider="external"\nopenai_base_url="https://builtin.invalid"\n'
                               '[model_providers.external]\nname="Keep"\nenv_key="TEST_KEY"\n', encoding="utf8")
        self.assertEqual(self.link.profile()["upstream"], "https://api.openai.com/v1")
        self.sync()
        self.assertEqual(self.link.profile()["upstream"], "https://api.openai.com/v1")
        current = tomllib.loads(self.config.read_text())
        self.assertEqual(current["model_provider"], "external")
        self.assertEqual(current["model_providers"]["external"]["env_key"], "TEST_KEY")
        self.assertFalse(self.sync()["changed"])
        self.link.restore()
        self.assertEqual(tomllib.loads(self.config.read_text())["model_providers"]["external"],
                         {"name": "Keep", "env_key": "TEST_KEY"})
        self.assertEqual(self.link.profile()["upstream"], "https://api.openai.com/v1")

    def test_rollback_preserves_bom_and_crlf_bytes(self):
        self.config.write_bytes(b'\xef\xbb\xbfmodel="before"\r\n')
        profile = self.root / "work.config.toml"
        profile.write_bytes(b'model="profile"\r\n')
        before = {path: path.read_bytes() for path in (self.config, profile)}
        real_write = ConfigLink._write
        def fail_profile(link, text):
            if link.config == profile:
                raise OSError("disk failure")
            return real_write(link, text)
        with patch.object(ConfigLink, "_write", fail_profile), self.assertRaises(OSError):
            self.sync()
        for path, content in before.items():
            self.assertEqual(path.read_bytes(), content)
        self.assertFalse(self.link._state())

    def test_final_readback_preserves_external_changes_and_keeps_recovery(self):
        profile = self.root / "work.config.toml"
        profile.write_text('model="profile"\n', encoding="utf8")
        original_profile = profile.read_bytes()
        real_write = ConfigLink._write
        def change_previous_file(link, text):
            result = real_write(link, text)
            if link.config == profile:
                self.config.write_text(self.config.read_text() + '\n[notice]\nuser_edit=true\n', encoding="utf8")
            return result
        with patch.object(ConfigLink, "_write", change_previous_file), self.assertRaisesRegex(ValueError, "发生变化"):
            self.sync()
        self.assertTrue(tomllib.loads(self.config.read_text())["notice"]["user_edit"])
        self.assertEqual(profile.read_bytes(), original_profile)
        self.assertTrue(self.link._state(), "Keep the original field records for a safe restore/retry")
        self.link.restore()
        restored = tomllib.loads(self.config.read_text())
        self.assertTrue(restored["notice"]["user_edit"])
        self.assertFalse(self.link.profile()["active"])

    def test_new_profile_during_commit_rolls_back_the_batch(self):
        before = self.config.read_bytes()
        real_write = ConfigLink._write
        def add_profile(link, text):
            result = real_write(link, text)
            (self.root / "new.config.toml").write_text('model="concurrent-profile"\n', encoding="utf8")
            return result
        with patch.object(ConfigLink, "_write", add_profile), self.assertRaisesRegex(ValueError, "发生变化"):
            self.sync()
        self.assertEqual(self.config.read_bytes(), before)
        self.assertEqual(tomllib.loads((self.root / "new.config.toml").read_text())["model"], "concurrent-profile")

    def test_unchanged_connected_file_is_verified_too(self):
        self.sync()
        before = self.config.read_bytes()
        plan = self.manager.plan(self.routes)
        self.assertFalse(plan.batch.edits)
        self.config.write_bytes(before + b"\n# edited concurrently\n")
        with self.assertRaisesRegex(ValueError, "发生变化"):
            plan.batch.apply(self.link)

    def test_status_and_planner_agree_on_explicit_builtin_and_custom_defaults(self):
        self.config.write_text('model_provider="openai"\nopenai_base_url="https://builtin.invalid/v1"\n'
                               '[model_providers.custom]\nname="OpenAI"\nrequires_openai_auth=true\n', encoding="utf8")
        self.assertEqual(self.link.profile()["upstream"], "https://builtin.invalid/v1")
        self.sync()
        by_provider = {route["provider"]: route["upstream"] for route in self.routes.values()}
        self.assertEqual(by_provider["openai"], "https://builtin.invalid/v1")
        self.assertEqual(by_provider["custom"], "https://chatgpt.com/backend-api/codex")
        self.link.restore()
        self.assertEqual(self.link.profile()["upstream"], "https://builtin.invalid/v1")

    def test_nested_provider_fields_inherit_without_a_field_whitelist(self):
        from packet_inspector.config_link import PROVIDER
        self.config.write_text('model_provider="legacy"\n[model_providers.legacy]\n'
            'name="OpenAI"\nrequires_openai_auth=true\nbase_url="https://upstream.invalid/v1"\n'
            'http_headers={X-Parent="keep", X-Override="old"}\n'
            'future_options={inherited={keep=1, replace=2}, sequence=[1,2]}\n', encoding="utf8")
        profile = self.root / "vision.config.toml"
        original = ('model_provider="legacy"\n[model_providers.legacy]\n'
            'http_headers={X-Override="new"}\n'
            'future_options={inherited={replace=3}, sequence=[3]}\n')
        profile.write_text(original, encoding="utf8")
        self.sync()
        data = tomllib.loads(profile.read_text())
        canonical = data["model_providers"][PROVIDER]
        self.assertEqual(canonical["http_headers"], {"X-Parent": "keep", "X-Override": "new"})
        self.assertEqual(canonical["future_options"], {"inherited": {"keep": 1, "replace": 3}, "sequence": [3]})
        self.assertFalse(self.sync()["changed"])
        self.link.restore()
        restored = tomllib.loads(profile.read_text())
        for key, value in tomllib.loads(original)["model_providers"]["legacy"].items():
            self.assertEqual(restored["model_providers"]["legacy"][key], value)

    def test_partial_canonical_declarations_are_compared_with_their_inherited_settings(self):
        from packet_inspector.config_link import PROVIDER
        self.config.write_text('model_provider="legacy"\n[model_providers.legacy]\n'
            'name="OpenAI"\nrequires_openai_auth=true\nbase_url="https://upstream.invalid/v1"\n'
            'http_headers={X-Parent="keep", X-Other="keep"}\n', encoding="utf8")
        self.sync()
        profile = self.root / "work.config.toml"
        profile.write_text('model_provider="legacy"\n'
                           f'[model_providers.{PROVIDER}]\nhttp_headers={{X-Parent="keep"}}\n', encoding="utf8")
        result = self.sync()
        self.assertFalse(result["warnings"])
        self.assertEqual(tomllib.loads(profile.read_text())["model_provider"], PROVIDER)
        self.assertFalse(self.sync()["changed"])

    def test_layer_merge_preserves_unknown_fields_and_does_not_alias_input_objects(self):
        parent = {"provider": {"headers": {"keep": "yes", "override": "before"}, "unknown": [1, 2]}}
        child = {"provider": {"headers": {"override": "after"}, "unknown": [3]}}
        result = merge_layers(parent, child)
        self.assertEqual(result, {"provider": {"headers": {"keep": "yes", "override": "after"}, "unknown": [3]}})
        result["provider"]["headers"]["keep"] = "changed"
        result["provider"]["unknown"].append(4)
        self.assertEqual(parent["provider"]["headers"]["keep"], "yes")
        self.assertEqual(child["provider"]["unknown"], [3])

    def test_malformed_optional_auth_file_uses_the_same_default_everywhere(self):
        for content in (b"not json", b"[]", b"null", b"\xff"):
            with self.subTest(content=content):
                (self.root / "auth.json").write_bytes(content)
                plan = self.manager.plan({})
                plan.check()
                self.assertEqual(self.link.profile()["upstream"], "https://chatgpt.com/backend-api/codex")
                self.assertEqual({value["upstream"] for value in plan.routes.values()},
                                 {"https://chatgpt.com/backend-api/codex"})

    def test_failed_disconnect_preserves_user_edit_and_is_retryable(self):
        profile = self.root / "work.config.toml"
        profile.write_text('model="profile"\n', encoding="utf8")
        self.sync()
        real_write = ConfigLink._write
        def edit_after_restore(link, text):
            result = real_write(link, text)
            if link.config == profile:
                self.config.write_text(self.config.read_text() + '\n[notice]\nuser_edit=true\n', encoding="utf8")
            return result
        with patch.object(ConfigLink, "_write", edit_after_restore), self.assertRaisesRegex(ValueError, "发生变化"):
            self.link.restore()
        self.assertTrue(self.link._state())
        self.link.restore()
        self.assertTrue(tomllib.loads(self.config.read_text())["notice"]["user_edit"])
        self.assertFalse(self.link._state())


if __name__ == "__main__":
    unittest.main()
