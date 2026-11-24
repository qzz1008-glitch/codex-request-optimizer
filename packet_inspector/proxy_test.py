"""One explicit, bounded HEAD probe; no inference, account auth or redirects."""
import threading
import time
from urllib.error import HTTPError
from urllib.parse import unquote, urlsplit
from urllib.request import Request

from .network_diagnostics import exception_detail, safe_url
from .proxy_settings import bypass_proxy, proxy_details

TEST_TIMEOUT_SECONDS = 10


class ProxyTester:
    def __init__(self, opener_factory, timeout=TEST_TIMEOUT_SECONDS):
        self.opener_factory, self.timeout = opener_factory, timeout
        self.running = threading.Lock()

    def test(self, proxies, mode, upstream):
        started, done, result = time.monotonic(), threading.Event(), {}
        target = urlsplit(upstream)
        hostname = "[" + target.hostname + "]" if ":" in target.hostname else target.hostname
        authority = hostname + ":" + str(target.port or (443 if target.scheme == "https" else 80))
        selected = proxies.get(target.scheme)
        used = bool(selected) and not bypass_proxy(authority, proxies, target.scheme)
        base = {"mode": mode, "target": safe_url(upstream), "proxy_used": used,
                "proxy": proxy_details(selected)["address"] if used else "", "bypassed": bool(selected) and not used}
        secrets = []
        for value in proxies.values():
            try:
                proxy = urlsplit(value if "://" in value else "http://" + value)
                secrets.extend(unquote(v) for v in (proxy.username, proxy.password) if v)
            except ValueError:
                pass
        if not self.running.acquire(blocking=False):
            raise ValueError("代理测试正在进行，请稍后重试。")

        def probe():
            try:
                opener = self.opener_factory(proxies)
                request = Request(upstream, method="HEAD", headers={"User-Agent": "Codex-Request-Optimizer-Proxy-Test"})
                try:
                    response = opener.open(request, timeout=self.timeout)
                except HTTPError as error:
                    response = error
                with response:
                    status = response.status
                result.update(base, state="ok" if 200 <= status < 400 or status in {401, 404, 405} else "rejected",
                              status=status)
            except Exception as error:
                chain = exception_detail(error, secrets)
                cause = chain[-1]
                result.update(base, state="failed", error=cause["type"] + ": " + cause["message"])
            finally:
                result["elapsed_ms"] = round((time.monotonic() - started) * 1000)
                self.running.release()
                done.set()

        try:
            threading.Thread(target=probe, daemon=True, name="proxy-test").start()
        except Exception:
            self.running.release()
            raise
        if done.wait(self.timeout):
            return result
        # A resolver can outlive socket timeouts. Keep the single slot occupied
        # until it exits so repeated clicks cannot accumulate waiting threads.
        return {**base, "state": "timeout", "elapsed_ms": round((time.monotonic() - started) * 1000)}
