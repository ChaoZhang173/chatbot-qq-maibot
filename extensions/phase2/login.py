"""Loopback-only QR UI. No generic static route or credential response."""

import json, os, threading, time, html, secrets
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from bili_login import BiliQrLoginManager


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = path.with_suffix(".tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp, path)


def make_server(private, port=18766, manager=None, poll_interval=3):
    os.umask(0o077)
    private = Path(private)
    private.mkdir(mode=0o700, parents=True, exist_ok=True)
    manager = manager or BiliQrLoginManager()
    lock = threading.Lock()
    manager_lock = threading.Lock()
    stopped = threading.Event()
    state = {"status": "idle"}
    generation = 0
    if (private / "bili-credentials.json").exists():
        # A UI restart must not silently permit replacing an already bound account.
        account = json.loads((private / "account.json").read_text())
        state.update(status="confirmed", account=account)
    csrf = secrets.token_hex(24)

    class Handler(BaseHTTPRequestHandler):
        timeout = 10

        def log_message(self, *args):
            pass

        def send(self, status, body, kind="text/html; charset=utf-8"):
            data = body.encode()
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'none'; img-src data:; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'",
            )
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.headers.get("Host") != f"127.0.0.1:{port}":
                return self.send(403, "拒绝")
            if self.path != "/":
                return self.send(404, "不存在")
            with lock:
                s = dict(state)
            text = {
                "idle": "输入要运行机器人的账号 UID，然后生成二维码。",
                "generating": "正在向 B 站申请二维码，请稍后刷新此页。",
                "waiting": "请用该账号的哔哩哔哩 App 扫码并确认；完成后刷新此页。",
                "scanned": "请在 App 内确认登录，再刷新此页。",
                "confirmed": "已登录并校验账号。此页面只管理登录，机器人运行状态以联调结果为准。",
                "expired": "二维码已过期，可以重新生成。",
                "error": "平台或网络请求未完成，请稍后重试。",
                "mismatch": "登录账号与指定 UID 不一致，未保存凭据，全部外发关闭。",
            }.get(s["status"], "等待")
            account = s.get("account", {})
            body = f'<html lang="zh"><meta charset="utf-8"><title>B站机器人账号登录</title><style>body{{font:18px system-ui;max-width:700px;margin:50px auto;padding:20px}}input,button{{font:inherit;padding:10px}}img{{max-width:320px}}</style><h1>B站机器人账号登录</h1><p>{text}</p><p>{html.escape(str(account.get("name", "")))} {html.escape(str(account.get("mid", "")))}</p>'
            if s.get("image") and s["status"] in ("waiting", "scanned"):
                body += f'<img alt="用B站App扫描" src="{s["image"]}">'
            if s["status"] != "confirmed":
                body += f'<form method="post" action="/new"><input type="hidden" name="csrf" value="{csrf}"><p>机器人账号 UID（主页数字）</p><input name="uid" pattern="[0-9]+" required><button>生成 / 更新二维码</button></form>'
            body += '<p>登录凭据保存在权限受限的文件中。此界面不会显示 Cookie，也不会发布内容。</p><a href="/">刷新状态</a></html>'
            self.send(200, body)

        def do_POST(self):
            nonlocal generation
            from urllib.parse import parse_qs

            if (
                self.path != "/new"
                or self.headers.get("Host") != f"127.0.0.1:{port}"
                or self.headers.get("Origin", f"http://127.0.0.1:{port}")
                != f"http://127.0.0.1:{port}"
            ):
                return self.send(403, "拒绝")
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 1024:
                    return self.send(400, "请求长度错误")
                data = parse_qs(self.rfile.read(length).decode("utf-8"))
            except (ValueError, UnicodeError):
                return self.send(400, "请求格式错误")
            uid = data.get("uid", [""])[0]
            if data.get("csrf", [""])[0] != csrf or not uid.isdigit() or int(uid) <= 0:
                return self.send(400, "请输入正确 UID")
            with lock:
                if state.get("status") == "confirmed":
                    return self.send(409, "已登录，若需更换账号请停止服务后重新准备")
                if state.get("status") == "generating":
                    return self.send(409, "正在生成二维码，请稍后刷新")
                generation += 1
                current = generation
                state.clear()
                state.update(status="generating", expected_uid=uid)
            threading.Thread(target=generate, args=(current, uid), daemon=True).start()
            self.send_response(303)
            self.send_header("Location", "/")
            self.send_header("Content-Length", "0")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()

    def generate(current, uid):
        try:
            with manager_lock:
                qr = manager.start()
            with lock:
                if current == generation and not stopped.is_set():
                    state.clear()
                    state.update(qr, status="waiting", expected_uid=uid)
        except Exception:
            with lock:
                if current == generation:
                    state.clear()
                    state.update(status="error")

    def poll():
        while not stopped.wait(poll_interval):
            with lock:
                if state.get("status") not in ("waiting", "scanned"):
                    continue
                current, token, expected_uid = (
                    generation,
                    state["token"],
                    state["expected_uid"],
                )
            try:
                with manager_lock:
                    result = manager.poll(token)
                with lock:
                    # Discard a previous QR's result if the user requested a new one.
                    if current != generation or stopped.is_set():
                        continue
                    if result["status"] == "confirmed":
                        if str(result["account"]["mid"]) != expected_uid:
                            state.clear()
                            state.update(status="mismatch")
                            continue
                        atomic_json(private / "bili-credentials.json", result["config"])
                        atomic_json(
                            private / "account.json",
                            dict(
                                result["account"],
                                user_selected_uid=expected_uid,
                                verified_at=int(time.time()),
                            ),
                        )
                        state.clear()
                        state.update(status="confirmed", account=result["account"])
                        print(
                            json.dumps(
                                {
                                    "event": "login_confirmed",
                                    "account": result["account"],
                                },
                                ensure_ascii=False,
                            ),
                            flush=True,
                        )
                    else:
                        state["status"] = result["status"]
            except Exception:
                with lock:
                    if current == generation:
                        state["status"] = "error"

    class LoginServer(ThreadingHTTPServer):
        daemon_threads = True

        def server_close(self):
            stopped.set()
            super().server_close()

    server = LoginServer(("127.0.0.1", port), Handler)
    port = server.server_port
    threading.Thread(target=poll, daemon=True).start()
    return server


def serve(private, port=18766):
    server = make_server(private, port)
    print(f"LOGIN_UI http://127.0.0.1:{port}", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser()
    p.add_argument("--private", required=True)
    p.add_argument("--port", type=int, default=18766)
    a = p.parse_args()
    serve(a.private, a.port)
