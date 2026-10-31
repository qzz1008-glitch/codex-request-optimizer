"""Sharing preserves native protocol data without forwarding peer identities."""
from email.message import Message
from types import SimpleNamespace
import unittest

from packet_inspector.sharing import (Sharing, SharingError, allowed_request, forwarding_headers,
                                      forwarding_response_headers)


class NativeSharingHTTPTests(unittest.TestCase):
    def test_native_query_parameters_survive_without_expanding_endpoint_access(self):
        for method, suffix in (("POST", "/responses?future=1&hint=a%2Fb&hint=c"),
                               ("POST", "/responses/compact?api_version=next"),
                               ("GET", "/models?client_version=next")):
            with self.subTest(method=method, suffix=suffix):
                self.assertTrue(allowed_request(method, suffix))
        for method, suffix in (("POST", "https://chatgpt.com/responses"),
                               ("POST", "//chatgpt.com/responses"), ("POST", "/responses#fragment"),
                               ("POST", "/responses#"), ("POST", "/responses/unknown?future=1"),
                               ("POST", "/models?future=1"), ("DELETE", "/responses?future=1"),
                               ("GET", "/responses?future=1")):
            with self.subTest(method=method, suffix=suffix):
                self.assertFalse(allowed_request(method, suffix))

    def test_connection_options_are_case_insensitive_and_cover_repeated_fields(self):
        headers = Message()
        for key, value in (("cOnNeCtIoN", "X-First, Keep-Alive"), ("Connection", "x-second"),
                           ("X-First", "private"), ("x-second", "private"),
                           ("Proxy-Connection", "close"), ("Authorization", "guest-secret"),
                           ("Cookie", "guest-cookie"), ("Session_id", "native-session"),
                           ("OpenAI-Beta", "native-beta"), ("X-Codex-Future", "opaque")):
            headers[key] = value
        self.assertEqual(forwarding_headers(headers),
                         {"Session_id": "native-session", "OpenAI-Beta": "native-beta", "X-Codex-Future": "opaque"})

    def test_unknown_response_headers_and_duplicates_survive_but_private_headers_do_not(self):
        headers = Message()
        for key, value in (("Connection", "X-First"), ("cOnNeCtIoN", "x-second"),
                           ("X-First", "hop-private"), ("X-Second", "hop-private"),
                           ("Set-Cookie", "owner-cookie"), ("Authorization", "owner-secret"),
                           ("ChatGPT-Account-Id", "owner-id"), ("X-Inspector-Control", "private"),
                           ("Content-Length", "123"), ("Retry-After", "12"),
                           ("X-Codex-Future", "first"), ("X-Codex-Future", "second"),
                           ("WWW-Authenticate", 'Bearer error="invalid_token"')):
            headers[key] = value
        self.assertEqual(forwarding_response_headers(headers),
                         [("Content-Length", "123"), ("Retry-After", "12"),
                          ("X-Codex-Future", "first"), ("X-Codex-Future", "second"),
                          ("WWW-Authenticate", 'Bearer error="invalid_token"')])

    @staticmethod
    def upstream(value, mode):
        link = SimpleNamespace(profile=lambda: {"provider": "openai", "upstream": value},
                               _read=lambda: ("", {}))
        sharing = SimpleNamespace(server=SimpleNamespace(link=link))
        return Sharing._host_upstream(sharing, {"mode": mode})

    def test_configured_official_api_path_is_authoritative(self):
        for mode, value in (("chatgpt", "https://chatgpt.com/backend-api/codex-next"),
                            ("apikey", "https://api.openai.com/v42")):
            with self.subTest(mode=mode):
                self.assertEqual(self.upstream(value, mode), value)

    def test_native_credentials_remain_bound_to_their_official_https_host(self):
        for mode, value in (("chatgpt", "https://api.openai.com/v1"),
                            ("apikey", "https://chatgpt.com/backend-api/codex"),
                            ("chatgpt", "http://chatgpt.com/backend-api/codex"),
                            ("chatgpt", "https://chatgpt.com.evil.invalid/backend-api/codex"),
                            ("chatgpt", "https://chatgpt.com:444/backend-api/codex"),
                            ("chatgpt", "https://chatgpt.com:invalid/backend-api/codex"),
                            ("chatgpt", "https://user:secret@chatgpt.com/backend-api/codex"),
                            ("chatgpt", "https://chatgpt.com/backend-api/codex?redirect=other"),
                            ("chatgpt", "https://chatgpt.com/backend-api/codex#fragment")):
            with self.subTest(mode=mode, value=value), self.assertRaises(SharingError):
                self.upstream(value, mode)


if __name__ == "__main__":
    unittest.main()
