"""Machine-wide roles survive cross-thread calls and cannot form relay chains."""
import secrets
import subprocess
import sys
import threading
import unittest

from packet_inspector.sharing_auth import SharingError
from packet_inspector.sharing_role import MachineSharingRole


class SharingRoleTests(unittest.TestCase):
    def test_other_instances_are_excluded_and_another_thread_can_release(self):
        name = 'CodexSharingTest-' + secrets.token_hex(16)
        owner, other = MachineSharingRole(name), MachineSharingRole(name)
        self.addCleanup(owner.close)
        self.addCleanup(other.close)
        owner.acquire()
        with self.assertRaises(SharingError):
            other.acquire()
        thread = threading.Thread(target=owner.close)
        thread.start()
        thread.join(3)
        self.assertFalse(thread.is_alive())
        other.acquire()
        with self.assertRaises(SharingError):
            owner.acquire()

    def test_process_exit_releases_the_role_without_stale_files_or_configuration(self):
        name = 'CodexSharingTest-' + secrets.token_hex(16)
        program = ('import sys\nfrom packet_inspector.sharing_role import MachineSharingRole\n'
                   'role=MachineSharingRole(sys.argv[1]);role.acquire()\n'
                   'print("ready",flush=True)\nsys.stdin.readline()\n')
        child = subprocess.Popen([sys.executable, '-u', '-c', program, name], stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf8')
        contender = MachineSharingRole(name)
        try:
            self.assertEqual(child.stdout.readline().strip(), 'ready')
            with self.assertRaises(SharingError):
                contender.acquire()
            child.terminate()  # Terminate this isolated test helper, never an app/service.
            child.communicate(timeout=5)
            contender.acquire()
        finally:
            contender.close()
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=5)
