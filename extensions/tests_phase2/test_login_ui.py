import http.client
import json
import re
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from urllib.parse import urlencode

from phase2.login import make_server


class FakeQR:
    def __init__(self):
        self.start_entered = threading.Event()
        self.start_release = threading.Event()
        self.start_release.set()
        self.poll_entered = threading.Event()
        self.poll_release = threading.Event()
        self.poll_release.set()
        self.result = {"status": "waiting"}
        self.serial = 0

    def start(self):
        self.start_entered.set()
        self.start_release.wait(3)
        self.serial += 1
        return {"token": str(self.serial), "image": "data:image/png;base64,TEST"}

    def poll(self, token):
        self.poll_entered.set()
        self.poll_release.wait(3)
        return self.result


class TestLoginUI(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.qr = FakeQR()
        self.server = make_server(self.root, 0, self.qr, poll_interval=0.02)
        self.port = self.server.server_port
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()
        self.csrf = re.search('name="csrf" value="([^"]+)"', self.get()[1]).group(1)

    def tearDown(self):
        self.qr.start_release.set()
        self.qr.poll_release.set()
        self.server.server_close()
        self.server.shutdown()
        self.worker.join(2)
        self.tmp.cleanup()

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=0.8)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            return response.status, response.read().decode()
        finally:
            connection.close()

    def get(self, path="/"):
        return self.request("GET", path)

    def start(self, uid="100", csrf=None):
        return self.request(
            "POST", "/new", urlencode({"uid": uid, "csrf": csrf or self.csrf})
        )

    def until(self, text):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if text in self.get()[1]:
                return
            time.sleep(0.02)
        self.fail("Expected UI status did not appear")

    def test_browser_idle_connection_does_not_block_page(self):
        idle = socket.create_connection(("127.0.0.1", self.port))
        try:
            time.sleep(0.05)
            self.assertEqual(self.get()[0], 200)
        finally:
            idle.close()

    def test_slow_qr_generation_does_not_block_page_or_submit(self):
        self.qr.start_release.clear()
        self.assertEqual(self.start()[0], 303)
        self.assertTrue(self.qr.start_entered.wait(1))
        self.assertIn("正在向 B 站申请二维码", self.get()[1])
        self.assertEqual(self.start()[0], 409)
        self.qr.start_release.set()
        self.until("用B站App扫描")

    def test_slow_poll_does_not_block_page(self):
        self.qr.poll_release.clear()
        self.assertEqual(self.start()[0], 303)
        self.assertTrue(self.qr.poll_entered.wait(1))
        self.assertEqual(self.get()[0], 200)
        self.assertIn("用B站App扫描", self.get()[1])

    def test_wrong_account_never_saved(self):
        self.qr.result = {
            "status": "confirmed",
            "account": {"mid": "200"},
            "config": {"fixture": True},
        }
        self.start("100")
        self.until("登录账号与指定 UID 不一致")
        self.assertFalse((self.root / "bili-credentials.json").exists())

    def test_stale_confirmation_cannot_bind_replacement_qr(self):
        self.qr.poll_release.clear()
        self.qr.result = {
            "status": "confirmed",
            "account": {"mid": "100"},
            "config": {"fixture": True},
        }
        self.start("100")
        self.assertTrue(self.qr.poll_entered.wait(1))
        self.assertEqual(self.start("200")[0], 303)
        self.qr.poll_release.set()
        self.until("登录账号与指定 UID 不一致")
        self.assertFalse((self.root / "bili-credentials.json").exists())

    def test_confirmed_account_saved_private_and_ui_restart_cannot_replace(self):
        self.qr.result = {
            "status": "confirmed",
            "account": {"mid": "100", "name": "fixture"},
            "config": {"fixture": True},
        }
        self.start()
        self.until("已登录并校验账号")
        self.assertEqual(
            (self.root / "bili-credentials.json").stat().st_mode & 0o777, 0o600
        )
        self.assertEqual(
            json.loads((self.root / "account.json").read_text())["user_selected_uid"],
            "100",
        )
        self.assertEqual(self.start("200")[0], 409)
        other = make_server(self.root, 0, FakeQR())
        other_thread = threading.Thread(target=other.serve_forever, daemon=True)
        other_thread.start()
        try:
            old_port = self.port
            self.port = other.server_port
            self.assertIn("已登录并校验账号", self.get()[1])
        finally:
            self.port = old_port
            other.server_close()
            other.shutdown()
            other_thread.join(2)

    def test_csrf_host_and_static_file_protection(self):
        self.assertEqual(self.start(csrf="invalid")[0], 400)
        self.assertEqual(self.get("/bili-credentials.json")[0], 404)
        self.assertEqual(
            self.request("GET", "/", headers={"Host": "example.com"})[0], 403
        )

    def test_malformed_length_does_not_break_server(self):
        self.assertEqual(
            self.request("POST", "/new", headers={"Content-Length": "bad"})[0], 400
        )
        self.assertEqual(self.get()[0], 200)


if __name__ == "__main__":
    unittest.main()
