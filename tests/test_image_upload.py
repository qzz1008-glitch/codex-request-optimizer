"""Budgeted image resizing, fixed zstd levels and relay integration."""
import base64
from functools import lru_cache
from io import BytesIO
import json
from pathlib import Path
import random
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image
import zstandard

from packet_inspector.analyzer import decode_body
from packet_inspector.image_upload import optimize_image_request
from packet_inspector.server import InspectorServer
from packet_inspector.transport import AUTO_IMAGE_ZSTD_LEVEL, PreparedRequest, prepare_request_info
from tests import test_packet_inspector as fixtures

HEADERS = {"Content-Type":"application/json"}
PATH = "/images/edits"
BUDGET = 1_000_000


@lru_cache()
def image_uri(fmt="PNG", alpha=False, mask=False):
    if mask:
        image = Image.new("RGBA", (900, 800), (255, 255, 255, 0))
    else:
        image = Image.frombytes("RGB", (900, 800), random.Random(1030).randbytes(900*800*3))
        if alpha:
            image.putalpha(128)
    buffer = BytesIO()
    image.save(buffer, format=fmt, **({"quality":90} if fmt in {"JPEG", "WEBP"} else {}))
    image.close()
    return "data:image/"+fmt.lower()+";base64,"+base64.b64encode(buffer.getvalue()).decode("ascii")


def body(*, alpha=False, mask=False, fmt="PNG"):
    images = [{"image_url":image_uri(fmt, alpha)}]
    fields = {"images":images}
    if mask:
        fields["mask"] = {"image_url":image_uri(mask=True)}
    return b' { "quality" : "high", "size":"1536x1024", "prompt":"unchanged", '+json.dumps(fields,separators=(",", ":")).encode()[1:-1]+b' }\n'


def decoded_image(request_body, pointer="image"):
    value = json.loads(request_body)
    uri = value["images"][0]["image_url"] if pointer == "image" else value["mask"]["image_url"]
    return Image.open(BytesIO(base64.b64decode(uri.split(",",1)[1])))


class ImageUploadTests(unittest.TestCase):
    def resize(self, raw, level=6, headers=None, prepared=None):
        headers = headers or HEADERS
        prepared = prepared or prepare_request_info(raw, headers, "POST", PATH, "zstd", level)
        return optimize_image_request(raw, headers, PATH, prepared, budget_bytes=BUDGET, zstd_level=level)

    def test_manual_level_stays_fixed_before_and_after_resizing(self):
        raw = body()
        for level in (1, 6, 16, 22):
            with self.subTest(level=level), patch("zstandard.ZstdCompressor", wraps=zstandard.ZstdCompressor) as compressor:
                result, metrics = self.resize(raw, level)
                self.assertTrue(metrics["image_upload_optimized"])
                self.assertLessEqual(metrics["image_upload_iterations"], 2)
                self.assertLessEqual(len(result.body), BUDGET)
                self.assertEqual(result.zstd_level, level)
                self.assertEqual({call.kwargs["level"] for call in compressor.call_args_list}, {level})
                decoded = decode_body(result.body, result.encoding)
                self.assertTrue(decoded.startswith(b' { "quality" : "high", "size":"1536x1024", "prompt":"unchanged", '))
                self.assertTrue(decoded.endswith(b' }\n'))
                with decoded_image(decoded) as resized:
                    self.assertLess(resized.width, 900)
                    self.assertLess(resized.height, 800)
                self.assertEqual(json.loads(raw)["prompt"], json.loads(decoded)["prompt"])

    def test_smart_image_level_is_selected_offline_and_never_probes_other_levels(self):
        raw = body()
        with patch("zstandard.ZstdCompressor", wraps=zstandard.ZstdCompressor) as compressor:
            prepared = prepare_request_info(raw, HEADERS, "POST", PATH, "zstd", 22,
                                            auto_zstd_level=True, budget_bytes=BUDGET)
            result, metrics = self.resize(raw, AUTO_IMAGE_ZSTD_LEVEL, prepared=prepared)
        self.assertEqual(AUTO_IMAGE_ZSTD_LEVEL, 6)
        self.assertEqual({call.kwargs["level"] for call in compressor.call_args_list}, {6})
        self.assertEqual(result.zstd_level, 6)
        self.assertTrue(metrics["image_upload_optimized"])

    def test_transparency_and_mask_dimensions_are_preserved(self):
        result, metrics = self.resize(body(alpha=True, mask=True))
        self.assertTrue(metrics["image_upload_optimized"])
        self.assertLessEqual(len(result.body), BUDGET)
        decoded = decode_body(result.body, result.encoding)
        with decoded_image(decoded) as image, decoded_image(decoded, "mask") as mask:
            self.assertEqual(image.format, "PNG")
            self.assertEqual(mask.format, "PNG")
            self.assertEqual(image.size, mask.size)
            self.assertEqual(image.getchannel("A").getextrema(), (128, 128))
            self.assertEqual(mask.getchannel("A").getextrema(), (0, 0))
        self.assertTrue(metrics["image_upload_images"][-1]["mask"])

    def test_jpeg_and_webp_remain_valid_static_images(self):
        for fmt in ("JPEG", "WEBP"):
            with self.subTest(fmt=fmt):
                raw = body(fmt=fmt)
                # These fixtures can be just under 1 MB. A conservative fixed
                # budget exercises resizing without changing their image data.
                original = prepare_request_info(raw, HEADERS, "POST", PATH, "zstd", 6)
                result, metrics = optimize_image_request(raw, HEADERS, PATH, original, budget_bytes=500_000, zstd_level=6)
                self.assertTrue(metrics["image_upload_optimized"])
                self.assertLessEqual(len(result.body), 500_000)
                with decoded_image(decode_body(result.body, result.encoding)) as image:
                    self.assertEqual(image.format, fmt)

    def test_under_budget_does_not_open_images(self):
        raw = body()
        original = PreparedRequest(b"small request", "identity")
        with patch("packet_inspector.image_upload.read_image", side_effect=AssertionError("Must not read images")):
            result, metrics = self.resize(raw, prepared=original)
        self.assertIs(result, original)
        self.assertEqual(metrics, {})

    def test_invalid_animated_and_ambiguous_mask_inputs_are_not_modified(self):
        cases = [({"images":[{"image_url":"data:image/png;base64,AAAA"}]}, "invalid_image"),
                 ({"images":[{"image_url":"https://example.invalid/reference.png"}]}, "no_inline_images"),
                 ({"images":[{"image_url":image_uri()}],"mask":{"image_url":"https://example.invalid/mask.png"}}, "unsupported_image_reference"),
                 ({"images":[{"image_url":image_uri()}],"mask":{"image_url":image_uri()}}, "mask_alpha_missing")]
        frames = [Image.new("RGB", (30, 30), color) for color in ("red", "blue")]
        animated = BytesIO()
        frames[0].save(animated, format="GIF", save_all=True, append_images=frames[1:])
        for frame in frames:frame.close()
        cases.append(({"images":[{"image_url":"data:image/gif;base64,"+base64.b64encode(animated.getvalue()).decode("ascii")}]}, "animated_image"))
        original = PreparedRequest(b"x"*(BUDGET+1), "identity")
        for value, reason in cases:
            with self.subTest(reason=reason):
                result, metrics = self.resize(json.dumps(value).encode(), prepared=original)
                self.assertIs(result, original)
                self.assertFalse(metrics["image_upload_optimized"])
                self.assertEqual(metrics["image_upload_outcome"], reason)
                self.assertEqual(metrics["image_upload_iterations"], 0)

    def test_image_pixel_limit_is_checked_before_decoding_large_images(self):
        with patch("packet_inspector.image_upload.MAX_PIXELS", 100):
            result, metrics = self.resize(body())
        self.assertFalse(metrics["image_upload_optimized"])
        self.assertEqual(metrics["image_upload_outcome"], "image_pixel_limit")

    def test_multiple_images_and_an_already_compressed_ingress_keep_order_and_fields(self):
        value = json.loads(body(alpha=True))
        value["images"].append({"image_url":image_uri(), "detail":"high"})
        value["metadata"] = {"unchanged":[1,2,3]}
        raw = json.dumps(value).encode()
        ingress = zstandard.ZstdCompressor(level=1).compress(raw)
        headers = {**HEADERS,"Content-Encoding":"zstd"}
        result, metrics = self.resize(ingress, level=3, headers=headers)
        after = json.loads(decode_body(result.body, result.encoding))
        self.assertTrue(metrics["image_upload_optimized"])
        self.assertLessEqual(len(result.body), BUDGET)
        self.assertEqual(result.zstd_level, 3)
        self.assertEqual(after["metadata"],value["metadata"])
        self.assertEqual(after["images"][1]["detail"],"high")
        self.assertEqual([row["field"] for row in metrics["image_upload_images"]],["/images/0/image_url","/images/1/image_url"])
        with decoded_image(json.dumps(after).encode()) as first:
            self.assertIn("A",first.getbands())

    def test_ambiguous_mask_dimensions_do_not_modify_the_request(self):
        value = json.loads(body())
        mask = Image.new("RGBA",(32,32),(255,255,255,0))
        output = BytesIO()
        mask.save(output,format="PNG")
        mask.close()
        value["mask"] = {"image_url":"data:image/png;base64,"+base64.b64encode(output.getvalue()).decode("ascii")}
        raw = json.dumps(value).encode()
        original = prepare_request_info(raw,HEADERS,"POST",PATH,"zstd",6)
        result, metrics = self.resize(raw,prepared=original)
        self.assertIs(result,original)
        self.assertEqual(metrics["image_upload_outcome"],"mask_dimension_mismatch")

    def test_minimum_size_does_not_repeat_identical_resizes_or_claim_a_budget_success(self):
        image = Image.frombytes("RGB",(256,256),random.Random(1030).randbytes(256*256*3))
        output = BytesIO()
        image.save(output,format="PNG")
        image.close()
        uri = "data:image/png;base64,"+base64.b64encode(output.getvalue()).decode("ascii")
        raw = json.dumps({"images":[{"image_url":uri}],"prompt":"test"}).encode()
        original = prepare_request_info(raw,HEADERS,"POST",PATH,"zstd",6)
        result, metrics = optimize_image_request(raw,HEADERS,PATH,original,budget_bytes=100_000,zstd_level=6)
        self.assertEqual(metrics["image_upload_iterations"],1)
        self.assertEqual(metrics["image_upload_outcome"],"over_budget")
        self.assertGreater(len(result.body),100_000)

    def test_defaults_and_existing_settings_survive_reload(self):
        for saved, expected in (({},6), ({"image_upload_optimization_enabled":True},6),
                                ({"image_upload_optimization_enabled":False,"image_zstd_level":12},12),
                                ({"image_zstd_level":22,"zstd_level":11,"packet_size_protection_enabled":True},22),
                                ({"image_zstd_level":True},6), ({"image_zstd_level":"9"},6),
                                ({"image_zstd_level":0},6), ({"image_zstd_level":23},6)):
            with self.subTest(saved=saved), tempfile.TemporaryDirectory() as directory:
                folder = Path(directory)
                config = folder/"config.toml"
                config.write_text('model="test"',encoding="utf-8")
                data = folder/"data"
                data.mkdir()
                (data/"settings.json").write_text(json.dumps(saved),encoding="utf-8")
                with InspectorServer(("127.0.0.1",0),data,config,"http://127.0.0.1:9/v1") as server:
                    self.assertEqual(server.status()["image_zstd_level"],expected)
                    self.assertEqual(server.status()["auto_image_zstd_level"],6)
                    self.assertEqual(server.settings["zstd_level"],saved.get("zstd_level",3))
                    self.assertEqual(server.settings["packet_size_protection_enabled"],saved.get("packet_size_protection_enabled",False))
                    self.assertNotIn("image_upload_optimization_enabled",server.status())
                    self.assertNotIn("image_upload_optimization_enabled",server.settings)
                    persisted = json.loads(server.settings_path.read_text(encoding="utf-8"))
                    self.assertNotIn("image_upload_optimization_enabled",persisted)
                    self.assertEqual(persisted["image_zstd_level"],expected)


class ImageUploadRelayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixtures.RelayTests.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls):
        fixtures.RelayTests.tearDownClass.__func__(cls)

    relay = fixtures.RelayTests.relay
    latest = fixtures.RelayTests.latest
    api = fixtures.RelayTests.api

    def test_image_levels_and_size_protection_are_live_without_extra_inference_requests(self):
        self.assertEqual(self.api("/api/status")[1]["image_zstd_level"],6)
        original_settings = self.server.settings_path.read_bytes()
        for invalid in (0,23,True,"6",None):
            self.assertEqual(self.api("/api/settings",{"image_zstd_level":invalid,"zstd_level":8})[0],400)
            self.assertEqual(self.server.settings_path.read_bytes(),original_settings)
        with patch.dict(self.server.settings, {"auto_detect_providers":True,"packet_size_protection_enabled":False,
                        "auto_zstd_level_enabled":False,"image_zstd_level":3,"zstd_level":22}):
            raw = body(alpha=True)
            before = len(self.up.requests)
            self.assertEqual(self.relay(raw,path=PATH)[0],200)
            sent = self.up.requests[-1]
            self.assertEqual(decode_body(sent["body"],sent["headers"].get("Content-Encoding","identity")),raw)
            self.assertEqual(self.latest()[0]["transport_zstd_level"],3)
            self.assertNotIn("image_upload_optimized",self.latest()[0])
            self.assertEqual(self.api("/api/settings",{"packet_size_protection_enabled":True})[0],200)
            self.assertEqual(self.relay(raw,path=PATH,headers={"Digest":"sha-256=old-body"})[0],200)
            sent = self.up.requests[-1]
            self.assertNotIn("Digest",sent["headers"])
            self.assertEqual(len(self.up.requests),before+2)
            self.assertLessEqual(len(sent["body"]),BUDGET)
            meta,_ = self.latest()
            self.assertTrue(meta["image_upload_optimized"])
            self.assertLessEqual(meta["image_upload_iterations"],2)
            self.assertEqual(meta["transport_zstd_level"],3)
            self.assertEqual(meta["relay_wire_bytes"],len(sent["body"]))
            self.assertNotIn("usage_override_applied",meta)
            self.assertNotIn("overflow_compaction_calls",meta)
            self.assertEqual(self.api("/api/settings",{"image_zstd_level":17,"auto_zstd_level_enabled":True})[0],200)
            self.assertEqual(self.relay(raw,path=PATH)[0],200)
            meta,_ = self.latest()
            self.assertEqual(meta["transport_zstd_level"],6)
            self.assertTrue(meta["image_upload_optimized"])
            self.assertEqual(self.server.settings["image_zstd_level"],17)
            self.assertEqual(self.api("/api/settings",{"packet_size_protection_enabled":False})[0],200)
            self.assertFalse(self.server.settings["auto_zstd_level_enabled"])
            self.assertEqual(self.relay(raw,path=PATH)[0],200)
            sent = self.up.requests[-1]
            self.assertEqual(decode_body(sent["body"],sent["headers"].get("Content-Encoding","identity")),raw)
            self.assertEqual(self.latest()[0]["transport_zstd_level"],17)
            self.assertNotIn("image_upload_optimized",self.latest()[0])
            ordinary = json.dumps({"input":[{"role":"user","content":"ordinary request "*300}]}).encode()
            self.assertEqual(self.relay(ordinary,path="/responses")[0],200)
            self.assertEqual(self.latest()[0]["transport_zstd_level"],22)
            self.assertEqual(len(self.up.requests),before+5)


if __name__ == "__main__":
    unittest.main()
