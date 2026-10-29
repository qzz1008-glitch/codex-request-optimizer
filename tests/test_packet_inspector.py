"""Regression tests for byte transparency, request accounting, and recovery."""
from __future__ import annotations

import gzip
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
import tomllib
import unittest
from unittest.mock import patch
from urllib.request import Request

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / ".local" / "builddeps"))
import zstandard

from packet_inspector.analyzer import (CLASSIFICATION_VERSION, JsonBytes, compare_packets, compressed_packet_size, describe_node,
                                      encode, inspect_request, resolve, search_body)
from packet_inspector.config_link import ConfigLink, PROVIDER
from packet_inspector.server import InspectorServer, outbound_proxies
from tests.relay_fixture import HTTP_TIMEOUT, wait_for_idle
from packet_inspector.storage import Store
from packet_inspector.transport import prepare_request


class ProxyTests(unittest.TestCase):
    def test_no_proxy_alone_keeps_windows_system_proxy(self):
        with patch("packet_inspector.server.urllib_request.getproxies",
                   return_value={"no": "localhost,127.0.0.1"}), \
                patch("packet_inspector.server.urllib_request.getproxies_registry",
                      return_value={"https": "http://system-proxy.invalid:8080"}, create=True):
            self.assertEqual(outbound_proxies(), {
                "https": "http://system-proxy.invalid:8080", "no": "localhost,127.0.0.1"})

    def test_explicit_proxy_retains_environment_precedence(self):
        proxies = {"https": "http://environment-proxy.invalid:8080", "no": "localhost"}
        with patch("packet_inspector.server.urllib_request.getproxies", return_value=proxies), \
                patch("packet_inspector.server.urllib_request.getproxies_registry", create=True) as registry:
            self.assertEqual(outbound_proxies(), proxies)
            registry.assert_not_called()


class TransportTests(unittest.TestCase):
    def test_compression_preserves_every_payload_byte(self):
        raw=b' { "input": ["spaces and escapes \\n"], "image_url": "data:image/png;base64,FAKE", "encrypted_content":"' + b"synthetic"*500 + b'" } '
        wire,encoding=prepare_request(raw,{},"POST","/responses","zstd")
        self.assertEqual(encoding,"zstd")
        self.assertLess(len(wire),len(raw))
        self.assertEqual(zstandard.ZstdDecompressor().decompress(wire),raw)

    def test_existing_encodings_and_unverified_endpoints_are_untouched(self):
        raw=encode({"input":"repeat "*500})
        for encoding,wire in [("zstd",zstandard.ZstdCompressor().compress(raw)),("gzip",gzip.compress(raw))]:
            self.assertEqual(prepare_request(wire,{"content-encoding":encoding},"POST","/responses","zstd"),(wire,encoding))
        for method,path,mode in [("POST","/responses","off"),("GET","/responses","zstd"),("POST","/responses/compact","zstd"),("POST","/other","zstd")]:
            self.assertEqual(prepare_request(raw,{},method,path,mode),(raw,"identity"))


class ZstdLevelTests(unittest.TestCase):
    def test_all_selectable_levels_preserve_uncompressed_bytes(self):
        raw = encode({"input": [{"role": "user", "content": "retained content " * 1500}]})
        for level in range(1, 23):
            with self.subTest(level=level):
                wire, coding = prepare_request(raw, {}, "POST", "/responses", "zstd", level)
                self.assertEqual(coding, "zstd")
                self.assertEqual(zstandard.ZstdDecompressor().decompress(wire), raw)
                self.assertEqual(wire, zstandard.ZstdCompressor(level=level).compress(raw))

    def test_recompression_keeps_the_smaller_packet_and_opaque_invalid_input(self):
        raw = encode({"input": "repeated source text " * 4000})
        incoming = zstandard.ZstdCompressor(level=1).compress(raw)
        expected = zstandard.ZstdCompressor(level=16).compress(raw)
        wire, coding = prepare_request(incoming, {"Content-Encoding": "zstd"},
                                       "POST", "/responses", "zstd", 16)
        self.assertEqual(coding, "zstd")
        self.assertEqual(zstandard.ZstdDecompressor().decompress(wire), raw)
        self.assertEqual(len(wire), min(len(incoming), len(expected)))
        self.assertEqual(prepare_request(incoming, {"Content-Encoding": "zstd"},
                                        "POST", "/responses", "off", 16), (incoming, "zstd"))
        invalid = b"opaque invalid zstd frame"
        self.assertEqual(prepare_request(invalid, {"Content-Encoding": "zstd"},
                                        "POST", "/responses", "zstd", 16), (invalid, "zstd"))

    def test_settings_reload_preserves_valid_level_and_defaults_legacy_config(self):
        for saved, expected in (({}, 3), ({"zstd_level": 17, "request_compression": "off"}, 17),
                                ({"zstd_level": 22}, 22), ({"zstd_level": 23}, 3), ({"zstd_level": True}, 3)):
            with self.subTest(saved=saved), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                config = root / "config.toml"
                config.write_text('model="test"', encoding="utf8")
                data = root / "data"
                data.mkdir()
                (data / "settings.json").write_text(json.dumps(saved), encoding="utf8")
                with InspectorServer(("127.0.0.1", 0), data, config, "http://127.0.0.1:9/v1") as server:
                    self.assertEqual(server.status()["zstd_level"], expected)
                    self.assertNotIn("request_compression", server.settings)
                    self.assertEqual(json.loads(server.settings_path.read_text(encoding="utf8"))["zstd_level"], expected)


class AnalyzerTests(unittest.TestCase):
    def test_tool_search_is_not_a_generation_or_compaction_request(self):
        for path in ("/alpha/search", "/v1/alpha/search/?source=test"):
            with self.subTest(path=path):
                meta, _ = inspect_request(encode({"query": "find a tool", "model": "m"}),
                                          {"x-codex-turn-metadata": '{"request_kind":"compaction"}'}, path)
                self.assertEqual(meta["request_kind"], "tool_search")
                self.assertFalse(meta["is_compaction"])

    def test_upload_comparison_uses_final_bytes_and_keeps_unknown(self):
        old={"wire_bytes":1100000,"outbound_wire_bytes":400000}
        new={"wire_bytes":1000000,"outbound_wire_bytes":500000}
        diff=compare_packets(old,new)
        self.assertEqual(diff["upload_delta"],100000)
        self.assertEqual(diff["wire_delta"],-100000)
        self.assertIsNone(compare_packets({"wire_bytes":2000000},new)["upload_delta"])
        self.assertIsNone(compare_packets(old,{"wire_bytes":2000000})["upload_delta"])
        self.assertEqual(compare_packets({"outbound_wire_bytes":0},{"outbound_wire_bytes":0})["upload_delta"],0)

    def test_compressed_comparison_excludes_summary_replacement(self):
        old = {"decoded_bytes": 2000000, "overflow_original_wire_bytes": 1100000,
               "outbound_wire_bytes": 100000}
        new = {"decoded_bytes": 2100000, "overflow_original_wire_bytes": 1200000,
               "outbound_wire_bytes": 50000}
        diff = compare_packets(old, new)
        self.assertEqual(diff["compressed_delta"], 100000)
        self.assertEqual(diff["decoded_delta"], 100000)
        self.assertEqual(diff["upload_delta"], -50000)
        self.assertEqual(compressed_packet_size({"usage_override_request_bytes": 950000,
                                                "outbound_wire_bytes": 200000}), 950000)
        self.assertEqual(compressed_packet_size({"overflow_original_wire_bytes": 0,
                                                "outbound_wire_bytes": 200000}), 0)
        self.assertEqual(compressed_packet_size({"outbound_wire_bytes": 559669}), 559669)
        for invalid in (None, True, -1, "559669"):
            self.assertIsNone(compressed_packet_size({"outbound_wire_bytes": invalid}))
        self.assertIsNone(compare_packets({"wire_bytes": 100}, new)["compressed_delta"])
        self.assertIsNone(compare_packets(old, {"decoded_bytes": 100})["compressed_delta"])
        self.assertEqual(compare_packets({"outbound_wire_bytes": 0},
                                        {"outbound_wire_bytes": 0})["compressed_delta"], 0)

    def test_summary_comparison_retains_size_changes_without_item_content(self):
        old = {"top_fields": [{"name": "input", "bytes": 50}], "wire_bytes": 80, "decoded_bytes": 100,
               "outbound_wire_bytes": 400, "input_items": [{"fingerprint": "old", "label": "private old"}]}
        new = {"top_fields": [{"name": "input", "bytes": 80}], "wire_bytes": 90, "decoded_bytes": 130,
               "outbound_wire_bytes": 450, "input_items": [{"fingerprint": "new", "label": "private new"}]}
        for previous_evicted, current_evicted in ((True, False), (False, True), (True, True)):
            with self.subTest(previous_evicted=previous_evicted, current_evicted=current_evicted):
                diff = compare_packets({**old, "details_evicted": previous_evicted}, {**new, "details_evicted": current_evicted})
                self.assertEqual(diff["state"], "summary_only")
                self.assertEqual(diff["compressed_delta"], 50)
                self.assertEqual(diff["decoded_delta"], 30)
                self.assertEqual(diff["fields"], [{"name": "input", "before": 50, "after": 80}])
                self.assertNotIn("added", diff)
                self.assertNotIn("removed", diff)
                self.assertNotIn("private", json.dumps(diff))

    def test_summary_comparison_keeps_missing_statistics_unknown(self):
        for missing in ({}, {"top_fields": None, "decoded_bytes": None, "wire_bytes": None, "outbound_wire_bytes": None}):
            with self.subTest(missing=missing):
                diff = compare_packets({"details_evicted": True, **missing}, {"top_fields": [{"name": "input", "bytes": 80}]})
                self.assertFalse(diff["fields_available"])
                self.assertEqual(diff["fields"], [])
                for key in ("decoded_delta", "wire_delta", "upload_delta", "compressed_delta"):
                    self.assertIsNone(diff[key])

    def test_compaction_dispatch_metadata_in_headers_and_body(self):
        dispatch={"request_kind":"compaction","compaction":{"trigger":"auto","reason":"context_limit",
                  "implementation":"responses","phase":"mid_turn","strategy":"memento","access_token":"secret"}}
        for metadata in [dispatch,json.dumps(dispatch)]:
            for body,headers in [({"client_metadata":{"x-codex-turn-metadata":metadata}},{}),
                                 ({},{"X-Codex-Turn-Metadata":json.dumps(dispatch)})]:
                with self.subTest(metadata_type=type(metadata).__name__,header=bool(headers)):
                    raw=encode({"input":[{"role":"user","content":"checkpoint summary"}],**body})
                    m,_=inspect_request(raw,headers,"/backend-api/codex/responses")
                    self.assertTrue(m["is_compaction"])
                    self.assertEqual(m["compaction_reason"],"request_metadata")
                    self.assertEqual(m["compaction_details"]["implementation"],"responses")
                    self.assertNotIn("secret",json.dumps(m))
                    self.assertEqual(m["wire_bytes"],len(raw))

    def test_history_and_transport_do_not_imply_compaction(self):
        history=[{"type":"compaction","encrypted_content":"old summary"},
                 {"type":"context_compaction"},{"role":"user","content":"request_kind=compaction /compact"}]
        for current in [None,{"request_kind":"turn"},"invalid json",{"request_kind":[]}]:
            body={"input":history,"client_metadata":{"x-codex-turn-metadata":current}}
            raw=encode(body)
            for coding,wire in [("identity",raw),("gzip",gzip.compress(raw)),("zstd",zstandard.ZstdCompressor().compress(raw))]:
                m,_=inspect_request(wire,{"Content-Encoding":coding},"/responses")
                self.assertFalse(m["is_compaction"])
                self.assertEqual(m["inspection"],"json")

    def test_compact_endpoint_and_dispatch_header_work_without_json(self):
        for raw in (b"",b"opaque"):
            m,_=inspect_request(raw,{},"/responses/compact?source=test")
            self.assertTrue(m["is_compaction"])
            m,_=inspect_request(raw,{"x-codex-turn-metadata":'{"request_kind":"compaction"}'},"/responses")
            self.assertTrue(m["is_compaction"])

    def test_original_bytes_include_chinese_escapes_and_whitespace(self):
        raw = ' { "input": [ {"role":"user", "content":"中文\\n\\u4e00"} ], "model" : "m" } \n'.encode()
        index = JsonBytes(raw)
        for path, (a, b) in index.spans.items():
            self.assertEqual(json.loads(raw[a:b]), resolve(json.loads(raw), path))
            self.assertEqual(index.sizes[path], b-a)
        m, view = inspect_request(raw, {})
        self.assertEqual(m["inspection"], "json")
        self.assertEqual(sum(r["bytes"] for r in m["top_fields"])+m["json_envelope_bytes"], len(raw))
        self.assertEqual(view["source_sizes"]["/input/0/content"], len('"中文\\n\\u4e00"'.encode()))

    def test_redaction_is_separate_from_size_accounting(self):
        payload = {"metadata":{"access_token":"never-store-this-token"},
                   "input":[{"role":"user","content":[{"type":"input_image","image_url":"https://invalid.example/image.png"}]},
                            {"type":"reasoning","encrypted_content":"ciphertext"}]}
        raw = encode(payload)
        m, v = inspect_request(raw, {"Authorization":"Bearer secret", "X-Custom-Auth":"custom-secret"})
        rendered=encode({"m":m,"v":v})
        self.assertNotIn(b"never-store-this-token",rendered)
        self.assertNotIn(b"custom-secret",rendered)
        self.assertNotIn(b"invalid.example",rendered)
        self.assertEqual(m["wire_bytes"],len(raw))
        self.assertEqual(m["encrypted_content_bytes"],10)
        self.assertIn("omitted",v["body"]["input"][0]["content"][0]["image_url"])

    def test_gzip_zstd_and_unreadable_content(self):
        raw=encode({"input":[],"model":"中文模型"})
        for encoding,body in [("gzip",gzip.compress(raw)),("zstd",zstandard.ZstdCompressor().compress(raw))]:
            with self.subTest(encoding=encoding):
                m,v=inspect_request(body,{"Content-Encoding":encoding})
                self.assertEqual(m["decoded_bytes"],len(raw))
                self.assertEqual(m["wire_bytes"],len(body))
                self.assertEqual(v["body"],json.loads(raw))
        for body,headers in [(b"not json",{}),(raw,{"Content-Encoding":"unknown"})]:
            m,v=inspect_request(body,headers)
            self.assertEqual(m["inspection"],"unavailable")
            self.assertIsNone(v)

    def test_null_and_string_input_remain_viewable(self):
        for value in [{"input":None},{"input":"text"},{"input":[{"type":"reasoning","encrypted_content":None}]},[1,2]]:
            m,v=inspect_request(encode(value),{})
            self.assertEqual(m["inspection"],"json")
            self.assertEqual(v["body"],value)

    def test_tree_search_pointer_and_paging(self):
        body={"a/b~c":["中文 needle"]*130}
        raw=encode(body);index=JsonBytes(raw)
        node=describe_node(body,"/a~1b~0c",index.sizes)
        self.assertEqual(node["next_offset"],100)
        self.assertEqual(len(node["children"]),100)
        self.assertEqual(len(search_body(body,"needle")),60)
        self.assertEqual(resolve(body,"/a~1b~0c/0"),"中文 needle")

    def test_diff_respects_repeated_items(self):
        old,_=inspect_request(encode({"input":[{"type":"reasoning","encrypted_content":"x"}]*2}),{})
        new,_=inspect_request(encode({"input":[{"type":"reasoning","encrypted_content":"x"}]}),{})
        diff=compare_packets(old,new)
        self.assertEqual(len(diff["removed"]),1)
        self.assertFalse(diff["added"])
        self.assertLess(diff["wire_delta"],0)


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.path=self.root/"config.toml"
        self.link=ConfigLink(self.path,self.root/"state.json")

    def test_link_and_restore_preserve_later_user_edits(self):
        self.path.write_text('  model_provider = "old"\nmodel = "m"\n[model_providers.old]\nname="Existing"\nbase_url="http://127.0.0.1:47843/backend-api/codex"\nwire_api="responses"\nhttp_headers={"X-Required"="abc"}\n',encoding="utf8")
        r=self.link.activate("http://127.0.0.1:47844/relay/test","http://127.0.0.1:47843/backend-api/codex")
        self.assertTrue(Path(r["backup"]).is_file())
        data=tomllib.loads(self.path.read_text())
        self.assertEqual(data["model_provider"],PROVIDER)
        self.assertEqual(data["model_providers"][PROVIDER]["http_headers"],{"X-Required":"abc"})
        self.assertFalse(data["model_providers"][PROVIDER]["supports_websockets"])
        self.path.write_text(self.path.read_text().replace('model = "m"','model = "later"'),encoding="utf8")
        self.link.restore()
        restored=tomllib.loads(self.path.read_text())
        self.assertEqual(restored["model_provider"],"old")
        self.assertEqual(restored["model"],"later")
        self.assertNotIn(PROVIDER,restored["model_providers"])

    def test_native_provider_recovery(self):
        self.path.write_text('model="m"',encoding="utf8")
        self.link.activate("http://127.0.0.1:47844/relay/test","https://chatgpt.com/backend-api/codex")
        self.assertTrue(tomllib.loads(self.path.read_text())["model_providers"][PROVIDER]["requires_openai_auth"])
        self.link.restore()
        self.assertNotIn("model_provider",tomllib.loads(self.path.read_text()))

    def test_rejects_loop_and_untrusted_plaintext_upstream(self):
        for url in ["http://127.0.0.1:47844/relay/a","http://remote.example","https://user:pass@remote.example"]:
            with self.subTest(url=url),self.assertRaises(ValueError):
                self.link.activate("http://127.0.0.1:47844/relay/test",url)
        self.assertFalse(self.path.exists())

    def test_state_exists_before_config_changes(self):
        def check_write(_):
            self.assertTrue(self.link.state_path.is_file())
            raise OSError("test disk failure")
        with patch.object(self.link,"_write",check_write),self.assertRaises(OSError):
            self.link.activate("http://127.0.0.1:47844/relay/test","https://example.invalid/v1")
        self.assertFalse(self.path.exists())
        self.assertFalse(self.link.state_path.exists())

    def context_catalog(self, percent=95):
        (self.root/"models_cache.json").write_text(json.dumps({"models":[{"slug":"m","context_window":500000,
            "max_context_window":872000,"effective_context_window_percent":percent}]}),encoding="utf8")

    def test_context_save_converts_effective_window_and_preserves_other_content(self):
        self.context_catalog()
        original=('model="m"\nmodel_provider="old"\n'
                  'instructions="""Example:\nmodel_context_window = 10\n[example]\n"""\n'
                  '"model_context_window" = 178_948 # retain this note\n'
                  "'model_auto_compact_token_limit' = 155000\n"
                  '[model_providers.old]\nbase_url="https://example.invalid"\n'
                  'model_context_window = 888\n')
        self.path.write_text(original,encoding="utf8")
        result=self.link.save_context_settings({"context_tokens":240000,"compact_tokens":220000})
        saved=self.path.read_text(encoding="utf8");data=tomllib.loads(saved)
        self.assertEqual(data["model_context_window"],252632)
        self.assertEqual(data["model_auto_compact_token_limit"],220000)
        self.assertEqual(data["model_provider"],"old")
        self.assertEqual(data["instructions"],tomllib.loads(original)["instructions"])
        self.assertEqual(data["model_providers"],tomllib.loads(original)["model_providers"])
        self.assertIn('# retain this note',saved)
        self.assertEqual(result["context_tokens"],240000)
        self.assertEqual(result["compact_tokens"],220000)
        self.assertEqual(Path(result["backup"]).read_text(encoding="utf8"),original)
        self.assertTrue(result["reload_required"])

    def test_context_defaults_remove_only_root_overrides(self):
        self.context_catalog()
        original='model="m"\nmodel_context_window=252632\nmodel_auto_compact_token_limit=220000\n[tools]\nmodel_context_window=10\n'
        self.path.write_text(original,encoding="utf8")
        result=self.link.save_context_settings({"context_tokens":None,"compact_tokens":None})
        data=tomllib.loads(self.path.read_text())
        self.assertNotIn("model_context_window",data)
        self.assertNotIn("model_auto_compact_token_limit",data)
        self.assertEqual(data["tools"]["model_context_window"],10)
        self.assertIsNone(result["context_tokens"])
        self.assertIsNone(result["compact_tokens"])
        self.assertEqual(result["default_context_tokens"],475000)
        again=self.link.save_context_settings({"context_tokens":None,"compact_tokens":None})
        self.assertFalse(again["changed"])
        self.assertFalse(again["backup"])

    def test_invalid_context_changes_do_not_touch_config_or_create_backup(self):
        self.context_catalog()
        original='model="m"\n# unchanged\n'
        self.path.write_text(original,encoding="utf8")
        for values in [{"context_tokens":240000,"compact_tokens":241000},
                       {"context_tokens":None,"compact_tokens":500000},
                       {"context_tokens":900000,"compact_tokens":220000},
                       {"context_tokens":True,"compact_tokens":220000},
                       {"context_tokens":240000.0,"compact_tokens":220000},
                       {"context_tokens":240000,"compact_tokens":0},
                       {"context_tokens":240000},[],None]:
            with self.subTest(values=values),self.assertRaises(ValueError):
                self.link.save_context_settings(values)
            self.assertEqual(self.path.read_text(encoding="utf8"),original)
            self.assertFalse(list(self.root.glob('config.toml.inspector-backup-*')))

    def test_context_can_use_defaults_independently_and_catalog_percentage(self):
        self.context_catalog(percent=90)
        self.path.write_text('model="m"\n',encoding="utf8")
        result=self.link.save_context_settings({"context_tokens":240000,"compact_tokens":None})
        self.assertEqual(tomllib.loads(self.path.read_text())["model_context_window"],266667)
        self.assertEqual(result["context_tokens"],240000)
        self.assertIsNone(result["compact_tokens"])
        result=self.link.save_context_settings({"context_tokens":None,"compact_tokens":220000})
        self.assertIsNone(result["context_tokens"])
        self.assertEqual(result["compact_tokens"],220000)

    def test_context_defaults_work_without_model_catalog(self):
        result=self.link.context_settings()
        self.assertIsNone(result["default_context_tokens"])
        result=self.link.save_context_settings({"context_tokens":240000,"compact_tokens":220000})
        self.assertEqual(result["context_tokens"],240000)
        self.assertEqual(result["effective_percent"],95)

    def test_context_write_failure_preserves_original(self):
        original='model="m"\n'
        self.path.write_text(original,encoding="utf8")
        with patch.object(self.link,"_write",side_effect=OSError("disk failure")),self.assertRaises(OSError):
            self.link.save_context_settings({"context_tokens":240000,"compact_tokens":220000})
        self.assertEqual(self.path.read_text(encoding="utf8"),original)


class StorageTests(unittest.TestCase):
    def test_legacy_tool_search_is_reclassified_without_changing_the_packet(self):
        store = Store(self.path)
        meta, content = inspect_request(encode({"query": "find a tool", "model": "m"}), {}, "/alpha/search")
        meta.update(method="POST", path="/alpha/search", thread_id="search-test", classification_version=1)
        meta.pop("request_kind")
        packet_id = store.add(meta, content)
        store.update(packet_id, state="complete", status=200, outbound_wire_bytes=2599)
        with store.connect() as connection:
            original = connection.execute("SELECT content FROM packets WHERE id=?", (packet_id,)).fetchone()[0]
        self.assertEqual(store.list(kind="generation")[0]["id"], packet_id)
        self.assertEqual(store.reclassify_pending(), 1)
        self.assertFalse(store.list(kind="generation"))
        self.assertEqual(store.list(kind="other")[0]["id"], packet_id)
        store.update(packet_id, response_bytes=47067)
        saved, viewing_copy = store.get(packet_id, True)
        self.assertEqual(saved["request_kind"], "tool_search")
        self.assertEqual((saved["status"], saved["outbound_wire_bytes"]), (200, 2599))
        self.assertEqual(viewing_copy, content)
        self.assertEqual(store.list(kind="other")[0]["id"], packet_id)
        with store.connect() as connection:
            self.assertEqual(connection.execute("SELECT content FROM packets WHERE id=?", (packet_id,)).fetchone()[0], original)

    def test_legacy_classification_repairs_filter_without_changing_content(self):
        s=Store(self.path)
        raw=encode({"input":[{"role":"user","content":"handoff"}],
                    "client_metadata":{"x-codex-turn-metadata":'{"request_kind":"compaction"}'}})
        meta,content=inspect_request(raw,{})
        meta.update(is_compaction=False,method="POST",path="/responses",thread_id="old")
        for key in ("classification_version","request_kind","compaction_reason"):
            meta.pop(key,None)
        pid=s.add(meta,content)
        s.update(pid,state="complete",status=200,outbound_wire_bytes=123)
        with s.connect() as c:
            original=c.execute("SELECT content FROM packets WHERE id=?",(pid,)).fetchone()[0]
        self.assertFalse(s.list(kind="compaction"))
        self.assertEqual(s.reclassify_pending(),1)
        saved,view=s.get(pid,True)
        self.assertEqual(s.list(kind="compaction")[0]["id"],pid)
        self.assertEqual(saved["status"],200)
        self.assertEqual(saved["outbound_wire_bytes"],123)
        self.assertEqual(view,content)
        with s.connect() as c:
            self.assertEqual(c.execute("SELECT content FROM packets WHERE id=?",(pid,)).fetchone()[0],original)
            plan=c.execute(f"EXPLAIN QUERY PLAN SELECT id FROM packet_summaries WHERE coalesce(json_extract(meta,'$.classification_version'),0)<{CLASSIFICATION_VERSION} ORDER BY created DESC,id DESC LIMIT 8").fetchall()
        self.assertIn(f"packet_summary_classification_v{CLASSIFICATION_VERSION}",str([tuple(row) for row in plan]))
        with patch("packet_inspector.storage.zlib.decompress",side_effect=AssertionError("already analyzed")):
            self.assertEqual(s.reclassify_pending(),0)
        s.update(pid,response_bytes=456)
        self.assertEqual(s.reclassify_pending(),0)
        self.assertTrue(s.get(pid)[0]["is_compaction"])

    def test_malformed_legacy_copy_does_not_block_other_classification(self):
        s=Store(self.path)
        pid=s.add({"method":"POST"})
        with s.connect() as c:
            c.execute("UPDATE packets SET content=? WHERE id=?",(b"invalid zlib",pid))
        self.assertEqual(s.reclassify_pending(),1)
        self.assertIn("classification_error",s.get(pid)[0])
        self.assertEqual(s.reclassify_pending(),0)

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)

    def test_null_body_metadata_is_bounded_and_newest_records_survive(self):
        s=Store(self.path,max_count=2,max_bytes=1000000)
        live=s.add({"wire_bytes":0},None)
        recent=[]
        for _ in range(4):
            p=s.add({"wire_bytes":0},None);s.update(p,state="complete",status=200);recent.append(p)
        self.assertEqual(s.stats()["count"],2)
        self.assertGreater(s.stats()["stored"],0)
        with self.assertRaises(KeyError):
            s.get(live)
        self.assertCountEqual([p["id"] for p in s.list()],recent[-2:])
        reopened=Store(self.path,max_count=1)
        self.assertEqual(reopened.stats()["count"],1)
        self.assertEqual(reopened.list()[0]["id"],recent[-1])

    def test_count_limit_evicts_oldest_request_in_every_state(self):
        for state in ("captured","streaming","complete","client_disconnected","relay_error","interrupted"):
            with self.subTest(state=state):
                store=Store(self.path/state,max_count=1)
                with patch("packet_inspector.storage.time.time",return_value=1):
                    old=store.add({"method":"POST","thread_id":"old"})
                store.update(old,state=state)
                with patch("packet_inspector.storage.time.time",return_value=2):
                    recent=store.add({"method":"POST","thread_id":"recent"})
                self.assertEqual(store.stats()["count"],1)
                self.assertEqual(store.list()[0]["id"],recent)
                with self.assertRaises(KeyError):
                    store.get(old)
                # A response finishing after eviction must not recreate its record.
                store.update(old,state="complete",status=200,response_bytes=42)
                self.assertEqual(store.list()[0]["id"],recent)
                self.assertEqual(store.filter_options()["threads"],["recent"])

    def test_byte_limit_evicts_streaming_request_before_newer_completed_request(self):
        store=Store(self.path,max_bytes=5000)
        old=store.add({"method":"POST","note":"old"*1000})
        store.update(old,state="streaming")
        recent=store.add({"method":"POST","note":"new"*1000})
        store.update(recent,state="complete")
        self.assertEqual(store.stats()["count"],1)
        self.assertLessEqual(store.stats()["stored"],5000)
        self.assertEqual(store.list()[0]["id"],recent)
        with self.assertRaises(KeyError):
            store.get(old)

    def test_single_oversized_request_cannot_bypass_storage_limit(self):
        store=Store(self.path,max_bytes=500)
        packet=store.add({"method":"POST","note":"large"*1000})
        store.update(packet,state="complete",status=200)
        self.assertEqual(store.stats()["count"],0)
        self.assertEqual(store.stats()["stored"],0)

    def test_restart_recovers_own_and_retired_observer_but_not_another_relay(self):
        store=Store(self.path)
        own=store.add({"method":"POST"})
        retired_pending=store.add({"method":"POST"})
        store.update(retired_pending,capture_owner="existing_exit")
        retired=store.add({"method":"POST"})
        store.update(retired,capture_owner="existing_exit",state="streaming",status=200)
        finished=store.add({"method":"POST"})
        store.update(finished,capture_owner="existing_exit",state="complete",status=200)
        other=store.add({"method":"POST"})
        store.update(other,capture_owner="relay:"+"a"*32,state="streaming")
        self.assertEqual(store.stats()["observed_active"],2)
        reopened=Store(self.path)
        self.assertEqual(reopened.get(own)[0]["state"],"interrupted")
        self.assertEqual(reopened.get(retired_pending)[0]["state"],"interrupted")
        self.assertEqual(reopened.get(retired)[0]["state"],"interrupted")
        self.assertEqual(reopened.get(retired)[0]["status"],200)
        self.assertEqual(reopened.get(finished)[0]["state"],"complete")
        self.assertEqual(reopened.get(other)[0]["state"],"streaming")
        self.assertEqual(reopened.stats()["observed_active"],0)

    def test_comparison_requires_identified_same_chat(self):
        s=Store(self.path)
        a=s.add({"thread_id":"a","wire_bytes":10});s.update(a,state="complete")
        b=s.add({"thread_id":"b","wire_bytes":20});s.update(b,state="complete")
        c=s.add({"thread_id":"a","wire_bytes":15})
        self.assertEqual(s.compare(c)["previous_id"],a)
        self.assertIsNone(s.compare(s.add({"wire_bytes":30})))

    def test_get_filter_precedes_limit_and_preserves_history(self):
        s=Store(self.path)
        first=s.add({"method":"POST","thread_id":"filter","wire_bytes":100})
        second=s.add({"method":"POST","thread_id":"filter","wire_bytes":200})
        for method in ["GET","get","GET"]:
            s.add({"method":method,"thread_id":"filter","wire_bytes":0})
        rows=s.list(limit=1,thread="filter")
        self.assertEqual([r["id"] for r in rows],[second])
        self.assertEqual([r["id"] for r in s.list(limit=1,thread="filter",before=rows[0]["created"])],[first])
        self.assertEqual(s.list(limit=1,thread="filter",include_get=True)[0]["method"],"GET")
        self.assertEqual(len(s.list(thread="filter",include_get=True)),5)
        self.assertEqual(s.stats()["count"],5)

    def test_numeric_sort_paginates_all_matching_rows_without_duplicates(self):
        s=Store(self.path)
        for size in [90,1000,500,1000,None]:
            s.add({"method":"POST","thread_id":"sorted","wire_bytes":size})
        s.add({"method":"GET","thread_id":"sorted","wire_bytes":99999})
        s.add({"method":"POST","thread_id":"other","wire_bytes":99999})
        for direction,expected in [("asc",[-1,90,500,1000,1000]),("desc",[1000,1000,500,90,-1])]:
            with self.subTest(direction=direction):
                rows=[];after=None
                for _ in range(4):
                    page=s.list(limit=2,thread="sorted",sort="wire_bytes",direction=direction,after=after)
                    rows.extend(page)
                    if not page:break
                    after=page[-1]["id"]
                self.assertEqual([r["wire_bytes"] if r["wire_bytes"] is not None else -1 for r in rows],expected)
                self.assertEqual(len({r["id"] for r in rows}),5)


    def test_combined_filters_precede_limit_and_survive_pagination(self):
        s=Store(self.path)
        expected=[]
        for size in [500,200,500]:
            expected.append(s.add({"method":"POST","thread_id":"target","is_compaction":True,
                                   "status":429,"wire_bytes":size}))
        distractors=[{"thread_id":"other"},{"status":200},{"is_compaction":False},{"method":"GET"}]
        for i in range(105):
            s.add({"method":"POST","thread_id":"target","is_compaction":True,
                   "status":429,"wire_bytes":9000,**distractors[i%len(distractors)]})
        for direction,sizes in [("asc",[200,500,500]),("desc",[500,500,200])]:
            with self.subTest(direction=direction):
                rows=[];after=None
                for _ in range(3):
                    page=s.list(limit=2,kind="compaction",status="http:429",thread="target",
                                sort="wire_bytes",direction=direction,after=after)
                    rows.extend(page)
                    if not page:break
                    after=page[-1]["id"]
                self.assertCountEqual([r["id"] for r in rows],expected)
                self.assertEqual([r["wire_bytes"] for r in rows],sizes)

    def test_upload_sort_paginates_final_sizes_without_falling_back_to_input(self):
        s=Store(self.path)
        for entry,upload in [(900,100),(800,300),(700,300),(600,0),(2000000,None)]:
            s.add({"method":"POST","thread_id":"uploads","wire_bytes":entry,
                   **({"outbound_wire_bytes":upload} if upload is not None else {})})
        s.add({"method":"GET","outbound_wire_bytes":9999999})
        for direction,expected in [("asc",[None,0,100,300,300]),("desc",[300,300,100,0,None])]:
            with self.subTest(direction=direction):
                rows=[];after=None
                for _ in range(4):
                    page=s.list(limit=2,sort="upload_bytes",direction=direction,after=after)
                    rows.extend(page)
                    if not page:break
                    after=page[-1]["id"]
                self.assertEqual([r.get("outbound_wire_bytes") for r in rows],expected)
                self.assertEqual(len({r["id"] for r in rows}),5)

    def test_older_observer_upload_updates_change_index_and_order(self):
        s=Store(self.path)
        a=s.add({"method":"POST","wire_bytes":1100000,"outbound_wire_bytes":400000})
        b=s.add({"method":"POST","wire_bytes":1000000,"outbound_wire_bytes":500000})
        self.assertEqual([r["id"] for r in s.list(sort="upload_bytes")],[b,a])
        meta=s.get(a)[0];meta.update(outbound_wire_bytes=600000,outbound_content_encoding="zstd")
        revision=s.stats()["revision"]
        with s.connect() as c:
            c.execute("UPDATE packets SET meta=? WHERE id=?",(json.dumps(meta),a))
        self.assertEqual([r["id"] for r in s.list(sort="upload_bytes")],[a,b])
        self.assertGreater(s.stats()["revision"],revision)

    def test_compressed_size_sort_and_cursor_use_pre_summary_bytes(self):
        store = Store(self.path)
        cases = [
            ({}, -1),
            ({"outbound_wire_bytes": 0}, 0),
            ({"outbound_wire_bytes": 200000}, 200000),
            ({"usage_override_request_bytes": 950000, "outbound_wire_bytes": 100000}, 950000),
            ({"overflow_original_wire_bytes": 1100000, "usage_override_request_bytes": 950000,
              "outbound_wire_bytes": 50000}, 1100000),
        ]
        entries = [(store.add({"method": "POST", **meta}), size) for meta, size in cases]
        for direction in ("asc", "desc"):
            found, after = [], None
            while True:
                page = store.list(limit=2, sort="compressed_bytes", direction=direction, after=after)
                found.extend(packet["id"] for packet in page)
                if len(page) < 2:
                    break
                after = page[-1]["id"]
            expected = [packet_id for packet_id, _ in sorted(entries, key=lambda row: row[1],
                                                            reverse=direction == "desc")]
            self.assertEqual(found, expected)

    def test_filter_options_cover_history_and_unidentified_threads(self):
        s=Store(self.path)
        old=s.add({"method":"POST","thread_id":"old","is_compaction":True,"status":413})
        for _ in range(151):
            s.add({"method":"POST","thread_id":"recent","status":200})
        pending=s.add({"method":"PATCH"})
        s.update(pending,state="streaming")
        s.add({"method":"get","thread_id":"get-only","status":599})
        self.assertNotIn(old,[r["id"] for r in s.list(limit=100)])
        options=s.filter_options()
        self.assertCountEqual(options["kinds"],["generation","compaction","other"])
        self.assertCountEqual(options["statuses"],["http:413","http:200","state:streaming"])
        self.assertCountEqual(options["threads"],["old","recent",""])
        rows=s.list(thread_unknown=True,status="state:streaming",kind="other")
        self.assertEqual([r["id"] for r in rows],[pending])

    def test_legacy_database_migration_keeps_complete_details(self):
        meta={"id":"legacy","created":1,"method":"POST","thread_id":"legacy-chat",
              "state":"complete","status":200,"wire_bytes":500000,
              "input_items":[{"label":"large detail"*10000}],"headers":{"x":"retained"},
              "top_fields":[{"name":"input","bytes":400000}],"input_groups":{"message":400000}}
        raw=json.dumps(meta,ensure_ascii=False)
        with closing(sqlite3.connect(self.path/"packets.sqlite")) as c, c:
            c.execute("CREATE TABLE packets (id TEXT PRIMARY KEY,created REAL NOT NULL,thread TEXT NOT NULL,status INTEGER,fingerprint TEXT,meta TEXT NOT NULL,content BLOB)")
            c.execute("INSERT INTO packets VALUES(?,?,?,?,?,?,?)",("legacy",1,"legacy-chat",200,"",raw,None))
        s=Store(self.path)
        self.assertEqual(s.get("legacy")[0],meta)
        self.assertEqual(s.list()[0],{k:v for k,v in meta.items() if k not in {"input_items","headers","top_fields","input_groups"}})
        self.assertEqual(s.stats()["stored"],len(raw.encode("utf-8")))
        revision=s.stats()["revision"]
        reopened=Store(self.path)
        self.assertEqual(reopened.stats()["revision"],revision)
        self.assertEqual(reopened.get("legacy")[0],meta)

    def test_older_writer_keeps_summaries_stats_and_revision_in_sync(self):
        s=Store(self.path)
        revision=s.stats()["revision"]
        meta={"id":"external","created":2,"method":"POST","thread_id":"a",
              "wire_bytes":100,"state":"captured","capture_owner":"existing_exit"}
        with s.connect() as c:
            # Older observers insert all seven values without naming columns.
            c.execute("INSERT INTO packets VALUES(?,?,?,?,?,?,?)",("external",2,"a",None,"",json.dumps(meta),None))
        self.assertEqual(s.stats()["observed_active"],1)
        self.assertGreater(s.stats()["revision"],revision)
        self.assertEqual(s.list(kind="generation")[0]["wire_bytes"],100)
        meta.update(thread_id="b",wire_bytes=200,state="complete",status=429,is_compaction=True)
        with s.connect() as c:
            c.execute("UPDATE packets SET meta=?,status=?,thread=? WHERE id=?",(json.dumps(meta),429,"b","external"))
        self.assertEqual(s.list(kind="compaction",status="http:429",thread="b")[0],meta)
        self.assertEqual(s.filter_options(),{"kinds":["compaction"],"statuses":["http:429"],"threads":["b"]})
        self.assertEqual(s.stats()["observed_active"],0)
        self.assertEqual(s.stats()["stored"],len(json.dumps(meta).encode("utf-8")))
        revision=s.stats()["revision"]
        with s.connect() as c:
            c.execute("DELETE FROM packets WHERE id=?",("external",))
        self.assertEqual(s.list(),[])
        self.assertEqual(s.stats()["count"],0)
        self.assertGreater(s.stats()["revision"],revision)

    def test_list_reads_only_summary_and_sort_uses_indexes(self):
        s=Store(self.path)
        packet=s.add({"method":"POST","wire_bytes":123,"input_items":[{"label":"large"*30000}]})
        original_loads=json.loads
        with patch("packet_inspector.storage.json.loads",wraps=original_loads) as loads:
            self.assertEqual(s.list()[0]["id"],packet)
            s.filter_options();s.stats()
        self.assertTrue(all(len(call.args[0])<2000 for call in loads.call_args_list))
        with s.connect() as c:
            for ordering,index in [("created DESC,id DESC","packet_summary_created"),
                                   ("wire_bytes DESC,created DESC,id DESC","packet_summary_size"),
                                   ("coalesce(json_extract(meta,'$.outbound_wire_bytes'),-1) DESC,created DESC,id DESC","packet_summary_upload_size")]:
                plan=" ".join(r["detail"] for r in c.execute("EXPLAIN QUERY PLAN SELECT meta FROM packet_summaries WHERE method!='GET' ORDER BY "+ordering+" LIMIT 100"))
                self.assertIn(index,plan)
                self.assertNotIn("TEMP B-TREE",plan)


class Upstream(BaseHTTPRequestHandler):
    protocol_version="HTTP/1.1"

    def log_message(self,*_):
        pass

    def do_POST(self):
        body=self.rfile.read(int(self.headers.get("Content-Length","0")))
        self.server.requests.append({"path":self.path,"body":body,"headers":dict(self.headers)})
        if self.path.endswith("/stream"):
            self.send_response(200);self.send_header("Content-Type","text/event-stream");self.send_header("Connection","close");self.end_headers()
            self.wfile.write(b"data: first\n\n");self.wfile.flush();self.server.first.set()
            self.server.release.wait(5)
            self.wfile.write(b"data: last\n\n");self.wfile.flush();self.close_connection=True
            return
        status=403 if "/reject" in self.path else 302 if "/redirect" in self.path else 200
        payload=b'{"ok":true}'
        self.send_response(status)
        if status==302:self.send_header("Location","/must-not-follow")
        self.send_header("Content-Length",str(len(payload)));self.send_header("Content-Type","application/json");self.end_headers();self.wfile.write(payload)


class RelayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        repair = patch("packet_inspector.server.repair_loopback_proxy", return_value={"state": "ok", "changed": False})
        repair.start()
        cls.addClassCleanup(repair.stop)
        cls.temp=tempfile.TemporaryDirectory();root=Path(cls.temp.name)
        cls.up=ThreadingHTTPServer(("127.0.0.1",0),Upstream);cls.up.daemon_threads=False
        cls.up.requests=[];cls.up.first=threading.Event();cls.up.release=threading.Event()
        cls.config=root/"config.toml";cls.config.write_text('model="test"',encoding="utf8")
        cls.server=InspectorServer(("127.0.0.1",0),root/"data",cls.config,"http://127.0.0.1:"+str(cls.up.server_port)+"/v1")
        cls.server.daemon_threads=False
        cls.server.settings["auto_detect_providers"]=False
        cls.server.settings["packet_size_protection_enabled"]=False
        for s in [cls.up,cls.server]:threading.Thread(target=s.serve_forever,daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.up.release.set()
        cls.server.shutdown()
        try:
            wait_for_idle(cls.server)
        finally:
            cls.server.server_close()
            cls.server.analysis_thread.join(timeout=HTTP_TIMEOUT)
            cls.up.shutdown()
            cls.up.server_close()
        cls.temp.cleanup()

    def relay(self,raw,path="/responses",headers=None,chunked=False,*,timeout=HTTP_TIMEOUT):
        c=http.client.HTTPConnection("127.0.0.1",self.server.port,timeout=timeout)
        try:
            h={"Content-Type":"application/json","session_id":"test-thread",**(headers or {})}
            if chunked:h["Transfer-Encoding"]="chunked"
            c.request("POST",self.server.endpoint.split(str(self.server.port),1)[1]+path,[raw[:4],raw[4:]] if chunked else raw,h,encode_chunked=chunked)
            r=c.getresponse()
            result = r.status, r.read()
            wait_for_idle(self.server)
            return result
        finally:
            c.close()

    def api(self,path,data=None,extra=None):
        c=http.client.HTTPConnection("127.0.0.1",self.server.port,timeout=5)
        headers={}
        headers.update(extra or {})
        c.request("POST" if data is not None else "GET",path,encode(data) if data is not None else None,headers)
        r=c.getresponse();result=r.read();status=r.status;c.close();return status,json.loads(result)

    def latest(self):
        packet=self.server.store.list(limit=1)[0]
        for _ in range(100):
            m,body=self.server.store.get(packet["id"],True)
            if m["state"] not in {"captured","streaming"}:return m,body
            time.sleep(.005)
        self.fail("relay did not finish")

    def test_plaintext_bytes_and_auth_forward_without_auth_storage(self):
        raw=b' { "input": [{"role":"user","content":[{"type":"input_image","image_url":"https://invalid.example/a.png"}]}], "metadata":{"access_token":"body-secret-do-not-store"} } '
        self.assertEqual(self.relay(raw,headers={"Authorization":"Bearer auth-never-store","X-Secret":"extra-never-store"})[0],200)
        self.assertEqual(self.up.requests[-1]["body"],raw)
        self.assertEqual(self.up.requests[-1]["headers"]["Authorization"],"Bearer auth-never-store")
        m,v=self.latest();self.assertEqual(m["wire_bytes"],len(raw))
        with self.server.store.connect() as c:
            all_rows=c.execute("SELECT meta,content FROM packets").fetchall()
        for r in all_rows:
            self.assertNotIn("auth-never-store",r["meta"])
            self.assertNotIn("extra-never-store",r["meta"])
        self.assertNotIn(b"body-secret-do-not-store",encode(v))

    def test_hot_compression_covers_generation_and_compaction_without_rewriting(self):
        previous=self.server.settings["auto_detect_providers"]
        try:
            status,result=self.api("/api/link",{"action":"activate"})
            self.assertEqual(status,200)
            for compact in [False,True]:
                raw=encode({"input":[{"role":"user","content":"retained content "*400}]+([{"type":"compaction_trigger"}] if compact else [])})
                self.assertEqual(self.relay(raw)[0],200)
                forwarded=self.up.requests[-1]
                self.assertEqual(forwarded["headers"]["Content-Encoding"],"zstd")
                self.assertEqual(zstandard.ZstdDecompressor().decompress(forwarded["body"]),raw)
                meta,_=self.latest()
                self.assertEqual(meta["relay_wire_bytes"],len(forwarded["body"]))
                self.assertEqual(meta["is_compaction"],compact)
            self.assertEqual(self.api("/api/link",{"action":"restore"})[0],200)
            self.relay(raw)
            self.assertEqual(self.up.requests[-1]["body"],raw)
            self.assertEqual(self.api("/api/settings",{"request_compression":"invalid"})[0],400)
            self.assertEqual(self.server.status()["connection_state"],"disconnected")
        finally:
            self.api("/api/link",{"action":"activate" if previous else "restore"})

    def test_obsolete_compression_modes_cannot_create_a_third_backend_state(self):
        before = self.config.read_bytes()
        for mode in ("off", "zstd", "invalid"):
            self.assertEqual(self.api("/api/settings", {"request_compression": mode})[0], 400)
            self.assertNotIn("request_compression", self.server.settings)
        self.assertEqual(self.config.read_bytes(), before)
        self.assertIn(self.server.status()["connection_state"], {"connected", "disconnected"})

    def test_zstd_level_settings_validate_persist_and_apply_to_existing_zstd(self):
        previous = {"zstd_level": self.server.settings["zstd_level"]}
        previous_connection = self.server.settings["auto_detect_providers"]
        self.api("/api/link", {"action": "activate"})
        try:
            status, result = self.api("/api/settings", {"zstd_level": 22})
            self.assertEqual(status, 200)
            self.assertEqual(result["zstd_level"], 22)
            self.assertEqual(json.loads(self.server.settings_path.read_text(encoding="utf8"))["zstd_level"], 22)
            for value in (0, 23, -1, 1.5, True, "22", None):
                with self.subTest(invalid=value):
                    self.assertEqual(self.api("/api/settings", {"zstd_level": value})[0], 400)
                    self.assertEqual(self.server.settings["zstd_level"], 22)
            raw = encode({"input": [{"role": "user", "content": "unchanged request semantics " * 1500}]})
            incoming = zstandard.ZstdCompressor(level=1).compress(raw)
            with patch("zstandard.ZstdCompressor", wraps=zstandard.ZstdCompressor) as compressor:
                self.assertEqual(self.relay(incoming, headers={"Content-Encoding": "zstd"})[0], 200)
                compressor.assert_any_call(level=22)
            forwarded = self.up.requests[-1]
            self.assertEqual(zstandard.ZstdDecompressor().decompress(forwarded["body"]), raw)
            self.assertLessEqual(len(forwarded["body"]), len(incoming))
        finally:
            self.api("/api/settings", previous)
            self.api("/api/link", {"action": "activate" if previous_connection else "restore"})

    def test_applied_zstd_level_is_recorded_per_request_without_guessing_passthrough(self):
        previous = {"zstd_level": self.server.settings["zstd_level"]}
        previous_connection = self.server.settings["auto_detect_providers"]
        self.api("/api/link", {"action": "activate"})
        raw = encode({"input": "retained request content " * 1500})
        try:
            self.assertEqual(self.api("/api/settings", {"zstd_level": 7})[0], 200)
            self.assertEqual(self.relay(raw)[0], 200)
            first, _ = self.latest()
            self.assertEqual(first["relay_zstd_level"], 7)
            self.assertEqual(self.up.requests[-1]["body"], zstandard.ZstdCompressor(level=7).compress(raw))

            self.assertEqual(self.api("/api/settings", {"zstd_level": 16})[0], 200)
            self.assertEqual(self.relay(raw)[0], 200)
            latest, _ = self.latest()
            self.assertEqual(latest["relay_zstd_level"], 16)
            self.assertEqual(self.server.store.get(first["id"])[0]["relay_zstd_level"], 7)

            for body, headers, path in ((b"{}", {}, "/responses"),
                                        (gzip.compress(raw), {"Content-Encoding": "gzip"}, "/responses"),
                                        (raw, {}, "/alpha/search")):
                with self.subTest(path=path, encoding=headers.get("Content-Encoding", "identity")):
                    self.assertEqual(self.relay(body, path, headers)[0], 200)
                    self.assertIsNone(self.latest()[0]["relay_zstd_level"])
                    self.assertEqual(self.up.requests[-1]["body"], body)

            self.assertEqual(self.api("/api/link", {"action": "restore"})[0], 200)
            incoming = zstandard.ZstdCompressor(level=7).compress(raw)
            self.assertEqual(self.relay(incoming, headers={"Content-Encoding": "zstd"})[0], 200)
            self.assertIsNone(self.latest()[0]["relay_zstd_level"])
            self.assertEqual(self.up.requests[-1]["body"], incoming)
        finally:
            self.api("/api/settings", previous)
            self.api("/api/link", {"action": "activate" if previous_connection else "restore"})

    def test_installation_api_cannot_modify_login_tasks_from_disposable_instances(self):
        status,result=self.api("/api/installation")
        self.assertEqual(status,200)
        self.assertFalse(result["supported"])
        self.assertEqual(self.api("/api/installation",{"action":"Install"})[0],400)

    def test_encoded_requests_and_chunked_framing(self):
        raw=encode({"input":[{"role":"user","content":"hello"*200}]})
        for enc,body in [("gzip",gzip.compress(raw)),("zstd",zstandard.ZstdCompressor().compress(raw))]:
            with self.subTest(encoding=enc):
                self.assertEqual(self.relay(body,headers={"Content-Encoding":enc},chunked=True)[0],200)
                self.assertEqual(self.up.requests[-1]["body"],body)
                self.assertEqual(self.up.requests[-1]["headers"]["Content-Encoding"],enc)
                m,_=self.latest();self.assertEqual(m["decoded_bytes"],len(raw))

    def test_local_exit_does_not_mislabel_input_as_final_upload(self):
        raw=encode({"input":"local exit may compress this"})
        self.assertEqual(self.relay(raw)[0],200)
        m,_=self.latest()
        self.assertEqual(m["wire_bytes"],len(raw))
        self.assertNotIn("outbound_wire_bytes",m)

    def test_direct_upload_records_exact_final_bytes_and_encoding(self):
        raw=encode({"input":"repeated content "*500})
        def send_to_test_upstream(request,timeout):
            self.assertTrue(request.full_url.startswith("https://remote.invalid/v1/"))
            forwarded=Request("http://127.0.0.1:"+str(self.up.server_port)+"/v1/responses",
                              data=request.data,headers=dict(request.header_items()),method=request.method)
            return self.server.local_opener.open(forwarded,timeout=timeout)
        with patch.dict(self.server.settings,{"upstream":"https://remote.invalid/v1"}), \
                patch.object(self.server.opener,"open",side_effect=send_to_test_upstream):
            for encoding,body in [("identity",raw),("zstd",zstandard.ZstdCompressor().compress(raw))]:
                with self.subTest(encoding=encoding):
                    self.assertEqual(self.relay(body,headers={"Content-Encoding":encoding})[0],200)
                    m,_=self.latest()
                    self.assertEqual(m["outbound_wire_bytes"],len(body))
                    self.assertEqual(m["outbound_content_encoding"],encoding)
                    self.assertEqual(self.up.requests[-1]["body"],body)

    def test_failure_and_redirect_do_not_retry(self):
        for path,status in [("/reject",403),("/redirect",302)]:
            before=len(self.up.requests)
            self.assertEqual(self.relay(b"{}",path)[0],status)
            self.assertEqual(len(self.up.requests),before+1)
            self.assertEqual(self.latest()[0]["status"],status)

    def test_stream_first_event_arrives_before_upstream_finishes(self):
        c=http.client.HTTPConnection("127.0.0.1",self.server.port,timeout=3)
        path=self.server.endpoint.split(str(self.server.port),1)[1]+"/stream"
        try:
            c.request("POST",path,b"{}",{"Content-Type":"application/json"})
            r=c.getresponse()
            self.assertEqual(r.readline(),b"data: first\n")
            self.assertFalse(self.up.release.is_set())
            self.up.release.set()
            self.assertIn(b"data: last",r.read())
        finally:
            self.up.release.set();c.close()

    def test_capture_pause_and_unparseable_request_still_forward(self):
        self.server.settings["capture_enabled"]=False
        before=self.server.store.stats()["count"]
        try:self.assertEqual(self.relay(b"opaque request")[0],200)
        finally:self.server.settings["capture_enabled"]=True
        self.assertEqual(self.server.store.stats()["count"],before)
        self.assertEqual(self.relay(b"opaque request")[0],200)
        self.assertEqual(self.latest()[0]["inspection"],"unavailable")
        self.assertEqual(self.up.requests[-1]["body"],b"opaque request")

    def test_direct_local_ui_and_atomic_setting_validation(self):
        self.assertEqual(self.api("/api/status")[0],200)
        self.assertEqual(self.api("/api/packets")[0],200)
        self.assertEqual(self.api("/api/status",extra={"X-Inspector-Token":"stale"})[0],200)
        self.assertEqual(self.api("/api/status",extra={"Origin":"https://external.example"})[0],403)
        self.assertEqual(self.api("/api/status",extra={"Host":"untrusted.example"})[0],403)
        before=self.server.settings["capture_enabled"]
        status,_=self.api("/api/settings",{"capture_enabled":not before,"max_packets":-1})
        self.assertEqual(status,400)
        self.assertEqual(self.server.settings["capture_enabled"],before)

    def test_compact_endpoint_is_marked(self):
        self.relay(b"{}","/responses/compact")
        self.assertTrue(self.latest()[0]["is_compaction"])

    def test_context_settings_api_saves_validated_config_and_restores_defaults(self):
        original=self.config.read_bytes()
        try:
            status,info=self.api("/api/context-settings")
            self.assertEqual(status,200)
            self.assertIsNone(info["context_tokens"])
            status,result=self.api("/api/context-settings",{"context_tokens":240000,"compact_tokens":220000})
            self.assertEqual(status,200)
            self.assertEqual(result["context_tokens"],240000)
            self.assertTrue(Path(result["backup"]).is_file())
            self.assertEqual(self.api("/api/context-settings")[1]["compact_tokens"],220000)
            saved=self.config.read_bytes()
            self.assertEqual(self.api("/api/context-settings",{"context_tokens":200000,"compact_tokens":220000})[0],400)
            self.assertEqual(self.config.read_bytes(),saved)
            self.assertEqual(self.api("/api/context-settings",[])[0],400)
            self.assertEqual(self.api("/api/context-settings",{"context_tokens":None,"compact_tokens":None})[0],200)
            self.assertEqual(tomllib.loads(self.config.read_text()),tomllib.loads(original.decode()))
        finally:
            self.config.write_bytes(original)

    def test_existing_listener_keeps_original_bind_error(self):
        with patch.object(InspectorServer,"server_bind",side_effect=OSError("address busy")), self.assertRaisesRegex(OSError,"address busy"):
            InspectorServer(("127.0.0.1",self.server.port),Path(self.temp.name)/"duplicate",self.config,
                            "http://127.0.0.1:"+str(self.up.server_port)+"/v1")

    def test_old_exit_new_record_is_classified_without_restart(self):
        raw=encode({"client_metadata":{"x-codex-turn-metadata":'{"request_kind":"compaction"}'}})
        meta,content=inspect_request(raw,{})
        for key in ("classification_version","request_kind","compaction_reason"):
            meta.pop(key,None)
        meta.update(is_compaction=False,method="POST",path="/responses")
        pid=self.server.store.add(meta,content)
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            if self.server.store.get(pid)[0]["is_compaction"]:
                break
            time.sleep(.05)
        self.assertTrue(self.server.store.get(pid)[0]["is_compaction"])
        self.assertEqual(self.server.analysis_errors,0)

    def test_packet_api_hides_get_until_requested(self):
        self.server.store.add({"method":"POST","thread_id":"get-filter","wire_bytes":123})
        self.server.store.add({"method":"GET","thread_id":"get-filter","wire_bytes":0})
        status,rows=self.api("/api/packets?thread=get-filter&limit=1")
        self.assertEqual(status,200)
        self.assertEqual([r["method"] for r in rows],["POST"])
        status,rows=self.api("/api/packets?thread=get-filter&include_get=1")
        self.assertEqual(status,200)
        self.assertEqual([r["method"] for r in rows],["GET","POST"])

    def test_packet_sort_api_validates_fields_and_keeps_page_order(self):
        for status in [500,200,429]:
            self.server.store.add({"method":"POST","thread_id":"sorted-api","status":status})
        prefix="/api/packets?thread=sorted-api&sort=status&direction=asc&limit=1"
        status,rows=self.api(prefix)
        self.assertEqual(status,200)
        self.assertEqual(rows[0]["status"],200)
        status,rows=self.api(prefix+"&after="+rows[0]["id"])
        self.assertEqual(status,200)
        self.assertEqual(rows[0]["status"],429)
        for field in ["created","kind","wire_bytes","upload_bytes","thread"]:
            self.assertEqual(self.api("/api/packets?sort="+field)[0],200)
        self.assertEqual(self.api("/api/packets?sort=wire_bytes%20DESC")[0],400)
        self.assertEqual(self.api("/api/packets?direction=invalid")[0],400)

    def test_packet_filter_api_combines_filters_and_rejects_invalid_values(self):
        store=self.server.store
        selected=store.add({"method":"POST","thread_id":"filter-api","is_compaction":True,"status":413})
        store.add({"method":"POST","thread_id":"filter-api","is_compaction":False,"status":413})
        store.add({"method":"POST","thread_id":"filter-api","is_compaction":True,"status":200})
        unidentified=store.add({"method":"POST"})
        store.update(unidentified,state="relay_error")
        status,rows=self.api("/api/packets?thread=filter-api&kind=compaction&status=http:413&limit=1")
        self.assertEqual(status,200)
        self.assertEqual([r["id"] for r in rows],[selected])
        status,rows=self.api("/api/packets?thread_unknown=1&status=state:relay_error")
        self.assertEqual(status,200)
        self.assertIn(unidentified,[r["id"] for r in rows])
        status,options=self.api("/api/packet-filters")
        self.assertEqual(status,200)
        self.assertIn("compaction",options["kinds"])
        self.assertIn("http:413",options["statuses"])
        self.assertIn("filter-api",options["threads"])
        for query in ["kind=unknown","status=413","status=http:999","status=state:bad%27",
                      "thread=filter-api&thread_unknown=1"]:
            with self.subTest(query=query):
                self.assertEqual(self.api("/api/packets?"+query)[0],400)

    def test_concurrent_requests_remain_separate(self):
        bodies=[encode({"input":"parallel-"+str(i)}) for i in range(8)]
        # Check isolation, with a bounded I/O guard rather than a CI latency limit.
        # Futures also surface every worker exception in the test thread.
        with ThreadPoolExecutor(max_workers=len(bodies)) as pool:
            futures=[pool.submit(self.relay,body,timeout=30) for body in bodies]
            results=[future.result(timeout=35)[0] for future in futures]
        self.assertEqual(results,[200]*8)
        received=[r["body"] for r in self.up.requests[-8:]]
        self.assertCountEqual(received,bodies)


if __name__=="__main__":
    unittest.main()
