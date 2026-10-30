from email.message import Message
import io
import json
import unittest

from packet_inspector.sharing_errors import MAX_ERROR_BYTES, prepare_shared_error


class Response(io.BytesIO):
    def __init__(self, body, status=403, encoding="identity"):
        super().__init__(body)
        self.status, self.reason = status, "Forbidden"
        self.headers = Message()
        for name, value in {"Content-Type": "application/json", "Content-Length": str(len(body)),
                            "Content-Encoding": encoding, "ETag": "unchanged"}.items():
            self.headers[name] = value


class SharingErrorCompatibilityTests(unittest.TestCase):
    def test_legacy_message_becomes_the_model_error_message(self):
        message = "此凭据的本期额度已用完。"
        original = Response(json.dumps({"error": "sharing_unavailable", "message": message}).encode())
        with prepare_shared_error(original) as response:
            body = response.read()
            self.assertEqual(json.loads(body), {"error": {"code": "sharing_unavailable", "message": message}})
            self.assertEqual(response.status, 403)
            self.assertEqual(int(response.headers["Content-Length"]), len(body))
            self.assertNotIn("ETag", response.headers)
            self.assertTrue(response.changed)
        self.assertTrue(original.closed)

    def test_missing_legacy_message_has_an_actionable_fallback(self):
        with prepare_shared_error(Response(b'{"error":"sharing_unavailable"}')) as response:
            message = json.loads(response.read())["error"]["message"]
            self.assertIn("共享已停止", message)
            self.assertIn("提供方", message)

    def test_upstream_unknown_and_already_structured_errors_keep_their_bytes(self):
        for body in (b'{"error":{"message":"upstream-error","code":"upstream-code"}}',
                     b'{"error":"some-other-upstream-error","message":"unchanged"}', b'not json', b'[]'):
            with self.subTest(body=body), prepare_shared_error(Response(body)) as response:
                self.assertFalse(response.changed)
                self.assertEqual(response.read(), body)
                self.assertEqual(response.headers["ETag"], "unchanged")

    def test_success_streams_and_encoded_errors_are_not_read(self):
        for status, encoding in ((200, "identity"), (429, "identity"), (403, "gzip")):
            original = Response(b'{"error":"sharing_unavailable"}', status, encoding)
            with prepare_shared_error(original) as response:
                self.assertIs(response, original)
                self.assertEqual(original.tell(), 0)

    def test_large_errors_are_bounded_and_replayed_without_loss(self):
        body = b'{"error":"sharing_unavailable","message":"' + b'x' * MAX_ERROR_BYTES + b'"}'
        original = Response(body)
        with prepare_shared_error(original) as response:
            self.assertEqual(original.tell(), MAX_ERROR_BYTES + 1)
            self.assertFalse(response.changed)
            self.assertEqual(response.read(), body)
