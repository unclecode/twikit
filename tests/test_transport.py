import asyncio
import io
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from curl_cffi.requests import AsyncSession, Response

from twikit import Client
from twikit.constants import IMPERSONATE
from twikit.errors import TooManyRequests
from twikit.guest import GuestClient


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def answer(self, status, body, headers=None):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(status)
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.startswith("/json"):
            self.answer(200, {"cookie": self.headers.get("Cookie"), "tid": self.headers.get("X-Client-Transaction-Id"), "ua": self.headers.get("User-Agent")}, {"Set-Cookie": "ct0=rotated; Path=/"})
        elif self.path.startswith("/text"):
            self.answer(200, b"<html>not json</html>")
        elif self.path.startswith("/redirect"):
            self.answer(302, b"", {"Location": "/json"})
        elif self.path.startswith("/limited"):
            self.answer(429, b"slow down", {"x-rate-limit-reset": "1700000000"})
        elif self.path.startswith("/stream"):
            data = b'{"topic": "a", "payload": {}}\n\nnot json\n{"topic": "b", "payload": {}}\n'
            self.answer(200, data)
        else:
            self.answer(404, b"")

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length)
        self.answer(200, {"contentType": self.headers.get("Content-Type"), "hasPart": b'name="media"; filename="blob"' in body, "payload": b"chunk-bytes" in body})


@pytest.fixture(scope="module")
def server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


def offline_client():
    client = Client(language="en-US")
    client.client_transaction.home_page_response = True
    client.client_transaction.generate_transaction_id = lambda method, path: "tid"
    return client


def run(coroutine):
    return asyncio.run(coroutine)


def test_session_is_curl_cffi_with_pinned_impersonation():
    client = Client()
    assert isinstance(client.http, AsyncSession)
    assert client.http.impersonate == IMPERSONATE
    assert client.http.allow_redirects is False
    guest = GuestClient()
    assert isinstance(guest.http, AsyncSession)
    assert guest.http.impersonate == IMPERSONATE


def test_impersonation_can_be_overridden():
    assert Client(impersonate="chrome136").http.impersonate == "chrome136"


def test_proxy_round_trips():
    client = Client(proxy="http://user:pass@10.0.0.1:8080")
    assert client.proxy == "http://user:pass@10.0.0.1:8080"
    client.proxy = None
    assert client.proxy is None
    guest = GuestClient(proxy="socks5://10.0.0.2:1080")
    assert guest.proxy == "socks5://10.0.0.2:1080"


def test_set_cookies_are_sent_and_duplicate_ct0_keeps_the_first_as_with_httpx(server):
    client = offline_client()
    client.set_cookies({"auth_token": "a", "ct0": "old"}, clear_cookies=True)
    data, response = run(client.get(f"{server}/json"))
    assert isinstance(response, Response)
    assert "auth_token=a" in data["cookie"] and "ct0=old" in data["cookie"]
    assert data["tid"] == "tid"
    assert data["ua"] and "Safari" in data["ua"]
    assert [c.name for c in client.http.cookies.jar].count("ct0") == 1
    assert client.get_cookies() == {"auth_token": "a", "ct0": "old"}
    assert client._get_csrf_token() == "old"


def test_set_cookie_from_the_answer_lands_in_the_jar(server):
    client = offline_client()
    run(client.get(f"{server}/json"))
    assert client.get_cookies()["ct0"] == "rotated"


def test_non_json_answer_falls_back_to_text(server):
    data, response = run(offline_client().get(f"{server}/text"))
    assert data == "<html>not json</html>"
    assert response.status_code == 200


def test_redirects_are_not_followed(server):
    _, response = run(offline_client().get(f"{server}/redirect"))
    assert response.status_code == 302


def test_error_carries_the_answer_headers(server):
    client = offline_client()

    async def not_suspended():
        return "normal"

    client._get_user_state = not_suspended
    with pytest.raises(TooManyRequests) as caught:
        run(client.get(f"{server}/limited"))
    assert caught.value.rate_limit_reset == 1700000000


def test_media_append_goes_as_multipart(server):
    client = offline_client()
    client.v11.base = client

    async def upload(method, is_long_video, *args, **kwargs):
        return await client.request(method, f"{server}/upload", *args, **kwargs)

    client.v11.upload_media = upload
    data, _ = run(client.v11.upload_media_append(False, "1", 0, io.BytesIO(b"chunk-bytes")))
    assert data["contentType"].startswith("multipart/form-data")
    assert data["hasPart"] and data["payload"]


def test_stream_reads_lines(server):
    client = offline_client()

    async def lines():
        out = []
        async with client.http.stream("GET", f"{server}/stream", timeout=None) as response:
            async for line in response.aiter_lines():
                try:
                    out.append(json.loads(line)["topic"])
                except json.JSONDecodeError:
                    continue
        return out

    assert run(lines()) == ["a", "b"]
