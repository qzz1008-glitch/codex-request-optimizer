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
from packet_inspector.transport import CompressionHistory, prepare_request_info
from tests import test_packet_inspector as fixtures


class CompressionHistoryTests(unittest.TestCase):
    def test_session_pressure_scales_with_budget_and_keeps_a_stable_band(self):
        history = CompressionHistory()
        self.assertIsNone(history.level('session', 1000))
        history.remember('session', 899, 1)
        self.assertEqual(history.level('session', 1000), 1)
        history.remember('session', 900, 1)
        self.assertEqual(history.level('session', 1000), 17)
        history.remember('session', 850, 17)
        self.assertEqual(history.level('session', 1000), 17)
        self.assertEqual(history.level('session', 2000), 1)
        history.remember('session', 800, 17)
        self.assertEqual(history.level('session', 1000), 1)
        self.assertIsNone(history.level('other', 1000))

    def test_history_expires_is_bounded_and_late_seed_cannot_replace_new_data(self):
        history = CompressionHistory(max_entries=2, ttl=60)
        with patch('packet_inspector.transport.time.monotonic', return_value=0):
            history.remember('a', 950, 17)
            history.remember('b', 850, 17)
            history.remember('b', 100, 1, only_if_missing=True)
            self.assertEqual(history.level('b', 1000), 17)
            history.remember('c', 100, 1)
            self.assertIsNone(history.level('a', 1000))
        with patch('packet_inspector.transport.time.monotonic', return_value=60):
            self.assertIsNone(history.level('b', 1000))
            self.assertIsNone(history.level('c', 1000))


class AutomaticTransportTests(unittest.TestCase):
    def test_selected_mode_compresses_once_and_preserves_all_bytes(self):
        raw = encode({'input': 'retained text ' * 15000})
        for level in (1, 17):
            with self.subTest(level=level), patch('zstandard.ZstdCompressor', wraps=zstandard.ZstdCompressor) as compressor:
                prepared = prepare_request_info(raw, {}, 'POST', '/responses', 'zstd', level,
                                                auto_zstd_level=True, budget_bytes=200000)
                compressor.assert_called_once_with(level=level)
                self.assertEqual(prepared.zstd_level, level)
                self.assertEqual(zstandard.ZstdDecompressor().decompress(prepared.body), raw)

    def test_fast_mode_keeps_native_zstd_and_oversized_native_packet_goes_straight_to_17(self):
        raw = encode({'input': base64.b64encode(random.Random(3).randbytes(90000)).decode()})
        incoming = zstandard.ZstdCompressor(level=1).compress(raw)
        for budget, selected, expected in ((len(incoming) + 100, 1, None),
                                            (len(incoming) - 1, 1, 17),
                                            (len(incoming) + 100, 17, 17)):
            with self.subTest(budget=budget, selected=selected), patch('zstandard.ZstdCompressor', wraps=zstandard.ZstdCompressor) as compressor:
                result = prepare_request_info(incoming, {'Content-Encoding': 'zstd'}, 'POST', '/responses', 'zstd', selected,
                                              auto_zstd_level=True, budget_bytes=budget)
                if expected is None:
                    compressor.assert_not_called()
                    self.assertIs(result.body, incoming)
                else:
                    compressor.assert_called_once_with(level=17)
                    self.assertLessEqual(len(result.body), len(incoming))
                self.assertEqual(zstandard.ZstdDecompressor().decompress(result.body), raw)
                self.assertEqual(result.zstd_level, 17 if len(result.body) < len(incoming) else None)

    def test_off_gzip_and_unverified_endpoints_remain_untouched(self):
        raw = encode({'input': 'retained ' * 1000})
        for body, headers, path, mode in ((raw, {}, '/responses', 'off'),
                                          (gzip.compress(raw), {'Content-Encoding': 'gzip'}, '/responses', 'zstd'),
                                          (raw, {}, '/responses/compact', 'zstd')):
            result = prepare_request_info(body, headers, 'POST', path, mode, 17,
                                          auto_zstd_level=True, budget_bytes=100)
            self.assertIs(result.body, body)
            self.assertIsNone(result.zstd_level)

    def test_rebuilt_bodies_use_the_selected_mode_and_report_the_actual_level(self):
        raw = encode({'input': 'retained ' * 10000})
        for level in (1, 17):
            result = encode_changed_info(raw, {'Content-Encoding': 'zstd'}, '/responses', 'zstd', level,
                                         auto_zstd_level=True, budget_bytes=100000)
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

    def test_image_rewrite_uses_one_selected_level_and_keeps_paths(self):
        from packet_inspector.image_isolation import ImageIsolation
        from tests.test_image_isolation import request_body
        with tempfile.TemporaryDirectory() as folder:
            isolation = ImageIsolation(Path(folder))
            result, coding, meta = isolation.prepare(encode(request_body()), {}, '/responses', 'zstd',
                                                      zstd_level=1, auto_zstd_level=True, budget_bytes=300000)
            self.assertEqual((coding, meta['relay_zstd_level']), ('zstd', 1))
            decoded = zstandard.ZstdDecompressor().decompress(result)
            self.assertNotIn(b'data:image', decoded)
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
                                                   'request_compression': 'zstd', 'budget_bytes': 300000})[0], 200)
        self.server.compression_history = CompressionHistory()

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

    def test_previous_real_upload_changes_next_request_once_and_other_sessions_stay_fast(self):
        self.enable()
        random_text = base64.b64encode(random.Random(7).randbytes(280000)).decode()
        big = encode({'input': random_text})
        small = encode({'input': 'small retained request ' * 20})
        headers = {'session_id': 'adaptive-session'}
        for raw, expected in ((big, 1), (big, 17), (small, 17), (small, 1)):
            with self.subTest(expected=expected, size=len(raw)), patch('zstandard.ZstdCompressor', wraps=zstandard.ZstdCompressor) as compressor:
                self.assertEqual(self.relay(raw, headers=headers)[0], 200)
                compressor.assert_called_once_with(level=expected)
                meta, _ = self.latest()
                self.assertEqual(meta['relay_zstd_level'], expected)
                self.assertEqual(zstandard.ZstdDecompressor().decompress(self.up.requests[-1]['body']), raw)
                self.assertLessEqual(len(self.up.requests[-1]['body']), 300000)
        self.assertEqual(self.relay(big, headers={'session_id': 'separate-session'})[0], 200)
        self.assertEqual(self.latest()[0]['relay_zstd_level'], 1)
        self.assertEqual(self.server.settings['zstd_level'], 9)

    def test_existing_measurement_is_used_and_auxiliary_requests_do_not_reset_pressure(self):
        raw = encode({'input': base64.b64encode(random.Random(8).randbytes(280000)).decode()})
        headers = {'session_id': 'prior-record-session'}
        self.assertEqual(self.relay(raw, headers=headers)[0], 200)
        self.enable()
        self.assertEqual(self.relay(raw, headers=headers)[0], 200)
        self.assertEqual(self.latest()[0]['relay_zstd_level'], 17)
        auxiliary = encode({'input': 'tiny', 'client_metadata': {'request_kind': 'prewarm'}})
        self.assertEqual(self.relay(auxiliary, headers=headers)[0], 200)
        self.assertEqual(self.relay(raw, headers=headers)[0], 200)
        self.assertEqual(self.latest()[0]['relay_zstd_level'], 17)

    def test_one_compression_does_not_bypass_the_final_upload_limit(self):
        self.enable()
        self.assertEqual(self.api('/api/settings', {'budget_bytes': 50001})[0], 200)
        raw = encode({'input': base64.b64encode(random.Random(4).randbytes(180000)).decode()})
        before = len(self.up.requests)
        status, _ = self.relay(raw, headers={'session_id': 'uncompressible-session'})
        self.assertEqual(status, 413)
        self.assertEqual(len(self.up.requests), before)
        self.assertEqual(self.latest()[0]['relay_error'], 'overflow_unsupported_input')

    def test_prior_measurement_from_another_upstream_is_not_used(self):
        raw = encode({'input': base64.b64encode(random.Random(11).randbytes(280000)).decode()})
        headers = {'session_id': 'changed-upstream-session'}
        self.assertEqual(self.relay(raw, headers=headers)[0], 200)
        previous, _ = self.latest()
        self.server.store.update(previous['id'], relay_compression_scope='different-upstream')
        self.enable()
        self.assertEqual(self.relay(raw, headers=headers)[0], 200)
        self.assertEqual(self.latest()[0]['relay_zstd_level'], 1)

    def test_reenabling_auto_uses_the_latest_manual_request_instead_of_old_pressure(self):
        self.enable()
        headers = {'session_id': 'reenabled-session'}
        raw = encode({'input': base64.b64encode(random.Random(12).randbytes(280000)).decode()})
        self.assertEqual(self.relay(raw, headers=headers)[0], 200)
        self.assertEqual(self.api('/api/settings', {'auto_zstd_level_enabled': False})[0], 200)
        small = encode({'input': 'small manual request ' * 100})
        self.assertEqual(self.relay(small, headers=headers)[0], 200)
        self.enable()
        self.assertEqual(self.relay(small, headers=headers)[0], 200)
        self.assertEqual(self.latest()[0]['relay_zstd_level'], 1)


if __name__ == '__main__':
    unittest.main()
