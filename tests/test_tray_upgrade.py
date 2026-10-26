"""A tray handoff must leave the entry's HTTP listener alive."""
import json
import http.client
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest
from urllib.request import urlopen

from packet_inspector.tray import close_entry_tray


@unittest.skipUnless(os.name == "nt", "Windows notification area integration")
class NativeTrayUpgradeTests(unittest.TestCase):
    def test_close_only_the_owned_legacy_tray_and_keep_forwarding(self):
        code = r'''
import json,os,threading
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from packet_inspector.tray import NativeTray
tray=NativeTray(lambda:None,lambda:None).start()
release=threading.Event()
class Handler(BaseHTTPRequestHandler):
 def log_message(self,*args):pass
 def do_GET(self):
  if self.path=='/held':
   self.send_response(200);self.send_header('Content-Length','12');self.end_headers()
   self.wfile.write(b'first-');self.wfile.flush();release.wait(8);self.wfile.write(b'second');return
  if self.path in ('/release','/stop'):release.set()
  body=json.dumps({'tray_alive':tray.thread.is_alive(),'http_alive':True}).encode()
  self.send_response(200);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
  if self.path=='/stop':threading.Thread(target=self.server.shutdown,daemon=True).start()
server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
print(json.dumps({'pid':os.getpid(),'port':server.server_port}),flush=True)
try:server.serve_forever(poll_interval=.05)
finally:tray.stop();server.server_close()
'''
        child = subprocess.Popen([sys.executable, "-u", "-c", code], cwd=Path(__file__).resolve().parents[1],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf8",
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        endpoint = None
        held = None
        try:
            identity = json.loads(child.stdout.readline())
            endpoint = "http://127.0.0.1:" + str(identity["port"])
            held = http.client.HTTPConnection("127.0.0.1", identity["port"], timeout=6)
            held.request("GET", "/held")
            held_response = held.getresponse()
            self.assertEqual(held_response.read(6), b"first-")
            self.assertFalse(close_entry_tray(identity["pid"], Path(sys.executable).with_name("foreign.exe")))
            with urlopen(endpoint, timeout=2) as response:
                self.assertTrue(json.load(response)["tray_alive"])
            self.assertTrue(close_entry_tray(identity["pid"], sys.executable))
            deadline = time.monotonic() + 4
            while time.monotonic() < deadline:
                with urlopen(endpoint, timeout=2) as response:
                    state = json.load(response)
                if not state["tray_alive"]:
                    break
                time.sleep(.05)
            self.assertFalse(state["tray_alive"])
            self.assertTrue(state["http_alive"])
            self.assertIsNone(child.poll())
            with urlopen(endpoint + "/release", timeout=2):
                pass
            self.assertEqual(held_response.read(), b"second")
        finally:
            if held:
                held.close()
            if endpoint:
                try:
                    with urlopen(endpoint + "/stop", timeout=2):
                        pass
                except OSError:
                    pass
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=3)
            child.stdout.close()
            child.stderr.close()
