import base64
import gzip
import json
from pathlib import Path
import random
import tempfile
import unittest
from unittest.mock import patch

import zstandard

from packet_inspector.analyzer import encode
from packet_inspector.overflow import encode_changed_info
from packet_inspector.server import InspectorServer
from packet_inspector.transport import prepare_request_info
from packet_inspector.compression_policy import CompressionPolicy, select_phase
from packet_inspector.storage import Store
from tests import test_packet_inspector as fixtures


class PhaseSelectionTests(unittest.TestCase):
    def test_unknown_session_uses_fixed_initial_threshold_after_small_body_check(self):
        for raw, expected in ((899999, 0), (900000, 1), (1499999, 1), (1500000, 17), (2000000, 17)):
            with self.subTest(raw=raw):
                self.assertEqual(select_phase(None, raw, 1000000), expected)
        # The first-selection split is fixed in bytes, not scaled with the cap.
        self.assertEqual(select_phase(None, 1000000, 500000), 1)
        self.assertEqual(select_phase(None, 1500000, 2000000), 0)

    def test_initial_threshold_does_not_override_a_known_phase(self):
        for previous in (0, 1):
            self.assertEqual(select_phase(previous, 2000000, 1000000), 1)
        self.assertEqual(select_phase(17, 2000000, 1000000), 17)
        self.assertEqual(select_phase(17, 900000, 1000000), 17)
        self.assertEqual(select_phase(17, 899999, 1000000), 0)


class CompressionPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name))
        self.policy = CompressionPolicy(self.store)
        self.key = ('upstream', 'session')
        self.policy.begin(self.key, self.policy.reserve(), 500, 1000)

    def begin(self, raw=2000, budget=1000, key=None):
        return self.policy.begin(key or self.key, self.policy.reserve(), raw, budget)

    def record(self, key, **fields):
        meta = {'method': 'POST', 'path': '/responses', 'thread_id': key[1],
                'relay_compression_scope': key[0], 'decoded_bytes': 2000,
                'outbound_wire_bytes': 850, **fields}
        return self.store.add(meta, {'input': 'retained request details'})

    def test_high_phase_does_not_drop_when_high_compression_reduces_wire_size(self):
        fast = self.begin()
        self.assertEqual(fast.phase, 1)
        self.assertEqual(self.policy.finish(self.key, fast, 900, 1000), 17)
        for wire in (850, 800, 700, 100):
            high = self.begin()
            self.assertEqual(high.phase, 17)
            self.assertEqual(self.policy.finish(self.key, high, wire, 1000), 17)
        # A larger budget alone does not release a still-large raw context.
        self.assertEqual(self.begin(raw=10000, budget=2000).phase, 17)
        self.assertEqual(self.begin(raw=899).phase, 0)
        self.assertEqual(self.begin(raw=900).phase, 1)
        for key in (('upstream', 'child'), ('other-upstream', 'session')):
            self.assertEqual(self.begin(key=key).phase, 1)
            self.assertEqual(self.begin(raw=500, key=key).phase, 0)
            self.assertEqual(self.begin(key=key).phase, 1)

    def test_restart_and_packet_cleanup_keep_the_recorded_phase_not_actual_grade(self):
        high = self.begin()
        self.policy.finish(self.key, high, 950, 1000)
        packet = self.store.add({'thread_id': 'session', 'relay_zstd_level': 1}, None)
        with self.store.connect() as c:
            c.execute('DELETE FROM packets WHERE id=?', (packet,))
        self.policy = CompressionPolicy(Store(Path(self.temp.name)))
        with patch('packet_inspector.compression_policy.time.time', return_value=10**12):
            self.assertEqual(self.begin().phase, 17)

    def test_existing_high_phase_uses_sixteen_after_upgrade_without_releasing_the_lock(self):
        # Emulate the durable state written by the previous level-17 worker.
        with self.store.connect() as c:
            c.execute('UPDATE auto_zstd_sessions SET phase=17 WHERE scope=? AND thread=?', self.key)
        self.policy = CompressionPolicy(Store(Path(self.temp.name)))
        self.assertEqual(self.policy.level(self.key, 2000, 1000), 16)
        decision = self.begin()
        self.assertEqual((decision.previous_phase, decision.phase, decision.level), (17, 17, 16))
        self.assertEqual(self.policy.finish(self.key, decision, 100, 1000), 17)
        self.assertEqual(self.begin().level, 16)
        self.assertEqual(self.begin(raw=899).phase, 0)
        self.assertEqual(self.begin().level, 1)

    def test_late_old_requests_cannot_relock_or_release_a_newer_phase(self):
        old = self.begin()
        self.policy.finish(self.key, old, 950, 1000)
        old_high = self.begin()
        delayed = self.policy.reserve()  # Inspected after the new compacted request.
        other_worker = CompressionPolicy(Store(Path(self.temp.name)))
        newer = other_worker.begin(self.key, other_worker.reserve(), 500, 1000)
        self.assertEqual(newer.phase, 0)
        self.assertIsNone(self.policy.finish(self.key, old_high, 950, 1000))
        stale = self.policy.begin(self.key, delayed, 2000, 1000)
        self.assertIsNone(self.policy.finish(self.key, stale, 950, 1000))
        self.assertEqual(self.begin().phase, 1)
        newer_high = self.begin()
        self.policy.finish(self.key, newer_high, 950, 1000)
        self.assertIsNone(self.policy.finish(self.key, newer, 100, 1000))
        self.assertEqual(self.begin().phase, 17)

    def test_clear_invalidates_reserved_and_started_requests(self):
        old = self.begin()
        delayed = self.policy.reserve()
        self.policy.clear()
        self.assertIsNone(self.policy.finish(self.key, old, 950, 1000))
        stale = self.policy.begin(self.key, delayed, 2000, 1000)
        self.assertIsNone(self.policy.finish(self.key, stale, 950, 1000))
        self.assertEqual(self.begin().phase, 1)

    def test_all_phases_survive_elapsed_time_and_restart(self):
        with patch('packet_inspector.compression_policy.time.time', return_value=0):
            for name, raw in (('original', 500), ('fast', 2000), ('high', 1500000)):
                self.begin(raw=raw, key=('upstream', name))
        self.policy = CompressionPolicy(Store(Path(self.temp.name)))
        with patch('packet_inspector.compression_policy.time.time', return_value=10**12):
            self.begin(key=('upstream', 'new'))
            # Initial selection would choose high mode for this large original body.
            # Known original/fast sessions must still select 1 after any gap.
            for name, expected in (('original', 1), ('fast', 1), ('high', 17)):
                with self.subTest(name=name):
                    decision = self.begin(raw=1500000, key=('upstream', name))
                    self.assertEqual(decision.phase, expected)
                    self.assertIsNotNone(decision.previous_phase)

    def test_full_and_summary_only_records_restore_the_same_next_phase(self):
        for details in (True, False):
            with self.subTest(details=details):
                key = ('upstream', 'retained-' + str(details))
                self.store.configure_limits(512 * 1024**2, 2000, 1)
                packet = self.record(key, relay_auto_zstd_next_phase=17,
                                     overflow_compaction_calls=1, overflow_original_wire_bytes=950,
                                     outbound_wire_bytes=100, relay_zstd_level=None,
                                     relay_content_encoding='identity')
                if not details:
                    # A newer unrelated request removes the old body, keeping
                    # its summary and all decision metadata under the real quota.
                    self.record(('upstream', 'unrelated'))
                meta, body = self.store.get(packet, content=True)
                self.assertEqual(body is not None, details)
                self.assertEqual(meta.get('details_evicted', False), not details)
                self.policy = CompressionPolicy(Store(Path(self.temp.name)))
                # Auxiliary selection reads the same metadata without creating
                # a main-session phase; the next main request then persists it.
                self.assertEqual(self.policy.level(key, 2000, 1000), 16)
                decision = self.begin(key=key)
                self.assertEqual((decision.previous_phase, decision.phase), (17, 17))
                self.assertEqual(decision.level, 16)

    def test_legacy_summary_sizes_and_high_grade_recover_without_details_or_age_limit(self):
        self.store.configure_limits(512 * 1024**2, 2000, 0)
        cases = [
            ({'relay_zstd_level': 17, 'outbound_wire_bytes': 850}, 17),
            ({'relay_zstd_level': 16, 'outbound_wire_bytes': 850}, 17),
            ({'relay_zstd_level': 1, 'outbound_wire_bytes': 900}, 17),
            ({'transport_content_encoding': 'zstd', 'transport_zstd_level': 17,
              'overflow_compaction_calls': 1, 'overflow_original_wire_bytes': 850,
              'outbound_wire_bytes': 100, 'relay_zstd_level': None}, 17),
            ({'transport_content_encoding': 'zstd', 'transport_zstd_level': 16,
              'overflow_compaction_calls': 1, 'overflow_original_wire_bytes': 850,
              'outbound_wire_bytes': 100, 'relay_zstd_level': None}, 17),
            ({'overflow_compaction_calls': 1, 'overflow_original_wire_bytes': 950,
              'outbound_wire_bytes': 100, 'relay_zstd_level': None}, 17),
            ({'usage_override_request_bytes': 950, 'outbound_wire_bytes': 100}, 17),
            ({'outbound_wire_bytes': 850}, 1),
        ]
        for index, (fields, expected) in enumerate(cases):
            with self.subTest(fields=fields):
                key = ('upstream', 'legacy-' + str(index))
                with patch('packet_inspector.storage.time.time', return_value=1000):
                    packet = self.record(key, **fields)
                self.assertIsNone(self.store.get(packet, content=True)[1])
                with patch('packet_inspector.compression_policy.time.time', return_value=10**12):
                    decision = self.begin(raw=1500000, key=key)
                self.assertEqual(decision.phase, expected)
                self.assertEqual(decision.level, 16 if expected == 17 else 1)
                self.assertIsNotNone(decision.previous_phase)

    def test_recovery_uses_latest_main_record_in_the_same_upstream(self):
        key = ('upstream', 'filtered-history')
        self.record(key, relay_auto_zstd_next_phase=17)
        excluded = [
            {'relay_compression_scope': 'other-upstream'},
            {'source': 'imported'}, {'method': 'GET'},
            {'is_compaction': True}, {'path': '/responses/compact'},
            {'request_kind': 'prewarm'}, {'request_kind': 'tool_search'},
            {'request_purpose': 'image_analysis'},
            {'request_kind': 'memory'}, {'path': '/alpha/search'},
            {'request_purpose': 'title_generation'},
            {'request_purpose': 'next_message_suggestion'},
            {'request_purpose': 'session_index'},
        ]
        for fields in excluded:
            self.record(key, relay_auto_zstd_next_phase=0, **fields)
        # An unfinished newest capture has no usable size/phase yet.
        self.record(key, outbound_wire_bytes=None)
        self.assertEqual(self.begin(key=key).phase, 17)
        self.policy.clear()
        self.record(key, decoded_bytes=500, outbound_wire_bytes=500)
        decision = self.begin(raw=1500000, key=key)
        self.assertEqual((decision.previous_phase, decision.phase), (0, 1))

    def test_current_small_original_releases_a_recovered_high_phase(self):
        key = ('upstream', 'compacted-history')
        self.record(key, relay_auto_zstd_next_phase=17)
        decision = self.begin(raw=899, key=key)
        self.assertEqual((decision.previous_phase, decision.phase), (17, 0))
        self.assertEqual(self.begin(key=key).phase, 1)

    def test_recovery_excludes_records_from_a_later_request_ticket(self):
        key = ('upstream', 'ordered-history')
        self.record(key, relay_auto_zstd_next_phase=17)
        old_ticket = self.policy.reserve()
        new_ticket = self.policy.reserve()
        self.record(key, relay_auto_zstd_ticket=new_ticket, relay_auto_zstd_next_phase=0)
        decision = self.policy.begin(key, old_ticket, 2000, 1000)
        self.assertEqual((decision.previous_phase, decision.phase), (17, 17))

    def test_large_wire_size_promotes_only_the_next_request(self):
        selected = self.begin()
        self.assertEqual(selected.level, 1)
        self.assertEqual(self.policy.finish(self.key, selected, 1001, 1000), 17)
        self.assertEqual(selected.level, 1)
        self.assertEqual(self.begin().level, 16)


class AutomaticTransportTests(unittest.TestCase):
    def test_selected_mode_compresses_once_and_preserves_all_bytes(self):
        raw = encode({'input': 'retained text ' * 15000})
        for level, actual in ((1, 1), (16, 16), (17, 16)):
            with self.subTest(level=level), patch('zstandard.ZstdCompressor', wraps=zstandard.ZstdCompressor) as compressor:
                prepared = prepare_request_info(raw, {}, 'POST', '/responses', 'zstd', level,
                                                auto_zstd_level=True, budget_bytes=200000)
                compressor.assert_called_once_with(level=actual)
                self.assertEqual(prepared.zstd_level, actual)
                self.assertEqual(zstandard.ZstdDecompressor().decompress(prepared.body), raw)

    def test_current_original_size_takes_priority_at_the_strict_configured_boundary(self):
        empty_size = len(encode({'input': ''}))
        for budget in (1000, 1753, 200000):
            boundary = (budget * 90 + 99) // 100
            for size in (boundary - 1, boundary):
                raw = encode({'input': 'x' * (size - empty_size)})
                for level in (1, 16):
                    with self.subTest(budget=budget, size=size, level=level), patch(
                            'zstandard.ZstdCompressor', wraps=zstandard.ZstdCompressor) as compressor:
                        result = prepare_request_info(raw, {}, 'POST', '/responses', 'zstd', level,
                                                      auto_zstd_level=True, budget_bytes=budget)
                        if size < boundary:
                            compressor.assert_not_called()
                            self.assertTrue(result.body is raw, 'small original must pass through unchanged')
                            self.assertEqual(result.encoding, 'identity')
                            self.assertIsNone(result.zstd_level)
                        else:
                            compressor.assert_called_once_with(level=level)
                            self.assertEqual(result.zstd_level, level)
                            self.assertEqual(zstandard.ZstdDecompressor().decompress(result.body), raw)

    def test_small_native_zstd_is_preserved_even_in_high_mode(self):
        raw = encode({'input': 'small original ' * 20})
        incoming = zstandard.ZstdCompressor(level=1).compress(raw)
        for level in (1, 16):
            with self.subTest(level=level), patch('zstandard.ZstdCompressor', wraps=zstandard.ZstdCompressor) as compressor:
                result = prepare_request_info(incoming, {'Content-Encoding': 'zstd'}, 'POST', '/responses', 'zstd', level,
                                              auto_zstd_level=True, budget_bytes=1000)
                compressor.assert_not_called()
                self.assertIs(result.body, incoming)
                self.assertEqual(result.encoding, 'zstd')
                self.assertIsNone(result.zstd_level)

    def test_small_native_wire_with_large_original_still_tries_high_mode(self):
        raw = encode({'input': 'large original ' * 10000})
        incoming = zstandard.ZstdCompressor(level=1).compress(raw)
        self.assertLess(len(incoming), 9000)
        self.assertGreater(len(raw), 9000)
        with patch('zstandard.ZstdCompressor', wraps=zstandard.ZstdCompressor) as compressor:
            result = prepare_request_info(incoming, {'Content-Encoding': 'zstd'}, 'POST', '/responses', 'zstd', 16,
                                          auto_zstd_level=True, budget_bytes=10000)
            compressor.assert_called_once_with(level=16)
            self.assertLessEqual(len(result.body), len(incoming))
            self.assertEqual(zstandard.ZstdDecompressor().decompress(result.body), raw)

    def test_manual_mode_still_compresses_small_originals(self):
        raw = encode({'input': 'small manual request ' * 20})
        for level in (9, 17):
            with self.subTest(level=level), patch('zstandard.ZstdCompressor', wraps=zstandard.ZstdCompressor) as compressor:
                result = prepare_request_info(raw, {}, 'POST', '/responses', 'zstd', level,
                                              auto_zstd_level=False, budget_bytes=1000000)
                compressor.assert_called_once_with(level=level)
                self.assertEqual(result.zstd_level, level)
                self.assertEqual(zstandard.ZstdDecompressor().decompress(result.body), raw)

    def test_native_zstd_obeys_the_selected_level_even_when_already_oversized(self):
        raw = encode({'input': base64.b64encode(random.Random(3).randbytes(90000)).decode()})
        incoming = zstandard.ZstdCompressor(level=1).compress(raw)
        for budget in (50000, 100000):
            for level in (1, 16):
                with self.subTest(budget=budget, level=level), patch(
                        'zstandard.ZstdCompressor', wraps=zstandard.ZstdCompressor) as compressor:
                    result = prepare_request_info(incoming, {'Content-Encoding': 'zstd'}, 'POST', '/responses', 'zstd', level,
                                                  auto_zstd_level=True, budget_bytes=budget)
                    if level == 1:
                        compressor.assert_not_called()
                        self.assertIs(result.body, incoming)
                    else:
                        compressor.assert_called_once_with(level=16)
                        self.assertLessEqual(len(result.body), len(incoming))
                    self.assertEqual(zstandard.ZstdDecompressor().decompress(result.body), raw)

    def test_off_gzip_and_unverified_endpoints_remain_untouched(self):
        raw = encode({'input': 'retained ' * 1000})
        for body, headers, path, mode in ((raw, {}, '/responses', 'off'),
                                          (gzip.compress(raw), {'Content-Encoding': 'gzip'}, '/responses', 'zstd'),
                                          (raw, {}, '/responses/compact', 'zstd')):
            result = prepare_request_info(body, headers, 'POST', path, mode, 16,
                                          auto_zstd_level=True, budget_bytes=100)
            self.assertIs(result.body, body)
            self.assertIsNone(result.zstd_level)

    def test_rebuilt_bodies_use_the_selected_mode_and_report_the_actual_level(self):
        raw = encode({'input': 'retained ' * 10000})
        for budget in (100000, 110000):
            for level in (1, 16):
                with self.subTest(budget=budget, level=level):
                    result = encode_changed_info(raw, {'Content-Encoding': 'zstd'}, '/responses', 'zstd', level,
                                                 auto_zstd_level=True, budget_bytes=budget)
                    if budget == 110000:
                        self.assertTrue(result.body is raw, 'small rebuilt original must pass through unchanged')
                        self.assertEqual(result.encoding, 'identity')
                        self.assertIsNone(result.zstd_level)
                    else:
                        self.assertEqual(result.zstd_level, level)
                        self.assertEqual(zstandard.ZstdDecompressor().decompress(result.body), raw)

    def test_saved_settings_preserve_manual_level_and_require_size_protection(self):
        for saved, enabled in (({}, False),
                                ({'auto_zstd_level_enabled': True}, False),
                                ({'packet_size_protection_enabled': True, 'auto_zstd_level_enabled': True}, True)):
            with self.subTest(saved=saved), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                config = root / 'config.toml'
                config.write_text('model="test"', encoding='utf-8')
                data = root / 'data'
                data.mkdir()
                (data / 'settings.json').write_text(json.dumps({'zstd_level': 9, **saved}), encoding='utf-8')
                with InspectorServer(('127.0.0.1', 0), data, config, 'http://127.0.0.1:9/v1') as server:
                    self.assertEqual(server.status()['auto_zstd_level_enabled'], enabled)
                    self.assertEqual(server.settings['zstd_level'], 9)

    def test_image_rewrite_skips_compression_for_small_body_and_keeps_paths(self):
        from packet_inspector.image_isolation import ImageIsolation
        from tests.test_image_isolation import request_body
        with tempfile.TemporaryDirectory() as folder:
            isolation = ImageIsolation(Path(folder))
            with patch('zstandard.ZstdCompressor', wraps=zstandard.ZstdCompressor) as compressor:
                result, coding, meta = isolation.prepare(encode(request_body()), {}, '/responses', 'zstd',
                                                          zstd_level=16, auto_zstd_level=True, budget_bytes=300000)
                compressor.assert_not_called()
            self.assertEqual((coding, meta['relay_zstd_level']), ('identity', None))
            self.assertNotIn(b'data:image', result)
            self.assertTrue(meta['image_paths'])


class AutomaticRelayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixtures.RelayTests.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls):
        fixtures.RelayTests.tearDownClass.__func__(cls)

    api = fixtures.RelayTests.api
    relay = fixtures.RelayTests.relay
    latest = fixtures.RelayTests.latest

    def setUp(self):
        self.assertEqual(self.api('/api/settings', {'packet_size_protection_enabled': False,
                                                   'auto_zstd_level_enabled': False, 'zstd_level': 9,
                                                   'budget_bytes': 300000})[0], 200)
        self.server.settings["auto_detect_providers"] = True
        self.server.compression_policy.clear()

    def enable(self):
        status, result = self.api('/api/settings', {'packet_size_protection_enabled': True,
                                                   'auto_zstd_level_enabled': True})
        self.assertEqual(status, 200, result)

    def test_settings_validate_atomically_and_parent_disable_restores_manual_mode(self):
        before = self.server.settings_path.read_bytes()
        for invalid in (True, 1, 'true', None):
            with self.subTest(invalid=invalid):
                self.assertEqual(self.api('/api/settings', {'auto_zstd_level_enabled': invalid, 'zstd_level': 7})[0], 400)
                self.assertEqual(self.server.settings_path.read_bytes(), before)
        self.enable()
        self.assertTrue(json.loads(self.server.settings_path.read_text())['auto_zstd_level_enabled'])
        status, result = self.api('/api/settings', {'packet_size_protection_enabled': False})
        self.assertEqual(status, 200)
        self.assertFalse(result['auto_zstd_level_enabled'])
        self.assertEqual(result['zstd_level'], 9)
        raw = encode({'input': 'retained ' * 1000})
        self.assertEqual(self.relay(raw)[0], 200)
        self.assertEqual(self.latest()[0]['relay_zstd_level'], 9)

    def test_fast_packet_schedules_high_next_and_small_main_body_releases_the_lock(self):
        self.enable()
        random_text = base64.b64encode(random.Random(7).randbytes(280000)).decode()
        big = encode({'input': random_text})
        small = encode({'input': 'small retained request ' * 20})
        headers = {'session_id': 'adaptive-session'}
        for raw, expected in ((big, [1]), (big, [16]), (small, []), (small, []), (big, [1]), (big, [16])):
            with self.subTest(expected=expected, size=len(raw)), patch('zstandard.ZstdCompressor', wraps=zstandard.ZstdCompressor) as compressor:
                self.assertEqual(self.relay(raw, headers=headers)[0], 200)
                self.assertEqual([call.kwargs['level'] for call in compressor.call_args_list], expected)
                meta, _ = self.latest()
                self.assertEqual(meta['relay_zstd_level'], expected[-1] if expected else None)
                uploaded = self.up.requests[-1]['body']
                self.assertEqual(zstandard.ZstdDecompressor().decompress(uploaded) if expected else uploaded, raw)
                self.assertLessEqual(len(self.up.requests[-1]['body']), 300000)
        self.assertEqual(self.relay(big, headers={'session_id': 'separate-session'})[0], 200)
        self.assertEqual(self.latest()[0]['relay_auto_zstd_phase'], 1)
        self.assertEqual(self.latest()[0]['relay_auto_zstd_next_phase'], 17)
        self.assertEqual(self.server.settings['zstd_level'], 9)

    def test_captured_small_manual_request_recovers_original_phase(self):
        raw = encode({'input': base64.b64encode(random.Random(8).randbytes(280000)).decode()})
        headers = {'session_id': 'prior-record-session'}
        self.assertEqual(self.relay(encode({'input': 'old manual request'}), headers=headers)[0], 200)
        self.enable()
        self.assertEqual(self.relay(raw, headers=headers)[0], 200)
        self.assertEqual(self.latest()[0]['relay_auto_zstd_phase'], 1)
        self.assertEqual(self.latest()[0]['relay_auto_zstd_previous_phase'], 0)
        self.assertEqual(self.latest()[0]['relay_auto_zstd_next_phase'], 17)
        auxiliary = encode({'input': 'tiny', 'client_metadata': {'request_kind': 'prewarm'}})
        self.assertEqual(self.relay(auxiliary, headers=headers)[0], 200)
        self.assertEqual(self.relay(raw, headers=headers)[0], 200)
        self.assertEqual(self.latest()[0]['relay_zstd_level'], 16)

    def test_fast_phase_does_not_bypass_the_final_upload_limit(self):
        self.enable()
        self.assertEqual(self.api('/api/settings', {'budget_bytes': 50001})[0], 200)
        raw = encode({'input': base64.b64encode(random.Random(4).randbytes(180000)).decode()})
        before = len(self.up.requests)
        status, _ = self.relay(raw, headers={'session_id': 'uncompressible-session'})
        self.assertEqual(status, 413)
        self.assertEqual(len(self.up.requests), before)
        self.assertEqual(self.latest()[0]['relay_error'], 'overflow_unsupported_input')

    def test_unseen_large_session_starts_in_high_mode_without_a_previous_packet(self):
        self.enable()
        raw = encode({'input': 'retained history ' * 100000})
        with patch('zstandard.ZstdCompressor', wraps=zstandard.ZstdCompressor) as compressor:
            self.assertEqual(self.relay(raw, headers={'session_id': 'unseen-large-session'})[0], 200)
            compressor.assert_called_once_with(level=16)
        meta, _ = self.latest()
        self.assertEqual(meta['relay_auto_zstd_phase'], 17)
        self.assertIsNone(meta['relay_auto_zstd_previous_phase'])
        self.assertEqual(meta['relay_auto_zstd_initial_threshold_bytes'], 1500000)
        self.assertEqual(meta['relay_auto_zstd_budget_bytes'], 300000)

    def test_reenabling_auto_uses_the_newer_small_manual_record(self):
        self.enable()
        headers = {'session_id': 'reenabled-session'}
        raw = encode({'input': base64.b64encode(random.Random(12).randbytes(280000)).decode()})
        self.assertEqual(self.relay(raw, headers=headers)[0], 200)
        self.assertEqual(self.api('/api/settings', {'auto_zstd_level_enabled': False})[0], 200)
        small = encode({'input': 'small manual request ' * 100})
        self.assertEqual(self.relay(small, headers=headers)[0], 200)
        self.enable()
        self.assertEqual(self.relay(raw, headers=headers)[0], 200)
        self.assertEqual(self.latest()[0]['relay_auto_zstd_phase'], 1)
        self.assertEqual(self.latest()[0]['relay_auto_zstd_previous_phase'], 0)
        self.assertEqual(self.latest()[0]['relay_auto_zstd_next_phase'], 17)

    def test_auxiliary_and_native_compaction_dispatches_cannot_release_high_phase(self):
        from packet_inspector.request_context import NEXT_MESSAGE_PREFIX, TITLE_REQUEST_PREFIX, IMAGE_ANALYST_PREFIXES
        self.enable()
        raw = encode({'input': base64.b64encode(random.Random(22).randbytes(280000)).decode()})
        headers = {'session_id': 'auxiliary-phase-session'}
        self.assertEqual(self.relay(raw, headers=headers)[0], 200)
        auxiliary = [
            ({'input': 'tiny', 'client_metadata': {'request_kind': kind}}, '/responses')
            for kind in ('prewarm', 'memory', 'compaction')
        ] + [
            ({'input': [{'role': 'user', 'content': prefix}]}, '/responses')
            for prefix in (NEXT_MESSAGE_PREFIX, TITLE_REQUEST_PREFIX, IMAGE_ANALYST_PREFIXES[0])
        ] + [
            ({'input': json.loads(raw)['input'], 'client_metadata': {'request_kind': 'compaction'}}, '/responses'),
            ({'input': 'tiny', 'client_metadata': {'thread_source': 'session_index'}}, '/responses'),
            ({'input': 'tiny'}, '/alpha/search'),
            ({'input': 'tiny'}, '/responses/compact'),
            ({'input': 'tiny', 'client_metadata': {'request_kind': 'compaction'}}, '/reject/responses'),
        ]
        for value, path in auxiliary:
            with self.subTest(path=path, kind=value.get('client_metadata')):
                self.assertEqual(self.relay(encode(value), path=path, headers=headers)[0], 403 if 'reject' in path else 200)
                auxiliary_meta = self.latest()[0]
                self.assertNotIn('relay_auto_zstd_phase', auxiliary_meta)
                if len(encode(value)) > 300000:
                    self.assertEqual(auxiliary_meta['relay_zstd_level'], 16)
                with patch('zstandard.ZstdCompressor', wraps=zstandard.ZstdCompressor) as compressor:
                    self.assertEqual(self.relay(raw, headers=headers)[0], 200)
                    compressor.assert_called_once_with(level=16)

    def test_capture_disabled_and_policy_restart_do_not_lose_or_contaminate_phase(self):
        self.enable()
        raw = encode({'input': base64.b64encode(random.Random(23).randbytes(280000)).decode()})
        headers = {'session_id': 'uncaptured-session'}
        self.assertEqual(self.relay(raw, headers=headers)[0], 200)
        try:
            self.assertEqual(self.api('/api/settings', {'capture_enabled': False})[0], 200)
            self.server.compression_policy = CompressionPolicy(self.server.store)
            auxiliary = encode({'input': 'tiny', 'client_metadata': {'thread_source': 'session_index'}})
            self.assertEqual(self.relay(auxiliary, headers=headers)[0], 200)
            with patch('zstandard.ZstdCompressor', wraps=zstandard.ZstdCompressor) as compressor:
                self.assertEqual(self.relay(raw, headers=headers)[0], 200)
                compressor.assert_called_once_with(level=16)
        finally:
            self.assertEqual(self.api('/api/settings', {'capture_enabled': True})[0], 200)


if __name__ == '__main__':
    unittest.main()
