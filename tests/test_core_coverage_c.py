"""核心模块补充覆盖率测试（C 组）。

覆盖 ``builtin_tunnel`` / ``levilamina`` / ``upnp`` / ``remote`` / ``web.handler``：

- 全部网络访问打桩（``urllib.request.urlopen`` / 假 socket / 假 Popen），不联网、
  不启动真实子进程、不访问真实路由器；
- HTTP Handler 使用绑定 127.0.0.1:0 的 ThreadingHTTPServer，不占用 19100；
- 每个用例结束恢复 AppContext.settings / api_token，保证可重复。

只新增本文件，不修改任何生产代码。
"""

import hashlib
import io
import json
import os
import socket
import sys
import tarfile
import tempfile
import threading
import time
import types
import unittest
import urllib.error
import zipfile
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from unittest import mock

from bedrock_server_manager import (
    builtin_tunnel as bt,
    levilamina as L,
    remote,
    upnp,
)
from bedrock_server_manager.app_context import AppContext
from bedrock_server_manager.web import handler as H

# --------------------------------------------------------------------------- 公共工具


def _write(path, data="x", binary=False):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    mode = "wb" if binary else "w"
    kwargs = {} if binary else {"encoding": "utf-8"}
    with open(path, mode, **kwargs) as handle:
        handle.write(data)
    return path


def _manifest(name, version="1.0.0", type_="native"):
    return json.dumps({"name": name, "version": version, "type": type_})


def _zip_bytes(members):
    """members: dict[name] = bytes/str -> zip 文件字节。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in members.items():
            if isinstance(data, str):
                data = data.encode("utf-8")
            zf.writestr(name, data)
    return buf.getvalue()


class _FakeResponse:
    """urlopen 返回值桩（支持 with 与分块 read）。"""

    def __init__(self, data):
        self._data = data if isinstance(data, bytes) else str(data).encode()
        self._pos = 0

    def read(self, size=-1):
        if size is None or size < 0:
            size = len(self._data) - self._pos
        chunk = self._data[self._pos : self._pos + size]
        self._pos += len(chunk)
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class _HttpError(urllib.error.HTTPError):
    def __init__(self, code, body, url="http://fake.local/ctl"):
        super().__init__(url, code, "boom", {}, io.BytesIO(body))


class _SettingsCase(unittest.TestCase):
    """隔离 AppContext.settings / api_token。"""

    _KEYS = (
        "frp_config",
        "playit_claim",
        "remote_enabled",
        "remote_password_hash",
        "remote_hosts",
        "server_dir",
        "installed_version",
    )

    def setUp(self):
        self.settings = AppContext.instance().settings
        self._saved_settings = dict(self.settings)
        for key in self._KEYS:
            self.settings.pop(key, None)
        self._saved_token = AppContext.instance().api_token

    def tearDown(self):
        self.settings.clear()
        self.settings.update(self._saved_settings)
        AppContext.instance().api_token = self._saved_token


def _wait_until(predicate, timeout=5.0, interval=0.01):
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return True
        time.sleep(interval)
    return False


# =========================================================================== remote


class TestRemotePasswordsAndConfig(_SettingsCase):
    def test_hash_verify_and_policy(self):
        stored = remote.hash_password("goodpass123")
        self.assertTrue(stored.startswith("pbkdf2_sha256$"))
        self.assertNotIn("goodpass123", stored)
        self.assertTrue(remote.verify_password("goodpass123", stored))
        self.assertFalse(remote.verify_password("bad", stored))
        self.assertFalse(remote.verify_password("", stored))
        self.assertFalse(remote.verify_password("x", ""))
        for bad in ("", "plain", "md5$1$a$b", "pbkdf2_sha256$abc$@@$@@", "pbkdf2_sha256$1$@@$@@"):
            self.assertFalse(remote.verify_password("whatever", bad), bad)

    def test_password_ok(self):
        self.assertFalse(remote.password_ok(12345678))
        self.assertFalse(remote.password_ok("short"))
        self.assertFalse(remote.password_ok("aaaaaaaa"))
        self.assertTrue(remote.password_ok("abcd1234"))

    def test_enabled_and_configured(self):
        self.assertFalse(remote.enabled())
        self.assertFalse(remote.configured())
        self.settings["remote_password_hash"] = remote.hash_password("abcd1234")
        self.assertTrue(remote.configured())
        self.assertFalse(remote.enabled())
        self.settings["remote_enabled"] = True
        self.assertTrue(remote.enabled())

    def test_settings_failures_are_safe(self):
        with mock.patch("bedrock_server_manager.app_context.AppContext.instance", side_effect=RuntimeError("x")):
            self.assertFalse(remote.enabled())
            self.assertFalse(remote.configured())
            self.assertEqual(remote.extra_hosts(), set())

    def test_extra_hosts_normalization(self):
        self.settings["remote_hosts"] = [" Office.Example.com ", "", "nas.local"]
        self.assertEqual(remote.extra_hosts(), {"office.example.com", "nas.local"})
        self.settings["remote_hosts"] = "not-a-list"
        self.assertEqual(remote.extra_hosts(), set())
        self.settings["remote_hosts"] = ["  "]
        self.assertEqual(remote.extra_hosts(), set())

    def test_set_config_validation_and_restart_flag(self):
        with self.assertRaises(ValueError):
            remote.set_config(True, password="short")
        with self.assertRaises(ValueError):
            remote.set_config(True)  # 未设置密码
        with mock.patch("bedrock_server_manager.utils.save_settings", return_value=True):
            result = remote.set_config(True, password="abcd1234", hosts=[" a.com ", "", "b.com"])
            self.assertTrue(result["enabled"])
            self.assertTrue(result["restart_required"])
            self.assertTrue(remote.enabled())
            self.assertEqual(self.settings["remote_hosts"], ["a.com", "b.com"])
            again = remote.set_config(True)
            self.assertFalse(again["restart_required"])
            off = remote.set_config(False)
            self.assertFalse(off["enabled"])
            self.assertTrue(off["restart_required"])

    def test_set_config_hosts_limit(self):
        with mock.patch("bedrock_server_manager.utils.save_settings", return_value=True):
            remote.set_config(
                True,
                password="abcd1234",
                hosts=[f"h{i}.example.com" for i in range(30)],
            )
        self.assertEqual(len(self.settings["remote_hosts"]), 20)

    def test_reenable_clears_sessions(self):
        self.settings["remote_password_hash"] = remote.hash_password("abcd1234")
        self.settings["remote_enabled"] = True
        remote.sessions.create("10.0.0.2")
        with mock.patch("bedrock_server_manager.utils.save_settings", return_value=True):
            result = remote.set_config(True)
        self.assertFalse(result["restart_required"])
        self.assertEqual(remote.sessions.count(), 0)


class TestRemoteNetworkAndSessions(_SettingsCase):
    def setUp(self):
        super().setUp()
        remote._ip_cache.update({"time": 0.0, "values": set()})

    def tearDown(self):
        remote._ip_cache.update({"time": 0.0, "values": set()})
        super().tearDown()

    def _fake_psutil(self, mapping):
        return types.SimpleNamespace(net_if_addrs=lambda: mapping)

    def test_local_ipv4_from_psutil_and_cache(self):
        class _Addr:
            family = socket.AF_INET
            address = "192.168.1.50"

        class _Addr6:
            family = socket.AF_INET6
            address = "::1"

        fake = self._fake_psutil(
            {
                "Loopback Pseudo-Interface 1": [_Addr()],
                "Ethernet": [_Addr(), _Addr6()],
                "Ethernet 2": [types.SimpleNamespace(family=socket.AF_INET, address="")],
            }
        )
        with mock.patch.dict(sys.modules, {"psutil": fake}):
            values = remote.local_ipv4_addresses()
        self.assertIn("192.168.1.50", values)
        self.assertIn("127.0.0.1", values)
        # 缓存命中：第二次即使 psutil 炸掉也返回缓存
        with mock.patch.dict(sys.modules, {"psutil": self._fake_psutil({})}):
            self.assertIn("192.168.1.50", remote.local_ipv4_addresses())

    def test_local_ipv4_falls_back_to_hostname(self):
        fake = self._fake_psutil({})
        with (
            mock.patch.dict(sys.modules, {"psutil": fake}),
            mock.patch.object(remote.socket, "gethostbyname", return_value="10.9.8.7"),
        ):
            values = remote.local_ipv4_addresses()
        self.assertIn("10.9.8.7", values)

    def test_local_ipv4_hostname_oserror(self):
        fake = self._fake_psutil({})
        with (
            mock.patch.dict(sys.modules, {"psutil": fake}),
            mock.patch.object(remote.socket, "gethostbyname", side_effect=OSError("offline")),
        ):
            self.assertEqual(remote.local_ipv4_addresses(), {"127.0.0.1"})

    def test_local_ipv4_psutil_failure(self):
        fake = types.SimpleNamespace(net_if_addrs=mock.Mock(side_effect=RuntimeError("boom")))
        with (
            mock.patch.dict(sys.modules, {"psutil": fake}),
            mock.patch.object(remote.socket, "gethostbyname", return_value="10.1.1.1"),
        ):
            self.assertIn("10.1.1.1", remote.local_ipv4_addresses())

    def test_primary_lan_ip_ranking(self):
        def pick(values):
            with mock.patch.object(remote, "local_ipv4_addresses", return_value=set(values)):
                return remote.primary_lan_ip()

        self.assertEqual(pick({"127.0.0.1", "169.254.1.1", "192.168.1.10"}), "192.168.1.10")
        self.assertEqual(pick({"127.0.0.1", "169.254.1.1", "172.20.0.2", "10.0.0.3"}), "10.0.0.3")
        self.assertEqual(pick({"127.0.0.1", "169.254.1.1", "8.8.8.8"}), "8.8.8.8")
        self.assertEqual(pick({"127.0.0.1", "169.254.1.1"}), "169.254.1.1")
        self.assertEqual(pick({"127.0.0.1", "0.0.0.0"}), "127.0.0.1")

    def test_private_172(self):
        self.assertTrue(remote._is_private_172("172.16.0.1"))
        self.assertTrue(remote._is_private_172("172.31.255.1"))
        self.assertFalse(remote._is_private_172("172.32.0.1"))
        self.assertFalse(remote._is_private_172("172.x.0.1"))
        self.assertFalse(remote._is_private_172("172"))
        self.assertFalse(remote._is_private_172("10.0.0.1"))

    def test_allowed_hostnames(self):
        with (
            mock.patch.object(remote, "enabled", return_value=False),
            mock.patch.object(remote, "local_ipv4_addresses", return_value={"127.0.0.1", "192.168.1.10"}),
        ):
            names = remote.allowed_hostnames()
        self.assertLessEqual({"127.0.0.1", "localhost", "::1"}, names)
        self.assertNotIn("192.168.1.10", names)
        with (
            mock.patch.object(remote, "enabled", return_value=True),
            mock.patch.object(remote, "local_ipv4_addresses", return_value={"127.0.0.1", "192.168.1.10"}),
            mock.patch.object(remote.socket, "gethostname", return_value=" MyHost "),
            mock.patch.object(remote, "extra_hosts", return_value={"ddns.example.com"}),
        ):
            names = remote.allowed_hostnames()
        self.assertIn("192.168.1.10", names)
        self.assertIn("myhost", names)
        self.assertIn("myhost.local", names)
        self.assertIn("ddns.example.com", names)

    def test_allowed_hostnames_empty_hostname(self):
        with (
            mock.patch.object(remote, "enabled", return_value=True),
            mock.patch.object(remote, "local_ipv4_addresses", return_value={"127.0.0.1"}),
            mock.patch.object(remote.socket, "gethostname", return_value="   "),
            mock.patch.object(remote, "extra_hosts", return_value=set()),
        ):
            self.assertLessEqual({"127.0.0.1", "localhost", "::1"}, remote.allowed_hostnames())

    def test_is_loopback_value(self):
        for value in ("127.0.0.1", "LOCALHOST", "::1", "::ffff:127.0.0.1", " 127.0.0.1 "):
            self.assertTrue(remote.is_loopback_value(value), value)
        for value in ("", "10.0.0.1", "not-an-ip"):
            self.assertFalse(remote.is_loopback_value(value), value)

    def test_session_store_lifecycle(self):
        store = remote.SessionStore(ttl=10, max_sessions=2)
        token = store.create("1.1.1.1")
        self.assertTrue(store.validate(token))
        self.assertFalse(store.validate(""))
        self.assertFalse(store.validate("missing"))
        store.revoke("")
        store.revoke(token)
        self.assertFalse(store.validate(token))
        a = store.create()
        b = store.create()
        c = store.create()
        self.assertEqual(store.count(), 2)
        self.assertFalse(store.validate(a))
        self.assertTrue(store.validate(b))
        self.assertTrue(store.validate(c))
        store.clear()
        self.assertEqual(store.count(), 0)

    def test_session_expiry_and_purge(self):
        store = remote.SessionStore(ttl=10, max_sessions=5)
        token = store.create()
        base = time.time()
        with mock.patch.object(remote.time, "time", return_value=base + 11):
            self.assertFalse(store.validate(token))
        first = store.create()
        with mock.patch.object(remote.time, "time", return_value=base + 100):
            store.create()  # 触发 purge
        self.assertEqual(store.count(), 1)
        self.assertFalse(store.validate(first))

    def test_session_validate_extends_ttl(self):
        store = remote.SessionStore(ttl=10, max_sessions=5)
        token = store.create()
        base = time.time()
        with mock.patch.object(remote.time, "time", return_value=base + 5):
            self.assertTrue(store.validate(token))
        with mock.patch.object(remote.time, "time", return_value=base + 14):
            self.assertTrue(store.validate(token))
        with mock.patch.object(remote.time, "time", return_value=base + 26):
            self.assertFalse(store.validate(token))

    def test_login_limiter(self):
        limiter = remote.LoginLimiter(window=60, max_failures=3, lock=30)
        self.assertEqual(limiter.retry_after("1.1.1.1"), 0.0)
        limiter.record_failure("1.1.1.1")
        limiter.record_failure("1.1.1.1")
        self.assertEqual(limiter.retry_after("1.1.1.1"), 0.0)
        locked = limiter.record_failure("1.1.1.1")
        self.assertEqual(locked, 30)
        self.assertGreater(limiter.retry_after("1.1.1.1"), 0)
        limiter.record_success("1.1.1.1")
        self.assertEqual(limiter.retry_after("1.1.1.1"), 0.0)

    def test_login_limiter_window_and_expiry(self):
        limiter = remote.LoginLimiter(window=1.0, max_failures=2, lock=30)
        base = time.time()
        with mock.patch.object(remote.time, "time", return_value=base):
            limiter.record_failure("2.2.2.2")
        with mock.patch.object(remote.time, "time", return_value=base + 5):
            self.assertEqual(limiter.record_failure("2.2.2.2"), 0.0)  # 旧记录被窗口清掉
            self.assertEqual(limiter.record_failure("2.2.2.2"), 30.0)
            self.assertGreater(limiter.retry_after("2.2.2.2"), 0)
        with mock.patch.object(remote.time, "time", return_value=base + 100):
            self.assertEqual(limiter.retry_after("2.2.2.2"), 0.0)

    def test_cookies(self):
        self.assertEqual(remote.parse_cookie("a=1; mbes_session=abc; b=2"), "abc")
        self.assertEqual(remote.parse_cookie("mbes_session=  spaced  "), "spaced")
        self.assertEqual(remote.parse_cookie("other=1"), "")
        self.assertEqual(remote.parse_cookie(""), "")
        value = remote.session_cookie("tok")
        for flag in ("HttpOnly", "SameSite=Strict", "Path=/", "Max-Age=43200"):
            self.assertIn(flag, value)
        self.assertIn("Max-Age=0", remote.clear_cookie())

    def test_audit(self):
        with mock.patch("bedrock_server_manager.security.audit_log") as audit_log:
            remote.audit("REMOTE_LOGIN_OK", "ip=1.2.3.4")
            audit_log.assert_called_once_with("REMOTE_LOGIN_OK", "ip=1.2.3.4")
        with mock.patch("bedrock_server_manager.security.audit_log", side_effect=RuntimeError("disk")):
            remote.audit("REMOTE_LOGIN_OK", "ip=1.2.3.4")  # 不抛出


# =========================================================================== upnp


DESCRIPTION = """<?xml version="1.0"?>
<root xmlns="urn:schemas-upnp-org:device-1-0">
  <device>
    <friendlyName>Test Router</friendlyName>
    <serviceList>
      <service>
        <serviceType>urn:schemas-upnp-org:service:WANIPConnection:1</serviceType>
        <controlURL>/ctl/IPConn</controlURL>
      </service>
    </serviceList>
  </device>
</root>"""

SERVICE = "urn:schemas-upnp-org:service:WANIPConnection:1"
GATEWAY = {
    "control_url": "http://192.168.1.1:5000/ctl/IPConn",
    "service_type": SERVICE,
    "device_name": "Test Router",
    "external_ip": "203.0.113.7",
}


class TestUpnpParsing(unittest.TestCase):
    def test_local_name_and_find_text(self):
        self.assertEqual(upnp._local_name("{ns}tag"), "tag")
        self.assertEqual(upnp._local_name("tag"), "tag")
        root = upnp.safe_fromstring("<root><a>1</a><b>  </b><b>2</b></root>")
        self.assertEqual(upnp._find_text(root, "b"), "2")
        self.assertEqual(upnp._find_text(root, "missing"), "")

    def test_safe_fromstring_variants(self):
        node = upnp.safe_fromstring('<root a="1"><child>text<sub>more</sub></child></root>')
        self.assertEqual(node.tag, "root")
        self.assertEqual(node.attrib["a"], "1")
        with self.assertRaises(upnp.UpnpError):
            upnp.safe_fromstring(123)
        with self.assertRaises(upnp.UpnpError):
            upnp.safe_fromstring("<root>" + "x" * upnp.MAX_XML_BYTES + "</root>")
        with self.assertRaises(upnp.UpnpError):
            upnp.safe_fromstring("")
        with self.assertRaises(upnp.UpnpError):
            upnp.safe_fromstring("not xml")
        bomb = (
            '<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;">]><root>&lol2;</root>'
        )
        with self.assertRaises(upnp.UpnpError):
            upnp.safe_fromstring(bomb)

    def test_parse_ssdp_response(self):
        parsed = upnp.parse_ssdp_response(
            "HTTP/1.1 200 OK\r\n"
            "location: http://192.168.1.1/root.xml\r\n"
            "ST: urn:test\r\n"
            "SERVER: Linux UPnP/1.0\r\n"
            "garbage-line-without-colon\r\n\r\n"
        )
        self.assertEqual(parsed["location"], "http://192.168.1.1/root.xml")
        self.assertEqual(parsed["st"], "urn:test")
        self.assertIn("Linux", parsed["server"])
        self.assertEqual(parsed["usn"], "")

    def test_parse_description_direct_service(self):
        info = upnp.parse_description(DESCRIPTION, base_url="http://192.168.1.1:5000/root.xml")
        self.assertEqual(info["service_type"], SERVICE)
        self.assertEqual(info["control_url"], "http://192.168.1.1:5000/ctl/IPConn")
        self.assertEqual(info["device_name"], "Test Router")

    def test_parse_description_errors(self):
        with self.assertRaises(upnp.UpnpError):
            upnp.parse_description("<root><device><friendlyName>x</friendlyName></device></root>")
        # 有 serviceType 但 controlURL 为空：跳过，最终无可用服务
        with self.assertRaises(upnp.UpnpError):
            upnp.parse_description(
                f"<root><service><serviceType>{SERVICE}</serviceType><controlURL></controlURL></service></root>"
            )


class _FakeSsdpSocket:
    def __init__(self, responses, fail_sends=()):
        self.responses = list(responses)
        self.fail_sends = set(fail_sends)
        self.sent = []
        self.closed = False

    def setsockopt(self, *args):
        return None

    def settimeout(self, *args):
        return None

    def sendto(self, data, addr):
        index = len(self.sent)
        self.sent.append((data, addr))
        if index in self.fail_sends:
            raise OSError("no route")

    def recvfrom(self, size):
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def close(self):
        self.closed = True


class TestUpnpSsdp(unittest.TestCase):
    def test_ssdp_search_collects_unique_locations(self):
        good = b"HTTP/1.1 200 OK\r\nLOCATION: http://192.168.1.1/root.xml\r\nST: st1\r\n\r\n"
        duplicate = b"HTTP/1.1 200 OK\r\nLOCATION: http://192.168.1.1/root.xml\r\nST: st2\r\n\r\n"
        no_location = b"HTTP/1.1 200 OK\r\nSERVER: x\r\n\r\n"
        notify = b"NOTIFY * HTTP/1.1\r\nLOCATION: http://192.168.1.2/root.xml\r\n\r\n"
        fake = _FakeSsdpSocket(
            [
                TimeoutError(),
                (good, ("192.168.1.1", 1900)),
                (duplicate, ("192.168.1.1", 1900)),
                (no_location, ("192.168.1.3", 1900)),
                (notify, ("192.168.1.4", 1900)),
                OSError("done"),
            ],
            fail_sends={1},
        )
        with mock.patch.object(upnp.socket, "socket", return_value=fake):
            results = upnp.ssdp_search(timeout=5)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["location"], "http://192.168.1.1/root.xml")
        self.assertEqual(results[0]["source"], "192.168.1.1")
        self.assertTrue(fake.closed)
        self.assertEqual(len(fake.sent), len(upnp.SSDP_TARGETS))

    def test_ssdp_search_send_errors_are_ignored(self):
        fake = _FakeSsdpSocket([OSError("done")], fail_sends={0, 1, 2})
        with mock.patch.object(upnp.socket, "socket", return_value=fake):
            self.assertEqual(upnp.ssdp_search(timeout=5), [])


class TestUpnpDiscovery(unittest.TestCase):
    def test_fetch(self):
        response = _FakeResponse("<root>ok</root>")
        with mock.patch.object(upnp.urllib.request, "urlopen", return_value=response) as urlopen:
            self.assertEqual(upnp._fetch("http://192.168.1.1/root.xml", timeout=1), "<root>ok</root>")
        request = urlopen.call_args[0][0]
        self.assertEqual(request.get_header("User-agent"), upnp._USER_AGENT)

    def test_discover_no_responses(self):
        with mock.patch.object(upnp, "ssdp_search", return_value=[]), self.assertRaises(upnp.NoGatewayError):
            upnp.discover()

    def test_discover_skips_bad_response_then_uses_good(self):
        responses = [
            {"location": "http://bad/root.xml", "source": "10.0.0.1"},
            {"location": "http://good/root.xml", "source": "10.0.0.2"},
        ]
        with (
            mock.patch.object(upnp, "ssdp_search", return_value=responses),
            mock.patch.object(upnp, "_fetch", side_effect=[upnp.UpnpError("bad xml"), DESCRIPTION]),
            mock.patch.object(upnp, "get_external_ip", return_value="203.0.113.7"),
        ):
            info = upnp.discover()
        self.assertEqual(info["source"], "10.0.0.2")
        self.assertEqual(info["external_ip"], "203.0.113.7")

    def test_discover_external_ip_failure_kept_empty(self):
        with (
            mock.patch.object(upnp, "ssdp_search", return_value=[{"location": "http://good", "source": "x"}]),
            mock.patch.object(upnp, "_fetch", return_value=DESCRIPTION),
            mock.patch.object(upnp, "get_external_ip", side_effect=upnp.UpnpError("no ip")),
        ):
            self.assertEqual(upnp.discover()["external_ip"], "")

    def test_discover_all_fail(self):
        responses = [{"location": "http://bad/root.xml", "source": "10.0.0.1"}]
        with (
            mock.patch.object(upnp, "ssdp_search", return_value=responses),
            mock.patch.object(upnp, "_fetch", side_effect=OSError("unreachable")),
        ):
            with self.assertRaises(upnp.NoGatewayError) as ctx:
                upnp.discover()
        self.assertIn("unreachable", str(ctx.exception))

    def test_gateway_cache_hit_and_error(self):
        upnp._gateway_cache.update({"time": time.time(), "info": dict(GATEWAY), "error": ""})
        try:
            info = upnp.gateway()
            self.assertEqual(info["control_url"], GATEWAY["control_url"])
            info["control_url"] = "mutated"
            self.assertEqual(upnp.gateway()["control_url"], GATEWAY["control_url"])
            upnp._gateway_cache.update({"time": time.time(), "info": None, "error": "cached failure"})
            with self.assertRaises(upnp.NoGatewayError) as ctx:
                upnp.gateway()
            self.assertIn("cached failure", str(ctx.exception))
        finally:
            upnp._gateway_cache.update({"time": 0.0, "info": None, "error": ""})

    def test_gateway_force_and_failure_cached(self):
        upnp._gateway_cache.update({"time": 0.0, "info": None, "error": ""})
        try:
            with mock.patch.object(upnp, "discover", return_value=dict(GATEWAY)):
                self.assertEqual(upnp.gateway(force=True)["service_type"], SERVICE)
            with mock.patch.object(upnp, "discover", side_effect=upnp.UpnpError("no gateway")):
                with self.assertRaises(upnp.UpnpError):
                    upnp.gateway(force=True)
            self.assertEqual(upnp._gateway_cache["error"], "no gateway")
        finally:
            upnp._gateway_cache.update({"time": 0.0, "info": None, "error": ""})

    def test_mapping_state(self):
        with mock.patch.object(upnp, "gateway", side_effect=upnp.UpnpError("offline")):
            state = upnp.mapping_state(19132)
        self.assertFalse(state["available"])
        self.assertIn("offline", state["error"])

        with (
            mock.patch.object(upnp, "gateway", return_value=dict(GATEWAY)),
            mock.patch.object(upnp, "get_mapping", side_effect=upnp.UpnpError("soap down")),
        ):
            self.assertIn("soap down", upnp.mapping_state(19132)["error"])

        with (
            mock.patch.object(upnp, "gateway", return_value=dict(GATEWAY)),
            mock.patch.object(upnp, "get_mapping", return_value=None),
        ):
            state = upnp.mapping_state(19132)
        self.assertTrue(state["available"])
        self.assertFalse(state["mapped"])

        with (
            mock.patch.object(upnp, "gateway", return_value=dict(GATEWAY)),
            mock.patch.object(
                upnp,
                "get_mapping",
                return_value={"enabled": True, "internal_client": "192.168.1.10"},
            ),
        ):
            state = upnp.mapping_state(19132)
        self.assertTrue(state["mapped"])
        self.assertEqual(state["internal_client"], "192.168.1.10")


class TestUpnpSoap(unittest.TestCase):
    def test_soap_success(self):
        response = _FakeResponse("<root><NewExternalIPAddress>1.2.3.4</NewExternalIPAddress></root>")
        with mock.patch.object(upnp.urllib.request, "urlopen", return_value=response) as urlopen:
            root = upnp._soap(GATEWAY["control_url"], SERVICE, "GetExternalIPAddress", {})
        self.assertEqual(upnp._find_text(root, "NewExternalIPAddress"), "1.2.3.4")
        request = urlopen.call_args[0][0]
        self.assertEqual(request.get_method(), "POST")
        self.assertIn("GetExternalIPAddress", request.get_header("Soapaction"))

    def test_soap_upnp_fault_and_http_error(self):
        fault = (
            "<s:Envelope><s:Body><s:Fault><detail><UPnPError>"
            "<errorCode>714</errorCode><errorDescription>NoSuchEntryInArray</errorDescription>"
            "</UPnPError></detail></s:Fault></s:Body></s:Envelope>"
        )
        with mock.patch.object(upnp.urllib.request, "urlopen", side_effect=_HttpError(500, fault.encode())):
            with self.assertRaises(upnp.UpnpError) as ctx:
                upnp._soap(GATEWAY["control_url"], SERVICE, "X", {})
        self.assertIn("714", str(ctx.exception))
        self.assertIn("NoSuchEntryInArray", str(ctx.exception))

        with mock.patch.object(upnp.urllib.request, "urlopen", side_effect=_HttpError(500, b"plain failure")):
            with self.assertRaises(upnp.UpnpError) as ctx:
                upnp._soap(GATEWAY["control_url"], SERVICE, "X", {})
        self.assertIn("HTTP 500", str(ctx.exception))

    def test_soap_transport_errors(self):
        for error in (urllib.error.URLError("down"), TimeoutError("slow"), OSError("reset")):
            with mock.patch.object(upnp.urllib.request, "urlopen", side_effect=error):
                with self.assertRaises(upnp.UpnpError) as ctx:
                    upnp._soap(GATEWAY["control_url"], SERVICE, "X", {})
            self.assertIn("无法连接网关控制地址", str(ctx.exception))

    def test_require_and_validate(self):
        with mock.patch.object(upnp, "discover", return_value=dict(GATEWAY)):
            self.assertEqual(upnp._require_gateway(None)["service_type"], SERVICE)
        self.assertIs(upnp._require_gateway(GATEWAY), GATEWAY)
        for port in (0, 70000, "19132", None):
            with self.assertRaises(upnp.UpnpError):
                upnp._validate(port, "UDP")
        with self.assertRaises(upnp.UpnpError):
            upnp._validate(19132, "SCTP")

    def test_get_external_ip_variants(self):
        with mock.patch.object(
            upnp,
            "_soap",
            return_value=upnp.safe_fromstring("<r><NewExternalIPAddress>0.0.0.0</NewExternalIPAddress></r>"),
        ):
            self.assertEqual(upnp.get_external_ip(GATEWAY), "")
        with mock.patch.object(
            upnp,
            "_soap",
            return_value=upnp.safe_fromstring("<r><NewExternalIPAddress>203.0.113.9</NewExternalIPAddress></r>"),
        ):
            self.assertEqual(upnp.get_external_ip(GATEWAY), "203.0.113.9")
        with (
            mock.patch.object(upnp, "discover", return_value=dict(GATEWAY)),
            mock.patch.object(upnp, "_soap", return_value=upnp.safe_fromstring("<r/>")),
        ):
            self.assertEqual(upnp.get_external_ip(None), "")

    def test_add_mapping(self):
        with self.assertRaises(upnp.UpnpError):
            upnp.add_mapping(19132, "not-an-ip", gateway=GATEWAY)
        with (
            mock.patch.object(upnp, "_soap", return_value=upnp.safe_fromstring("<r/>")) as soap,
            mock.patch.object(upnp, "get_external_ip", return_value="203.0.113.7"),
        ):
            result = upnp.add_mapping(19132, "192.168.1.10", gateway=GATEWAY)
        self.assertEqual(result["protocol"], "UDP")
        self.assertEqual(result["external_ip"], "203.0.113.7")
        args = soap.call_args[0][3]
        self.assertEqual(args["NewExternalPort"], 19132)
        self.assertEqual(args["NewInternalClient"], "192.168.1.10")
        with (
            mock.patch.object(upnp, "discover", return_value=dict(GATEWAY)),
            mock.patch.object(upnp, "_soap", return_value=upnp.safe_fromstring("<r/>")),
            mock.patch.object(upnp, "get_external_ip", return_value=""),
        ):
            self.assertEqual(upnp.add_mapping(19132, "192.168.1.10", protocol="tcp")["protocol"], "TCP")

    def test_delete_mapping(self):
        with mock.patch.object(upnp, "_soap", return_value=upnp.safe_fromstring("<r/>")) as soap:
            upnp.delete_mapping(19132, gateway=GATEWAY)
        self.assertEqual(soap.call_args[0][2], "DeletePortMapping")
        with (
            mock.patch.object(upnp, "discover", return_value=dict(GATEWAY)),
            mock.patch.object(upnp, "_soap", return_value=upnp.safe_fromstring("<r/>")),
        ):
            upnp.delete_mapping(19132, protocol="TCP", gateway=None)

    def test_get_mapping(self):
        xml = (
            "<r><NewInternalClient>192.168.1.10</NewInternalClient>"
            "<NewInternalPort>19132</NewInternalPort><NewEnabled>1</NewEnabled>"
            "<NewPortMappingDescription>MbesBeacon-BDS</NewPortMappingDescription></r>"
        )
        with mock.patch.object(upnp, "_soap", return_value=upnp.safe_fromstring(xml)):
            mapping = upnp.get_mapping(19132, gateway=GATEWAY)
        self.assertEqual(mapping["internal_client"], "192.168.1.10")
        self.assertTrue(mapping["enabled"])
        for message in ("网关返回错误 714: NoSuchEntryInArray", "网关返回错误 501: Entry not found"):
            with mock.patch.object(upnp, "_soap", side_effect=upnp.UpnpError(message)):
                self.assertIsNone(upnp.get_mapping(19132, gateway=GATEWAY))
        with mock.patch.object(upnp, "_soap", side_effect=upnp.UpnpError("网关 HTTP 500")):
            with self.assertRaises(upnp.UpnpError):
                upnp.get_mapping(19132, gateway=GATEWAY)
        with (
            mock.patch.object(upnp, "discover", return_value=dict(GATEWAY)),
            mock.patch.object(upnp, "_soap", return_value=upnp.safe_fromstring("<r><NewEnabled>true</NewEnabled></r>")),
        ):
            self.assertTrue(upnp.get_mapping(19132, gateway=None)["enabled"])

    def test_placeholder_and_ipv4(self):
        self.assertTrue(upnp.is_placeholder_ip("0.0.0.0"))
        self.assertTrue(upnp.is_placeholder_ip(""))
        self.assertFalse(upnp.is_placeholder_ip(" 203.0.113.7 "))
        self.assertTrue(upnp.looks_like_ipv4("203.0.113.7"))
        self.assertFalse(upnp.looks_like_ipv4("example.com"))


# =========================================================================== builtin_tunnel


class TestBuiltinPlatform(_SettingsCase):
    def test_platform_and_asset_matrix(self):
        cases = [
            (True, "AMD64", ("windows", "x86_64"), "playit-windows-x86_64-signed.exe"),
            (True, "i686", ("windows", "x86"), "playit-windows-x86-signed.exe"),
            (True, "ARM64", ("windows", "aarch64"), None),
            (False, "x86_64", ("linux", "amd64"), "playit-cli-linux-amd64"),
            (False, "aarch64", ("linux", "arm64"), "playit-cli-linux-arm64"),
            (False, "arm", ("linux", "arm"), None),
            (False, "mips", ("linux", "mips"), None),
        ]
        for is_win, machine, tag, playit_name in cases:
            with (
                mock.patch.object(bt, "IS_WINDOWS", is_win),
                mock.patch.object(bt.platform, "machine", return_value=machine),
            ):
                self.assertEqual(bt.platform_tag(), tag, (is_win, machine))
                if playit_name:
                    self.assertEqual(bt.playit_asset_names()[0], playit_name)
                    self.assertEqual(bt._exe("frpc"), "frpc.exe" if is_win else "frpc")
                    self.assertEqual(bt._exe("frpc") in bt.frp_asset_name("1.0"), False)
                else:
                    with self.assertRaises(bt.BuiltinTunnelError):
                        bt.playit_asset_names()

    def test_frp_asset_names(self):
        with mock.patch.object(bt, "IS_WINDOWS", True), mock.patch.object(bt.platform, "machine", return_value="AMD64"):
            self.assertEqual(bt.frp_asset_name("0.72.0"), "frp_0.72.0_windows_amd64.zip")
        with mock.patch.object(bt, "IS_WINDOWS", True), mock.patch.object(bt.platform, "machine", return_value="i686"):
            self.assertEqual(bt.frp_asset_name("0.72.0"), "frp_0.72.0_windows_386.zip")
        with (
            mock.patch.object(bt, "IS_WINDOWS", False),
            mock.patch.object(bt.platform, "machine", return_value="x86_64"),
        ):
            self.assertEqual(bt.frp_asset_name("0.72.0"), "frp_0.72.0_linux_amd64.tar.gz")
        with (
            mock.patch.object(bt, "IS_WINDOWS", False),
            mock.patch.object(bt.platform, "machine", return_value="arm64"),
        ):
            self.assertEqual(bt.frp_asset_name("0.72.0"), "frp_0.72.0_linux_arm64.tar.gz")
        with mock.patch.object(bt, "IS_WINDOWS", False), mock.patch.object(bt.platform, "machine", return_value="arm"):
            self.assertEqual(bt.frp_asset_name("0.72.0"), "frp_0.72.0_linux_arm.tar.gz")
        with mock.patch.object(bt, "IS_WINDOWS", True), mock.patch.object(bt.platform, "machine", return_value="ARM64"):
            with self.assertRaises(bt.BuiltinTunnelError):
                bt.frp_asset_name("0.72.0")
        with mock.patch.object(bt, "IS_WINDOWS", False), mock.patch.object(bt.platform, "machine", return_value="mips"):
            with self.assertRaises(bt.BuiltinTunnelError):
                bt.frp_asset_name("0.72.0")

    def test_github_headers(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertNotIn("Authorization", bt.github_headers())
        with mock.patch.dict(os.environ, {"GITHUB_TOKEN": "abc"}, clear=True):
            self.assertEqual(bt.github_headers()["Authorization"], "Bearer abc")
        with mock.patch.dict(os.environ, {"GH_TOKEN": "xyz"}, clear=True):
            self.assertEqual(bt.github_headers()["Authorization"], "Bearer xyz")

    def test_secret_candidates_linux(self):
        with (
            mock.patch.object(bt, "IS_WINDOWS", False),
            mock.patch.object(bt.os.path, "expanduser", return_value="/home/user"),
        ):
            candidates = bt.secret_candidates()
        self.assertEqual(candidates[0], "/etc/playit/playit.toml")
        self.assertEqual(len(candidates), 3)
        self.assertIn(".config", candidates[1])

    def test_asset_helpers(self):
        self.assertEqual(bt.asset_digest({"digest": "sha256:ABCDEF"}), "abcdef")
        self.assertEqual(bt.asset_digest({"digest": "md5:xx"}), "")
        self.assertEqual(bt.asset_digest({}), "")
        release = {"assets": [{"name": "a"}, {"name": "b"}]}
        self.assertEqual(bt.find_asset(release, "a")["name"], "a")
        with self.assertRaises(bt.BuiltinTunnelError):
            bt.find_asset(release, "c")
        with self.assertRaises(bt.BuiltinTunnelError):
            bt.find_asset({}, "a")


class TestBuiltinRelease(_SettingsCase):
    def setUp(self):
        super().setUp()
        bt._release_cache.clear()

    def tearDown(self):
        bt._release_cache.clear()
        super().tearDown()

    def test_fetch_release_success_cache_and_force(self):
        payload = json.dumps({"assets": [{"name": "a"}], "tag_name": "v1"}).encode()
        with mock.patch.object(
            bt.urllib.request, "urlopen", side_effect=[_FakeResponse(payload), _FakeResponse(payload)]
        ) as urlopen:
            first = bt.fetch_release("owner/repo")
            second = bt.fetch_release("owner/repo")
            forced = bt.fetch_release("owner/repo", force=True)
        self.assertIs(first, second)
        self.assertEqual(forced["tag_name"], "v1")
        self.assertEqual(urlopen.call_count, 2)

    def test_fetch_release_errors(self):
        with mock.patch.object(bt.urllib.request, "urlopen", side_effect=urllib.error.URLError("offline")):
            with self.assertRaises(bt.BuiltinTunnelError):
                bt.fetch_release("owner/repo")
        with mock.patch.object(bt.urllib.request, "urlopen", return_value=_FakeResponse(b"not json")):
            with self.assertRaises(bt.BuiltinTunnelError):
                bt.fetch_release("owner/repo")
        with mock.patch.object(bt.urllib.request, "urlopen", return_value=_FakeResponse(b'{"no_assets": 1}')):
            with self.assertRaises(bt.BuiltinTunnelError):
                bt.fetch_release("owner/repo")
        with mock.patch.object(bt.urllib.request, "urlopen", return_value=_FakeResponse(b"[]")):
            with self.assertRaises(bt.BuiltinTunnelError):
                bt.fetch_release("owner/repo")

    def test_download_file_success_and_checksum(self):
        data = b"hello-builtin"
        expected = hashlib.sha256(data).hexdigest()
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(bt.urllib.request, "urlopen", return_value=_FakeResponse(data)):
                path = bt.download_file("http://x/file", tmp, expected)
            with open(path, "rb") as handle:
                self.assertEqual(handle.read(), data)

    def test_download_file_checksum_failure_cleans_temp(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(bt.urllib.request, "urlopen", return_value=_FakeResponse(b"data")):
                with self.assertRaises(bt.BuiltinTunnelError):
                    bt.download_file("http://x/file", tmp, "0" * 64)
            self.assertEqual(os.listdir(tmp), [])

    def test_download_file_oversize_and_transport_error(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(bt, "MAX_DOWNLOAD_BYTES", 4):
            with mock.patch.object(bt.urllib.request, "urlopen", return_value=_FakeResponse(b"123456789")):
                with self.assertRaises(bt.BuiltinTunnelError):
                    bt.download_file("http://x/file", tmp)
            self.assertEqual(os.listdir(tmp), [])
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(bt.urllib.request, "urlopen", side_effect=urllib.error.URLError("down")):
                with self.assertRaises(urllib.error.URLError):
                    bt.download_file("http://x/file", tmp)
            self.assertEqual(os.listdir(tmp), [])

    def test_atomic_move_and_meta(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = _write(os.path.join(tmp, "tmp.bin"), "new")
            target = _write(os.path.join(tmp, "sub", "target.bin"), "old")
            bt._atomic_move(tmp_path, target)
            with open(target, encoding="utf-8") as handle:
                self.assertEqual(handle.read(), "new")
            self.assertFalse(os.path.exists(tmp_path))
            bt._write_meta(os.path.join(tmp, "meta"), {"version": "1.2.3"})
            self.assertEqual(bt._read_meta(os.path.join(tmp, "meta"))["version"], "1.2.3")
            self.assertEqual(bt._read_meta(os.path.join(tmp, "missing")), {})
            _write(os.path.join(tmp, "badmeta", "meta.json"), "not json")
            self.assertEqual(bt._read_meta(os.path.join(tmp, "badmeta")), {})
            _write(os.path.join(tmp, "listmeta", "meta.json"), "[]")
            self.assertEqual(bt._read_meta(os.path.join(tmp, "listmeta")), {})

    def test_make_executable(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(os.path.join(tmp, "tool"), "x")
            with mock.patch.object(bt, "IS_WINDOWS", True), mock.patch.object(bt.os, "chmod") as chmod:
                bt._make_executable(path)
                chmod.assert_not_called()
            with mock.patch.object(bt, "IS_WINDOWS", False), mock.patch.object(bt.os, "chmod") as chmod:
                bt._make_executable(path)
                chmod.assert_called_once()

    def test_install_playit(self):
        release = {
            "tag_name": "v1.2.3",
            "assets": [
                {
                    "name": "playit-windows-x86_64-signed.exe",
                    "browser_download_url": "http://x/cli",
                    "digest": "sha256:aa",
                },
                {
                    "name": "playitd-windows-x86_64-signed.exe",
                    "browser_download_url": "http://x/daemon",
                    "digest": "sha256:bb",
                },
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            calls = []

            def fake_download(url, dest_dir, expected="", timeout=0):
                path = os.path.join(dest_dir, "tmp-" + url.rsplit("/", 1)[-1])
                _write(path, url.encode(), binary=True)
                calls.append((url, expected))
                return path

            progress = []
            with (
                mock.patch.object(bt, "IS_WINDOWS", True),
                mock.patch.object(bt.platform, "machine", return_value="AMD64"),
                mock.patch.object(bt, "PLAYIT_DIR", tmp),
                mock.patch.object(bt, "fetch_release", return_value=release),
                mock.patch.object(bt, "download_file", side_effect=fake_download),
            ):
                result = bt.install_playit(progress.append)
            self.assertTrue(result["installed"])
            self.assertEqual(result["version"], "v1.2.3")
            self.assertEqual(len(calls), 2)
            self.assertEqual(calls[0][1], "aa")
            self.assertTrue(os.path.isfile(os.path.join(tmp, "playit.exe")))
            self.assertTrue(os.path.isfile(os.path.join(tmp, "playitd.exe")))
            self.assertEqual(
                progress, ["下载 playit-windows-x86_64-signed.exe …", "下载 playitd-windows-x86_64-signed.exe …"]
            )

    def test_install_playit_missing_asset_and_bad_digest(self):
        release = {
            "tag_name": "v1",
            "assets": [{"name": "playit-windows-x86_64-signed.exe", "browser_download_url": "http://x"}],
        }
        with tempfile.TemporaryDirectory() as tmp:

            def fake_download(url, dest_dir, expected="", timeout=0):
                path = os.path.join(dest_dir, "tmp-cli")
                _write(path, b"cli", binary=True)
                return path

            with (
                mock.patch.object(bt, "IS_WINDOWS", True),
                mock.patch.object(bt.platform, "machine", return_value="AMD64"),
                mock.patch.object(bt, "PLAYIT_DIR", tmp),
                mock.patch.object(bt, "fetch_release", return_value=release),
                mock.patch.object(bt, "download_file", side_effect=fake_download),
            ):
                with self.assertRaises(bt.BuiltinTunnelError):
                    bt.install_playit()
        # 摘要校验失败：使用真实 download_file + 假 urlopen
        release = {
            "tag_name": "v1",
            "assets": [
                {
                    "name": "playit-windows-x86_64-signed.exe",
                    "browser_download_url": "http://x/cli",
                    "digest": "sha256:" + "0" * 64,
                }
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            with (
                mock.patch.object(bt, "IS_WINDOWS", True),
                mock.patch.object(bt.platform, "machine", return_value="AMD64"),
                mock.patch.object(bt, "PLAYIT_DIR", tmp),
                mock.patch.object(bt, "fetch_release", return_value=release),
                mock.patch.object(bt.urllib.request, "urlopen", return_value=_FakeResponse(b"payload")),
            ):
                with self.assertRaises(bt.BuiltinTunnelError) as ctx:
                    bt.install_playit()
            self.assertIn("SHA256", str(ctx.exception))

    def test_install_playit_temp_file_cleanup(self):
        release = {
            "tag_name": "v1",
            "assets": [
                {"name": "playit-windows-x86_64-signed.exe", "browser_download_url": "http://x/cli"},
                {"name": "playitd-windows-x86_64-signed.exe", "browser_download_url": "http://x/daemon"},
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:

            def fake_download(url, dest_dir, expected="", timeout=0):
                return _write(os.path.join(dest_dir, "tmp-" + url.rsplit("/", 1)[-1]), b"x", binary=True)

            with (
                mock.patch.object(bt, "IS_WINDOWS", True),
                mock.patch.object(bt.platform, "machine", return_value="AMD64"),
                mock.patch.object(bt, "PLAYIT_DIR", tmp),
                mock.patch.object(bt, "fetch_release", return_value=release),
                mock.patch.object(bt, "download_file", side_effect=fake_download),
                mock.patch.object(bt, "_atomic_move"),  # 不移动：让 finally 负责清理临时文件
                mock.patch.object(bt, "_make_executable"),
            ):
                bt.install_playit()
            self.assertEqual(sorted(os.listdir(tmp)), ["meta.json"])

    def test_playit_installed_and_version(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(bt, "PLAYIT_DIR", tmp):
            self.assertFalse(bt.playit_installed())
            _write(os.path.join(tmp, "playit.exe"))
            _write(os.path.join(tmp, "playitd.exe"))
            self.assertTrue(bt.playit_installed())
            bt._write_meta(tmp, {"version": "9.9"})
            self.assertEqual(bt.PlayitManager().installed_version(), "9.9")
        self.assertEqual(bt.PlayitManager().installed_version(), "")


class TestBuiltinExtraction(_SettingsCase):
    def test_extract_frpc_from_zip(self):
        wanted = bt._exe("frpc")
        with tempfile.TemporaryDirectory() as tmp:
            archive = os.path.join(tmp, "pkg.zip")
            with zipfile.ZipFile(archive, "w") as zf:
                zf.writestr(f"frp_1/{wanted}", b"ZIP-FRPC")
                zf.writestr(f"frp_1/frps{'.exe' if bt.IS_WINDOWS else ''}", b"FRPS")
                zf.writestr("readme.txt", b"hi")
            target = bt.extract_frpc(archive, os.path.join(tmp, "out"))
            self.assertTrue(target.endswith(wanted))
            with open(target, "rb") as handle:
                self.assertEqual(handle.read(), b"ZIP-FRPC")

    def test_extract_frpc_from_tar_and_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = os.path.join(tmp, "pkg.tar.gz")
            with tarfile.open(archive, "w:gz") as tf:
                info = tarfile.TarInfo("frp_1/frpc")
                info.size = len(b"TAR-FRPC")
                tf.addfile(info, io.BytesIO(b"TAR-FRPC"))
                other = tarfile.TarInfo("frp_1/readme.txt")
                other.size = 1
                tf.addfile(other, io.BytesIO(b"x"))
            target_dir = os.path.join(tmp, "out")
            with mock.patch.object(bt, "IS_WINDOWS", False):
                target = bt.extract_frpc(archive, target_dir)
            self.assertEqual(os.path.basename(target), "frpc")
            with open(target, "rb") as handle:
                self.assertEqual(handle.read(), b"TAR-FRPC")
        with tempfile.TemporaryDirectory() as tmp:
            archive = os.path.join(tmp, "pkg.zip")
            with zipfile.ZipFile(archive, "w") as zf:
                zf.writestr("readme.txt", b"hi")
            with self.assertRaises(bt.BuiltinTunnelError):
                bt.extract_frpc(archive, os.path.join(tmp, "out"))

    def test_extract_frpc_tar_without_frpc(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = os.path.join(tmp, "pkg.tar")
            with tarfile.open(archive, "w") as tf:
                info = tarfile.TarInfo("readme.txt")
                info.size = 1
                tf.addfile(info, io.BytesIO(b"x"))
            with self.assertRaises(bt.BuiltinTunnelError):
                bt.extract_frpc(archive, os.path.join(tmp, "out"))

    def test_extract_frpc_tar_extractfile_none(self):
        member = types.SimpleNamespace(name=f"frp/{bt._exe('frpc')}", isfile=lambda: True)

        class _FakeTar:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def getmembers(self):
                return [member]

            def extractfile(self, item):
                return None

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(bt.tarfile, "open", return_value=_FakeTar()):
                with self.assertRaises(bt.BuiltinTunnelError):
                    bt.extract_frpc(os.path.join(tmp, "not-a-zip"), os.path.join(tmp, "out"))

    def test_install_frp(self):
        wanted = bt._exe("frpc")
        with tempfile.TemporaryDirectory() as tmp:
            archive = os.path.join(tmp, "download")
            with zipfile.ZipFile(archive, "w") as zf:
                zf.writestr(f"frp_1/{wanted}", b"FRPC")
            release = {
                "tag_name": "v0.72.0",
                "assets": [{"name": "frp.zip", "browser_download_url": "http://x", "digest": "sha256:aa"}],
            }
            progress = []
            with (
                mock.patch.object(bt, "FRP_DIR", tmp),
                mock.patch.object(bt, "frp_asset_name", return_value="frp.zip"),
                mock.patch.object(bt, "fetch_release", return_value=release),
                mock.patch.object(bt, "download_file", return_value=archive),
            ):
                result = bt.install_frp(progress.append)
                self.assertTrue(result["installed"])
                self.assertEqual(result["version"], "0.72.0")
                self.assertTrue(os.path.isfile(os.path.join(tmp, wanted)))
                self.assertEqual(progress, ["下载 frp.zip …"])
                self.assertTrue(bt.frp_installed())

    def test_install_frp_missing_asset(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(bt, "FRP_DIR", tmp),
            mock.patch.object(bt, "frp_asset_name", return_value="frp.zip"),
            mock.patch.object(bt, "fetch_release", return_value={"tag_name": "v1", "assets": []}),
        ):
            with self.assertRaises(bt.BuiltinTunnelError):
                bt.install_frp()

    def test_run_once(self):
        completed = types.SimpleNamespace(returncode=0, stdout="ok", stderr="")
        with mock.patch.object(bt.subprocess, "run", return_value=completed) as run:
            result = bt._run_once(["tool", "arg"], timeout=3, cwd="/tmp")
        self.assertIs(result, completed)
        self.assertEqual(run.call_args[0][0], ["tool", "arg"])
        self.assertEqual(run.call_args[1]["timeout"], 3)

    def test_line_buffer(self):
        buffer = bt._LineBuffer(limit=2)
        buffer.add("")
        buffer.add("[INFO] one")
        buffer.add("[INFO] two")
        buffer.add("[INFO] three")
        self.assertEqual(buffer.tail(), ["[INFO] two", "[INFO] three"])
        with mock.patch("bedrock_server_manager.tunnel.detect_address", return_value="host.playit.gg:25565"):
            buffer.add("[INFO] address available")
        self.assertEqual(buffer.address, "host.playit.gg:25565")
        buffer.add("another line")
        # 已检测到地址后不再重复检测
        self.assertEqual(buffer.address, "host.playit.gg:25565")

    def test_tail_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_path = os.path.join(tmp, "playitd.log")
            _write(log_path, "")
            buffer = bt._LineBuffer()
            stop = threading.Event()
            thread = threading.Thread(target=bt._tail_file, args=(log_path, buffer, stop), kwargs={"poll": 0.01})
            thread.start()
            try:
                with open(log_path, "a", encoding="utf-8") as handle:
                    handle.write("[INFO] tailed line\n")
                    handle.flush()
                self.assertTrue(_wait_until(lambda: "tailed line" in " ".join(buffer.tail()), timeout=3))
            finally:
                stop.set()
                thread.join(3)
            self.assertFalse(thread.is_alive())

    def test_tail_file_waits_for_missing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            buffer = bt._LineBuffer()
            stop = threading.Event()
            thread = threading.Thread(
                target=bt._tail_file,
                args=(os.path.join(tmp, "missing.log"), buffer, stop),
                kwargs={"poll": 0.01},
            )
            thread.start()
            time.sleep(0.05)
            stop.set()
            thread.join(3)
            self.assertFalse(thread.is_alive())

    def test_tail_file_rotates_when_inode_changes(self):
        class _Handle:
            def __init__(self):
                self.closed = False

            def fileno(self):
                return 1

            def seek(self, *args):
                return None

            def readline(self):
                return ""

            def close(self):
                self.closed = True

        handle = _Handle()
        stop = threading.Event()

        def fake_stat(path):
            stop.set()
            return types.SimpleNamespace(st_ino=2)

        with (
            mock.patch("builtins.open", return_value=handle),
            mock.patch.object(bt.os, "fstat", return_value=types.SimpleNamespace(st_ino=1)),
            mock.patch.object(bt.os, "stat", side_effect=fake_stat),
            mock.patch.object(bt.time, "sleep"),
        ):
            bt._tail_file("rotate.log", bt._LineBuffer(), stop, poll=0)
        self.assertTrue(handle.closed)


class TestBuiltinPlayitManager(_SettingsCase):
    def test_secret_path(self):
        manager = bt.PlayitManager()
        with tempfile.TemporaryDirectory() as tmp:
            secret = _write(os.path.join(tmp, "playit.toml"), "secret")
            with mock.patch.object(bt, "secret_candidates", return_value=[secret]):
                self.assertEqual(manager.secret_path(), secret)
            with (
                mock.patch.object(bt, "secret_candidates", return_value=[]),
                mock.patch.object(
                    bt, "playit_paths", return_value={"cli": os.path.join(tmp, "missing"), "daemon": "d", "log": "l"}
                ),
            ):
                self.assertEqual(manager.secret_path(), "")
            with (
                mock.patch.object(bt, "secret_candidates", return_value=[]),
                mock.patch.object(bt, "playit_paths", return_value={"cli": "cli", "daemon": "d", "log": "l"}),
                mock.patch.object(bt.os.path, "isfile", return_value=True),
                mock.patch.object(bt, "_run_once", return_value=types.SimpleNamespace(stdout=f"{secret}\nnot-toml\n")),
            ):
                self.assertEqual(manager.secret_path(), secret)
            with (
                mock.patch.object(bt, "secret_candidates", return_value=[]),
                mock.patch.object(bt, "playit_paths", return_value={"cli": "cli", "daemon": "d", "log": "l"}),
                mock.patch.object(bt.os.path, "isfile", return_value=True),
                mock.patch.object(bt, "_run_once", return_value=types.SimpleNamespace(stdout="nothing.txt\n")),
            ):
                self.assertEqual(manager.secret_path(), "")
            with (
                mock.patch.object(bt, "secret_candidates", return_value=[]),
                mock.patch.object(bt, "playit_paths", return_value={"cli": "cli", "daemon": "d", "log": "l"}),
                mock.patch.object(bt.os.path, "isfile", return_value=True),
                mock.patch.object(bt, "_run_once", side_effect=OSError("no exec")),
            ):
                self.assertEqual(manager.secret_path(), "")

    def test_claimed_running_status(self):
        manager = bt.PlayitManager()
        with tempfile.TemporaryDirectory() as tmp:
            secret = _write(os.path.join(tmp, "playit.toml"), "x")
            with mock.patch.object(manager, "secret_path", return_value=secret):
                self.assertTrue(manager.claimed())
        with mock.patch.object(manager, "secret_path", return_value=""):
            self.assertFalse(manager.claimed())
        manager._proc = None
        self.assertFalse(manager.running())
        manager._proc = types.SimpleNamespace(poll=lambda: None)
        self.assertTrue(manager.running())
        manager._proc = types.SimpleNamespace(poll=lambda: 0)
        self.assertFalse(manager.running())
        manager._buffer.add("[INFO] demo.playit.gg:25565")
        self.settings["playit_claim"] = {"code": "abc"}
        with (
            mock.patch.object(bt, "playit_installed", return_value=True),
            mock.patch.object(manager, "installed_version", return_value="1.0"),
            mock.patch.object(manager, "claimed", return_value=True),
        ):
            status = manager.status()
        self.assertTrue(status["installed"])
        self.assertEqual(status["version"], "1.0")
        self.assertTrue(status["claimed"])
        self.assertEqual(status["claim"], {"code": "abc"})

    def test_claim_start(self):
        manager = bt.PlayitManager()
        gen = types.SimpleNamespace(stdout="code123\n", stderr="")
        url = types.SimpleNamespace(stdout="https://playit.gg/claim/code123\n", stderr="")
        with (
            mock.patch.object(bt, "playit_paths", return_value={"cli": "cli", "daemon": "d", "log": "l"}),
            mock.patch.object(bt.os.path, "isfile", return_value=True),
            mock.patch.object(bt, "_run_once", side_effect=[gen, url]),
            mock.patch.object(manager, "_save"),
        ):
            result = manager.claim_start()
        self.assertEqual(result, {"code": "code123", "url": "https://playit.gg/claim/code123"})
        self.assertEqual(self.settings["playit_claim"]["code"], "code123")
        with (
            mock.patch.object(bt, "playit_paths", return_value={"cli": "cli"}),
            mock.patch.object(bt.os.path, "isfile", return_value=False),
            self.assertRaises(bt.BuiltinTunnelError),
        ):
            manager.claim_start()
        for side_effect in (
            [types.SimpleNamespace(stdout="", stderr="failed")],
            [types.SimpleNamespace(stdout="code\n", stderr=""), types.SimpleNamespace(stdout="not-http\n", stderr="")],
            OSError("no exec"),
        ):
            with (
                mock.patch.object(bt, "playit_paths", return_value={"cli": "cli"}),
                mock.patch.object(bt.os.path, "isfile", return_value=True),
                mock.patch.object(bt, "_run_once", side_effect=side_effect),
                self.assertRaises(bt.BuiltinTunnelError),
            ):
                manager.claim_start()

    def test_claim_check(self):
        manager = bt.PlayitManager()
        result = types.SimpleNamespace(stdout="line1\nline2\nclaimed\n", stderr="", returncode=0)
        with (
            mock.patch.object(bt, "playit_paths", return_value={"cli": "cli"}),
            mock.patch.object(bt.os.path, "isfile", return_value=True),
            mock.patch.object(bt, "_run_once", return_value=result),
            mock.patch.object(manager, "secret_path", return_value="/tmp/secret.toml"),
            mock.patch.object(manager, "_save") as save,
        ):
            self.settings["playit_claim"] = {"code": "code123"}
            check = manager.claim_check()
        self.assertTrue(check["claimed"])
        self.assertEqual(check["secret_path"], "/tmp/secret.toml")
        save.assert_called_once()
        self.assertIn("claimed_at", self.settings["playit_claim"])
        # 未领取
        with (
            mock.patch.object(bt, "playit_paths", return_value={"cli": "cli"}),
            mock.patch.object(bt.os.path, "isfile", return_value=True),
            mock.patch.object(bt, "_run_once", return_value=types.SimpleNamespace(stdout="", stderr="", returncode=1)),
            mock.patch.object(manager, "claimed", return_value=False),
        ):
            self.settings["playit_claim"] = {"code": "code123"}
            self.assertFalse(manager.claim_check()["claimed"])
        # 未生成领取码
        with (
            mock.patch.object(bt, "playit_paths", return_value={"cli": "cli"}),
            mock.patch.object(bt.os.path, "isfile", return_value=True),
        ):
            self.settings["playit_claim"] = {}
            with self.assertRaises(bt.BuiltinTunnelError):
                manager.claim_check()
        # 未安装
        with (
            mock.patch.object(bt, "playit_paths", return_value={"cli": "cli"}),
            mock.patch.object(bt.os.path, "isfile", return_value=False),
            self.assertRaises(bt.BuiltinTunnelError),
        ):
            manager.claim_check()
        # _run_once 失败
        with (
            mock.patch.object(bt, "playit_paths", return_value={"cli": "cli"}),
            mock.patch.object(bt.os.path, "isfile", return_value=True),
            mock.patch.object(bt, "_run_once", side_effect=OSError("exec")),
        ):
            self.settings["playit_claim"] = {"code": "c"}
            with self.assertRaises(bt.BuiltinTunnelError):
                manager.claim_check()

    def test_save_delegates_to_settings(self):
        manager = bt.PlayitManager()
        with mock.patch("bedrock_server_manager.utils.save_settings") as save_settings:
            manager._save()
        save_settings.assert_called_once_with(self.settings)

    def test_start_and_stop(self):
        manager = bt.PlayitManager()
        with tempfile.TemporaryDirectory() as tmp:
            secret = _write(os.path.join(tmp, "playit.toml"), "x")
            log_path = _write(os.path.join(tmp, "playitd.log"), "old log")
            proc = types.SimpleNamespace(poll=lambda: None, terminate=mock.Mock(), wait=mock.Mock())
            with (
                mock.patch.object(bt, "playit_installed", return_value=True),
                mock.patch.object(manager, "running", return_value=False),
                mock.patch.object(manager, "secret_path", return_value=secret),
                mock.patch.object(
                    bt,
                    "playit_paths",
                    return_value={
                        "cli": os.path.join(tmp, "playit.exe"),
                        "daemon": os.path.join(tmp, "playitd.exe"),
                        "log": log_path,
                    },
                ),
                mock.patch.object(bt.subprocess, "Popen", return_value=proc) as popen,
                mock.patch.object(bt, "_tail_file"),
            ):
                status = manager.start()
            self.assertFalse(os.path.exists(log_path))
            self.assertEqual(status["installed"], True)
            self.assertEqual(popen.call_args[0][0][0], os.path.join(tmp, "playitd.exe"))
            self.assertIn("--secret-path", popen.call_args[0][0])
            self.assertTrue(manager.running())
            manager.stop()
            proc.terminate.assert_called_once()
        # 未安装 / 已在运行 / 未领取
        with mock.patch.object(bt, "playit_installed", return_value=False):
            with self.assertRaises(bt.BuiltinTunnelError):
                manager.start()
        with (
            mock.patch.object(bt, "playit_installed", return_value=True),
            mock.patch.object(manager, "running", return_value=True),
            self.assertRaises(bt.BuiltinTunnelError),
        ):
            manager.start()
        with (
            mock.patch.object(bt, "playit_installed", return_value=True),
            mock.patch.object(manager, "running", return_value=False),
            mock.patch.object(manager, "secret_path", return_value=""),
            self.assertRaises(bt.BuiltinTunnelError),
        ):
            manager.start()
        # 权限 / 其他 OSError
        with tempfile.TemporaryDirectory() as tmp:
            secret = _write(os.path.join(tmp, "playit.toml"), "x")
            for error, expected in ((PermissionError("denied"), "安全软件"), (OSError("boom"), "启动 playitd 失败")):
                with (
                    mock.patch.object(bt, "playit_installed", return_value=True),
                    mock.patch.object(manager, "running", return_value=False),
                    mock.patch.object(manager, "secret_path", return_value=secret),
                    mock.patch.object(
                        bt, "playit_paths", return_value={"cli": "c", "daemon": "d", "log": os.path.join(tmp, "log")}
                    ),
                    mock.patch.object(bt.subprocess, "Popen", side_effect=error),
                ):
                    with self.assertRaises(bt.BuiltinTunnelError) as ctx:
                        manager.start()
                self.assertIn(expected, str(ctx.exception))

    def test_stop_variants(self):
        manager = bt.PlayitManager()
        manager.stop()  # proc 为 None
        manager._proc = types.SimpleNamespace(poll=lambda: None, terminate=mock.Mock(), wait=mock.Mock())
        manager.stop()
        self.assertIsNone(manager._proc)
        # 等待超时后 kill
        proc = types.SimpleNamespace(poll=lambda: None, terminate=mock.Mock(), kill=mock.Mock())
        proc.wait = mock.Mock(side_effect=[__import__("subprocess").TimeoutExpired("x", 1), 0])
        manager._proc = proc
        manager.stop()
        proc.kill.assert_called_once()
        # terminate 抛 OSError
        proc = types.SimpleNamespace(poll=lambda: None, terminate=mock.Mock(side_effect=OSError("x")), wait=mock.Mock())
        manager._proc = proc
        manager.stop()
        self.assertIsNone(manager._proc)


class TestBuiltinFrpManager(_SettingsCase):
    def test_install_meta_and_address(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(bt, "FRP_DIR", tmp):
            bt._write_meta(tmp, {"version": "0.72.0"})
            manager = bt.FrpManager()
            self.assertEqual(manager.installed_version(), "0.72.0")
            self.assertFalse(bt.frp_installed())
            _write(os.path.join(tmp, bt._exe("frpc")), b"x", binary=True)
            self.assertTrue(bt.frp_installed())
        manager = bt.FrpManager()
        with mock.patch.object(bt, "frp_config", return_value={"server_addr": ""}):
            self.assertEqual(manager.address(), "")
        with mock.patch.object(bt, "frp_config", return_value={"server_addr": "frp.example.com", "remote_port": 7001}):
            self.assertEqual(manager.address(), "frp.example.com:7001")

    def test_status(self):
        manager = bt.FrpManager()
        with (
            mock.patch.object(bt, "frp_installed", return_value=True),
            mock.patch.object(manager, "installed_version", return_value="1.0"),
            mock.patch.object(manager, "running", return_value=False),
            mock.patch.object(bt, "frp_config", return_value=dict(bt.DEFAULT_FRP_CONFIG)),
        ):
            status = manager.status()
        self.assertTrue(status["installed"])
        self.assertEqual(status["version"], "1.0")
        self.assertFalse(status["running"])
        self.assertEqual(status["address"], "")

    def test_start_errors(self):
        manager = bt.FrpManager()
        with mock.patch.object(bt, "frp_installed", return_value=False):
            with self.assertRaises(bt.BuiltinTunnelError):
                manager.start()
        with (
            mock.patch.object(bt, "frp_installed", return_value=True),
            mock.patch.object(manager, "running", return_value=True),
            self.assertRaises(bt.BuiltinTunnelError),
        ):
            manager.start()
        with (
            mock.patch.object(bt, "frp_installed", return_value=True),
            mock.patch.object(manager, "running", return_value=False),
            mock.patch.object(bt, "frp_config", return_value={"server_addr": ""}),
        ):
            with self.assertRaises(bt.BuiltinTunnelError):
                manager.start()
        with tempfile.TemporaryDirectory() as tmp:
            for error, expected in ((PermissionError("denied"), "杀毒"), (OSError("boom"), "启动 frpc 失败")):
                with (
                    mock.patch.object(bt, "frp_installed", return_value=True),
                    mock.patch.object(manager, "running", return_value=False),
                    mock.patch.object(bt, "frp_config", return_value={"server_addr": "frp.example.com"}),
                    mock.patch.object(bt, "FRP_DIR", tmp),
                    mock.patch.object(bt.subprocess, "Popen", side_effect=error),
                ):
                    with self.assertRaises(bt.BuiltinTunnelError) as ctx:
                        manager.start()
                self.assertIn(expected, str(ctx.exception))

    def test_start_read_loop_and_stop(self):
        manager = bt.FrpManager()
        stream = types.SimpleNamespace(readline=mock.Mock(side_effect=[b"frpc started\n", b"proxy ok\n", b""]))
        proc = types.SimpleNamespace(
            stdout=stream, poll=mock.Mock(return_value=None), terminate=mock.Mock(), wait=mock.Mock()
        )
        with tempfile.TemporaryDirectory() as tmp:
            with (
                mock.patch.object(bt, "frp_installed", return_value=True),
                mock.patch.object(manager, "running", return_value=False),
                mock.patch.object(
                    bt,
                    "frp_config",
                    return_value={
                        "server_addr": "frp.example.com",
                        "server_port": 7000,
                        "token": "",
                        "local_port": 19132,
                        "remote_port": 19132,
                    },
                ),
                mock.patch.object(bt, "FRP_DIR", tmp),
                mock.patch.object(bt.subprocess, "Popen", return_value=proc),
            ):
                status = manager.start()
                _wait_until(lambda: "proxy ok" in " ".join(manager._buffer.tail()), timeout=2)
                self.assertTrue(os.path.isfile(os.path.join(tmp, "frpc.toml")))
                self.assertIn("proxy ok", " ".join(manager._buffer.tail()))
            self.assertTrue(manager.running())
            self.assertTrue(status["installed"])
            manager.stop()
            proc.terminate.assert_called_once()

    def test_read_loop_marks_error(self):
        manager = bt.FrpManager()
        manager._read_loop(types.SimpleNamespace(stdout=None, poll=lambda: 0))
        stream = types.SimpleNamespace(readline=mock.Mock(side_effect=[b"one\n", b""]))
        manager._read_loop(types.SimpleNamespace(stdout=stream, poll=lambda: 3))
        self.assertIn("code=3", manager._error)
        manager._error = ""
        manager._read_loop(types.SimpleNamespace(stdout=stream, poll=lambda: 0))
        self.assertEqual(manager._error, "")

    def test_stop_variants(self):
        manager = bt.FrpManager()
        manager.stop()
        proc = types.SimpleNamespace(poll=lambda: None, terminate=mock.Mock(), wait=mock.Mock())
        manager._proc = proc
        manager.stop()
        self.assertIsNone(manager._proc)
        proc = types.SimpleNamespace(poll=lambda: None, terminate=mock.Mock(), kill=mock.Mock())
        proc.wait = mock.Mock(side_effect=[__import__("subprocess").TimeoutExpired("x", 1)])
        manager._proc = proc
        manager.stop()
        proc.kill.assert_called_once()
        proc = types.SimpleNamespace(poll=lambda: None, terminate=mock.Mock(side_effect=OSError("x")), wait=mock.Mock())
        manager._proc = proc
        manager.stop()
        self.assertIsNone(manager._proc)


class TestBuiltinFrpConfig(_SettingsCase):
    def test_frp_config_defaults_and_merge(self):
        self.assertEqual(bt.frp_config()["remote_port"], 19132)
        self.settings["frp_config"] = {"server_addr": "a.example.com", "unknown": 1}
        cfg = bt.frp_config()
        self.assertEqual(cfg["server_addr"], "a.example.com")
        self.assertNotIn("unknown", cfg)
        self.settings["frp_config"] = "not-a-dict"
        self.assertEqual(bt.frp_config()["server_port"], 7000)

    def test_save_frp_config(self):
        with mock.patch("bedrock_server_manager.utils.save_settings", return_value=True):
            cfg = bt.save_frp_config(
                server_addr="frp.example.com",
                server_port=7000,
                remote_port=19132,
                local_port=19133,
                token="secret",
            )
        self.assertEqual(cfg["server_addr"], "frp.example.com")
        self.assertEqual(cfg["token"], "secret")
        with mock.patch("bedrock_server_manager.utils.save_settings", return_value=True):
            with self.assertRaises(bt.BuiltinTunnelError):
                bt.save_frp_config(server_addr="bad host!")
            with self.assertRaises(bt.BuiltinTunnelError):
                bt.save_frp_config(server_port="abc")
            with self.assertRaises(bt.BuiltinTunnelError):
                bt.save_frp_config(remote_port=70000)
            long_addr = "a" * 300
            self.assertEqual(bt.save_frp_config(server_addr=long_addr)["server_addr"], "a" * 255)
            self.assertEqual(len(bt.save_frp_config(token="t" * 200)["token"]), 128)

    def test_render_frp_toml(self):
        toml = bt.render_frp_toml({"server_addr": "1.2.3.4", "token": 'a\\"b'})
        self.assertIn('serverAddr = "1.2.3.4"', toml)
        self.assertIn("serverPort = 7000", toml)
        self.assertIn('auth.token = "a\\\\\\"b"', toml)
        self.assertIn("localPort = 19132", toml)
        with self.assertRaises(bt.BuiltinTunnelError):
            bt.render_frp_toml({"server_addr": ""})
        self.assertEqual(bt._escape_toml('a"b\\c'), 'a\\"b\\\\c')


# =========================================================================== levilamina


class TestLeviLipInstall(_SettingsCase):
    def test_supported_and_paths(self):
        with mock.patch.object(L, "IS_WINDOWS", False):
            self.assertFalse(L.supported())
        with (
            mock.patch.object(L, "IS_WINDOWS", True),
            mock.patch.dict(os.environ, {"PROCESSOR_ARCHITECTURE": "ARM64"}, clear=False),
        ):
            self.assertFalse(L.supported())
        with (
            mock.patch.object(L, "IS_WINDOWS", True),
            mock.patch.dict(os.environ, {"PROCESSOR_ARCHITECTURE": "AMD64"}, clear=False),
        ):
            self.assertTrue(L.supported())
        self.assertTrue(L.lip_exe_path().endswith("lip.exe"))
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(L, "LIP_DIR", tmp):
            self.assertFalse(L.lip_installed())
            _write(os.path.join(tmp, "lip.exe"))
            self.assertTrue(L.lip_installed())

    def test_github_headers(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            headers = L._gitub_headers()
            self.assertEqual(headers["User-Agent"], L.USER_AGENT)
            self.assertNotIn("Authorization", headers)
        with mock.patch.dict(os.environ, {"GITHUB_TOKEN": "tok"}, clear=True):
            self.assertEqual(L._gitub_headers()["Authorization"], "Bearer tok")
        with mock.patch.dict(os.environ, {"GH_TOKEN": "gh"}, clear=True):
            self.assertEqual(L._gitub_headers()["Authorization"], "Bearer gh")

    def test_fetch_lip_release(self):
        payload = json.dumps({"assets": [], "tag_name": "v1"}).encode()
        with mock.patch.object(L.urllib.request, "urlopen", return_value=_FakeResponse(payload)):
            self.assertEqual(L.fetch_lip_release()["tag_name"], "v1")
        for side_effect in (urllib.error.URLError("offline"), TimeoutError("slow"), OSError("down")):
            with mock.patch.object(L.urllib.request, "urlopen", side_effect=side_effect):
                with self.assertRaises(L.LeviLaminaError):
                    L.fetch_lip_release()
        with mock.patch.object(L.urllib.request, "urlopen", return_value=_FakeResponse(b"not json")):
            with self.assertRaises(L.LeviLaminaError):
                L.fetch_lip_release()
        with mock.patch.object(L.urllib.request, "urlopen", return_value=_FakeResponse(b'{"tag_name": "v1"}')):
            with self.assertRaises(L.LeviLaminaError):
                L.fetch_lip_release()
        with mock.patch.object(L.urllib.request, "urlopen", return_value=_FakeResponse(b"[]")):
            with self.assertRaises(L.LeviLaminaError):
                L.fetch_lip_release()

    def _release(self, data, digest=None):
        return {
            "tag_name": "v1.2.3",
            "assets": [
                {
                    "name": L.lip_asset_name(),
                    "browser_download_url": "http://x/lip.zip",
                    "digest": digest if digest is not None else "sha256:" + hashlib.sha256(data).hexdigest(),
                }
            ],
        }

    def test_install_lip_success(self):
        data = _zip_bytes({"lip.exe": b"LIP", "readme.txt": b"hi"})
        with tempfile.TemporaryDirectory() as tmp:
            progress = []
            with (
                mock.patch.object(L, "LIP_DIR", tmp),
                mock.patch.object(L, "supported", return_value=True),
                mock.patch.object(L, "fetch_lip_release", return_value=self._release(data)),
                mock.patch.object(L.urllib.request, "urlopen", return_value=_FakeResponse(data)),
            ):
                result = L.install_lip(progress.append)
                self.assertTrue(result["installed"])
                self.assertEqual(result["version"], "v1.2.3")
                with open(os.path.join(tmp, "lip.exe"), "rb") as handle:
                    self.assertEqual(handle.read(), b"LIP")
                self.assertEqual(progress, ["下载 lip-win-x64.zip …"])
                self.assertEqual(L.lip_version(), "v1.2.3")
                self.assertFalse([name for name in os.listdir(tmp) if name.endswith(".zip")])

    def test_install_lip_digest_mismatch(self):
        data = _zip_bytes({"lip.exe": b"LIP"})
        with tempfile.TemporaryDirectory() as tmp:
            with (
                mock.patch.object(L, "LIP_DIR", tmp),
                mock.patch.object(L, "supported", return_value=True),
                mock.patch.object(
                    L, "fetch_lip_release", return_value=self._release(data, digest="sha256:" + "0" * 64)
                ),
                mock.patch.object(L.urllib.request, "urlopen", return_value=_FakeResponse(data)),
            ):
                with self.assertRaises(L.LeviLaminaError) as ctx:
                    L.install_lip()
            self.assertIn("SHA256", str(ctx.exception))
            self.assertFalse(os.path.exists(os.path.join(tmp, "lip.exe")))

    def test_install_lip_missing_asset_and_platform(self):
        with mock.patch.object(L, "supported", return_value=False), self.assertRaises(L.LeviLaminaError):
            L.install_lip()
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(L, "LIP_DIR", tmp),
            mock.patch.object(L, "supported", return_value=True),
            mock.patch.object(L, "fetch_lip_release", return_value={"assets": []}),
            self.assertRaises(L.LeviLaminaError),
        ):
            L.install_lip()

    def test_install_lip_oversize_and_missing_exe(self):
        data = _zip_bytes({"readme.txt": b"no exe"})
        with tempfile.TemporaryDirectory() as tmp:
            with (
                mock.patch.object(L, "LIP_DIR", tmp),
                mock.patch.object(L, "supported", return_value=True),
                mock.patch.object(L, "MAX_DOWNLOAD_BYTES", 2),
                mock.patch.object(L, "fetch_lip_release", return_value=self._release(data)),
                mock.patch.object(L.urllib.request, "urlopen", return_value=_FakeResponse(data)),
            ):
                with self.assertRaises(L.LeviLaminaError) as ctx:
                    L.install_lip()
            self.assertIn("大小上限", str(ctx.exception))
        with tempfile.TemporaryDirectory() as tmp:
            with (
                mock.patch.object(L, "LIP_DIR", tmp),
                mock.patch.object(L, "supported", return_value=True),
                mock.patch.object(L, "fetch_lip_release", return_value=self._release(data)),
                mock.patch.object(L.urllib.request, "urlopen", return_value=_FakeResponse(data)),
            ):
                with self.assertRaises(L.LeviLaminaError) as ctx:
                    L.install_lip()
            self.assertIn("没有 lip.exe", str(ctx.exception))

    def test_lip_version_invalid(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(L, "LIP_DIR", tmp):
            self.assertEqual(L.lip_version(), "")
            _write(os.path.join(tmp, "meta.json"), "not json")
            self.assertEqual(L.lip_version(), "")
            _write(os.path.join(tmp, "meta.json"), "[]")
            self.assertEqual(L.lip_version(), "")
            _write(os.path.join(tmp, "meta.json"), '{"version": 123}')
            self.assertEqual(L.lip_version(), "123")


class TestLeviStateAndMods(_SettingsCase):
    def test_game_version(self):
        self.assertEqual(L.game_version_for_lip("1.26.60.6"), "1.26.60")
        self.assertEqual(L.game_version_for_lip(" v1.21.0.3 "), "1.21.0")
        for bad in ("", "abc", "1.2", "1.x.0"):
            with self.assertRaises(L.LeviLaminaError):
                L.game_version_for_lip(bad)

    def test_state_variants(self):
        self.assertFalse(L.state("")["installed"])
        self.assertFalse(L.state("/definitely/missing")["installed"])
        with tempfile.TemporaryDirectory() as tmp:
            _write(os.path.join(tmp, L.OFFICIAL_EXE))
            st = L.state(tmp)
            self.assertFalse(st["installed"])
            _write(os.path.join(tmp, L.MODDED_EXE))
            _write(os.path.join(tmp, ".lip", "x"), "")
            _write(os.path.join(tmp, L.MODS_DIR, "LeviLamina", "manifest.json"), _manifest("LeviLamina", "26.60.9"))
            _write(os.path.join(tmp, L.MODS_DIR, "Alpha", "manifest.json"), _manifest("Alpha", "2.1.0"))
            st = L.state(tmp)
            self.assertTrue(st["installed"])
            self.assertFalse(st["enabled"])
            self.assertTrue(st["workspace"])
            self.assertEqual(st["loader_version"], "26.60.9")
            self.assertEqual([m["entry"] for m in st["mods"]], ["Alpha"])
            _write(os.path.join(tmp, L.MODS_DIR, "LeviLamina", "manifest.json"), "not json")
            self.assertEqual(L.state(tmp)["loader_version"], "")

    def test_state_version_detection_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write(os.path.join(tmp, L.MODDED_EXE))
            with mock.patch("bedrock_server_manager.install.detect_server_version", side_effect=RuntimeError("x")):
                self.assertEqual(L.state(tmp)["bds_version"], "")

    def test_manifest_reading(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = os.path.join(tmp, "mod")
            _write(os.path.join(d, "manifest.json"), _manifest("Mod", "1.2.3"))
            self.assertEqual(L._read_manifest(d)["name"], "Mod")
            _write(os.path.join(d, "manifest.json"), "[]")
            self.assertEqual(L._read_manifest(d), {})
            zip_path = os.path.join(tmp, "mod.zip")
            with zipfile.ZipFile(zip_path, "w") as zf:
                zf.writestr("mod/manifest.json", _manifest("ZipMod"))
            self.assertEqual(L._read_manifest(zip_path)["name"], "ZipMod")
            with zipfile.ZipFile(zip_path, "w") as zf:
                zf.writestr("deep/nested/manifest.json", _manifest("Deep"))
            self.assertEqual(L._read_manifest(zip_path), {})
            _write(os.path.join(tmp, "bad.zip"), "not a zip")
            self.assertEqual(L._read_manifest(os.path.join(tmp, "bad.zip")), {})
            self.assertEqual(L._read_manifest(os.path.join(tmp, "missing")), {})

    def test_mod_entries_and_sizes(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = os.path.join(tmp, "DirMod")
            _write(os.path.join(d, "manifest.json"), _manifest("DirMod", "1.0", "native"))
            entry = L._mod_entry(tmp, "DirMod", disabled=False)
            self.assertEqual(entry["type"], "native")
            self.assertEqual(entry["size"], os.path.getsize(os.path.join(d, "manifest.json")))
            loose = _write(os.path.join(tmp, "loose.zip"), "data")
            entry = L._mod_entry(tmp, "loose.zip", disabled=True)
            self.assertEqual(entry["type"], "zip")
            self.assertFalse(entry["enabled"])
            self.assertEqual(
                L._dir_size(tmp),
                os.path.getsize(loose) + os.path.getsize(os.path.join(d, "manifest.json")),
            )
            with mock.patch.object(L.os.path, "getsize", side_effect=OSError("gone")):
                self.assertEqual(L._dir_size(d), 0)

    def test_list_mods_filters_invalid_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.join(tmp, L.MODS_DIR)
            _write(os.path.join(root, "Good", "manifest.json"), _manifest("Good"))
            original = os.listdir
            with mock.patch.object(
                L.os,
                "listdir",
                side_effect=lambda p: ["Good", "bad/name", "bad\\name"] if p == root else original(p),
            ):
                names = [m["entry"] for m in L.list_mods(tmp)]
        self.assertEqual(names, ["Good"])

    def test_toggle_and_delete(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write(os.path.join(tmp, L.MODS_DIR, "Alpha", "manifest.json"), _manifest("Alpha"))
            result = L.toggle_mod(tmp, "Alpha", enabled=False)
            self.assertFalse(result["enabled"])
            self.assertTrue(os.path.isdir(os.path.join(tmp, L.DISABLED_DIR, "Alpha")))
            with self.assertRaises(L.LeviLaminaError):
                L.toggle_mod(tmp, "Alpha", enabled=False)
            L.toggle_mod(tmp, "Alpha", enabled=True)
            self.assertTrue(os.path.isdir(os.path.join(tmp, L.MODS_DIR, "Alpha")))
            _write(os.path.join(tmp, L.MODS_DIR, "Dup", "manifest.json"), _manifest("Dup"))
            _write(os.path.join(tmp, L.DISABLED_DIR, "Dup", "manifest.json"), _manifest("Dup"))
            with self.assertRaises(L.LeviLaminaError):
                L.toggle_mod(tmp, "Dup", enabled=False)
            for bad in ("", "../etc", ".", "..", "a/b"):
                with self.assertRaises(L.LeviLaminaError):
                    L._safe_name(bad)
            L.delete_mod(tmp, "Alpha")
            self.assertFalse(os.path.exists(os.path.join(tmp, L.MODS_DIR, "Alpha")))
            L.delete_mod(tmp, "Dup")
            loose = _write(os.path.join(tmp, L.DISABLED_DIR, "loose.dll"), "dll")
            L.delete_mod(tmp, "loose.dll")
            self.assertFalse(os.path.exists(loose))
            with self.assertRaises(L.LeviLaminaError):
                L.delete_mod(tmp, "missing")


class TestLeviModInstall(_SettingsCase):
    def test_zip_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(L.LeviLaminaError):
                L.install_mod_zip(tmp, os.path.join(tmp, "missing.zip"))
            bad = _write(os.path.join(tmp, "bad.zip"), "not a zip")
            with self.assertRaises(L.LeviLaminaError):
                L.install_mod_zip(tmp, bad)

    def test_zip_lip_workspace_success_and_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write(os.path.join(tmp, ".lip", "x"), "")
            zip_path = _write(
                os.path.join(tmp, "Mod.zip"), _zip_bytes({"manifest.json": _manifest("Mod")}), binary=True
            )
            with (
                mock.patch.object(L, "lip_installed", return_value=True),
                mock.patch.object(L, "run_lip", return_value=(0, "ok")) as run,
            ):
                self.assertEqual(L.install_mod_zip(tmp, zip_path)["via"], "lip")
            self.assertEqual(run.call_args[0][0][:1], ["install"])
            with (
                mock.patch.object(L, "lip_installed", return_value=True),
                mock.patch.object(L, "run_lip", return_value=(1, "error-output")),
            ):
                with self.assertRaises(L.LeviLaminaError) as ctx:
                    L.install_mod_zip(tmp, zip_path)
            self.assertIn("error-output", str(ctx.exception))

    def test_zip_plain_install_and_sanitize(self):
        with tempfile.TemporaryDirectory() as tmp:
            zip_path = _write(
                os.path.join(tmp, "tmp123.zip"),
                _zip_bytes({"manifest.json": _manifest("Named"), "Named.dll": b"dll", "sub/": b""}),
                binary=True,
            )
            result = L.install_mod_zip(tmp, zip_path, display_name="My Mod!.zip")
            self.assertEqual(result["via"], "unzip")
            self.assertEqual(result["name"], "My_Mod_")
            self.assertTrue(os.path.isfile(os.path.join(tmp, L.MODS_DIR, "My_Mod_", "Named.dll")))
            with self.assertRaises(L.LeviLaminaError):
                L.install_mod_zip(tmp, zip_path, display_name="My Mod!.zip")  # 已存在

    def test_zip_rejects_traversal(self):
        with tempfile.TemporaryDirectory() as tmp:
            zip_path = _write(
                os.path.join(tmp, "evil.zip"),
                _zip_bytes({"../../evil.txt": b"x"}),
                binary=True,
            )
            with self.assertRaises(L.LeviLaminaError):
                L.install_mod_zip(tmp, zip_path)
            self.assertFalse(os.path.exists(os.path.join(tmp, "..", "evil.txt")))
            abs_zip = _write(
                os.path.join(tmp, "abs.zip"),
                _zip_bytes({"/abs.txt": b"x"}),
                binary=True,
            )
            with self.assertRaises(L.LeviLaminaError):
                L.install_mod_zip(tmp, abs_zip)

    def test_install_mod_url(self):
        data = _zip_bytes({"manifest.json": _manifest("Remote")})
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(L, "LIP_DIR", os.path.join(tmp, "tools")):
            with self.assertRaises(L.LeviLaminaError):
                L.install_mod_url(tmp, "http://example.com/mod.zip")
            with (
                mock.patch.object(L.urllib.request, "urlopen", return_value=_FakeResponse(data)),
            ):
                result = L.install_mod_url(tmp, "https://example.com/Remote")
            self.assertEqual(result["via"], "unzip")
            self.assertTrue(os.path.isdir(os.path.join(tmp, L.MODS_DIR, "Remote")))
        with tempfile.TemporaryDirectory() as tmp:
            with (
                mock.patch.object(L, "LIP_DIR", os.path.join(tmp, "tools")),
                mock.patch.object(L, "MAX_DOWNLOAD_BYTES", 2),
                mock.patch.object(L.urllib.request, "urlopen", return_value=_FakeResponse(data)),
            ):
                with self.assertRaises(L.LeviLaminaError) as ctx:
                    L.install_mod_url(tmp, "https://example.com/Mod.zip")
            self.assertIn("大小上限", str(ctx.exception))
        with tempfile.TemporaryDirectory() as tmp:
            with (
                mock.patch.object(L, "LIP_DIR", os.path.join(tmp, "tools")),
                mock.patch.object(L.urllib.request, "urlopen", side_effect=urllib.error.URLError("offline")),
            ):
                with self.assertRaises(L.LeviLaminaError) as ctx:
                    L.install_mod_url(tmp, "https://example.com/Mod.zip")
            self.assertIn("下载插件失败", str(ctx.exception))

    def test_extract_member_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            zip_path = _write(os.path.join(tmp, "a.zip"), _zip_bytes({"a.txt": b"x", "b/c.txt": b"y"}), binary=True)
            self.assertEqual(L.extract_member_names(zip_path), ["a.txt", "b/c.txt"])
            tar_path = os.path.join(tmp, "a.tar")
            with tarfile.open(tar_path, "w") as tf:
                info = tarfile.TarInfo("one.txt")
                info.size = 1
                tf.addfile(info, io.BytesIO(b"x"))
            self.assertEqual(L.tar_member_names(tar_path), ["one.txt"])
            bad = _write(os.path.join(tmp, "bad.bin"), "x")
            self.assertEqual(L.extract_member_names(bad), [])
            self.assertEqual(L.tar_member_names(bad), [])


class _FakeStream:
    def __init__(self, lines):
        self._lines = iter(lines)

    def readline(self):
        return next(self._lines, b"")


class _Proc:
    def __init__(self, lines=(b"",), code=0):
        self.stdout = _FakeStream(lines)
        self._code = code
        self.killed = False

    def wait(self, timeout=None):
        return self._code

    def poll(self):
        return self._code

    def kill(self):
        self.killed = True


class TestLeviProcessAndJobs(_SettingsCase):
    def test_run_stream_success(self):
        lines = ["first", "second"]

        def on_line(text):
            lines.append(text)

        proc = _Proc([b"hello\n", b"\n", b"world\r\n", b""], code=0)
        with mock.patch.object(L.subprocess, "Popen", return_value=proc):
            code, output = L._run_stream(["lip", "x"], "/tmp", on_line)
        self.assertEqual(code, 0)
        self.assertEqual(output, ["hello", "world"])
        self.assertEqual(len(lines), 4)

    def test_run_stream_popen_error(self):
        with mock.patch.object(L.subprocess, "Popen", side_effect=OSError("no exec")):
            with self.assertRaises(L.LeviLaminaError) as ctx:
                L._run_stream(["x"], "/tmp", lambda line: None)
        self.assertIn("启动命令失败", str(ctx.exception))

    def test_run_stream_timeout(self):
        proc = _Proc([b"line\n"], code=0)
        with (
            mock.patch.object(L.subprocess, "Popen", return_value=proc),
            mock.patch.object(L.time, "time", side_effect=[0.0, 10_000.0]),
            self.assertRaises(L.LeviLaminaError) as ctx,
        ):
            L._run_stream(["x"], "/tmp", lambda line: None, timeout=1)
        self.assertIn("超时", str(ctx.exception))
        self.assertTrue(proc.killed)

    def test_run_stream_trims_log_window(self):
        proc = _Proc([f"line{i}\n".encode() for i in range(L.MAX_LOG_LINES + 5)] + [b""], code=0)
        with mock.patch.object(L.subprocess, "Popen", return_value=proc):
            code, output = L._run_stream(["x"], "/tmp", lambda line: None)
        self.assertEqual(code, 0)
        self.assertEqual(len(output), L.MAX_LOG_LINES)
        self.assertEqual(output[0], f"line{5}")

    def test_run_lip(self):
        with tempfile.TemporaryDirectory() as tmp:
            exe = _write(os.path.join(tmp, "lip.exe"), "x")
            completed = types.SimpleNamespace(returncode=0, stdout="out", stderr="err")
            with (
                mock.patch.object(L, "lip_exe_path", return_value=exe),
                mock.patch.object(L.subprocess, "run", return_value=completed) as run,
            ):
                code, output = L.run_lip(["list"], tmp)
            self.assertEqual(code, 0)
            self.assertEqual(output, "outerr")
            self.assertEqual(run.call_args[0][0], [exe, "list"])
        with mock.patch.object(L, "lip_exe_path", return_value="/missing/lip.exe"):
            with self.assertRaises(L.LeviLaminaError):
                L.run_lip(["list"], "/tmp")
        with tempfile.TemporaryDirectory() as tmp:
            exe = _write(os.path.join(tmp, "lip.exe"), "x")
            for error in (OSError("x"), L.subprocess.SubprocessError("boom")):
                with (
                    mock.patch.object(L, "lip_exe_path", return_value=exe),
                    mock.patch.object(L.subprocess, "run", side_effect=error),
                    self.assertRaises(L.LeviLaminaError),
                ):
                    L.run_lip(["list"], tmp)

    def test_job_status_and_trim(self):
        job = L.Job()
        job._append("a")
        job.running = True
        job.lines = [f"line{i}" for i in range(L.MAX_LOG_LINES + 20)]
        job._append("last")
        status = job.status()
        self.assertEqual(len(status["lines"]), 40)
        self.assertEqual(status["lines"][-1], "last")

    def test_backup_configs(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name in ("server.properties", "permissions.json", "allowlist.json", "whitelist.json"):
                _write(os.path.join(tmp, name), name)
            target = L._backup_configs(tmp)
            for name in ("server.properties", "permissions.json", "allowlist.json", "whitelist.json"):
                self.assertTrue(os.path.isfile(os.path.join(target, name)))
            with mock.patch.object(L.shutil, "copy2", side_effect=OSError("locked")):
                self.assertTrue(os.path.isdir(L._backup_configs(tmp)))

    def test_enable_loader_job_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write(os.path.join(tmp, L.OFFICIAL_EXE))

            def fake_run(argv, cwd, on_line, timeout=0):
                _write(os.path.join(cwd, L.MODDED_EXE))
                on_line("done")
                return 0, ["done"]

            with (
                mock.patch.object(L, "supported", return_value=True),
                mock.patch.object(L, "lip_installed", return_value=True),
                mock.patch.object(L, "_run_stream", side_effect=fake_run),
            ):
                result = L.enable_loader(tmp, "1.26.60.6")
                self.assertTrue(result["started"])
                self.assertTrue(_wait_until(lambda: L.job.status()["done"]))
            self.assertTrue(L.job.status()["ok"])
            self.assertFalse(L.job.status()["running"])

    def test_enable_loader_job_failures(self):
        scenarios = [
            (mock.Mock(return_value=(1, ["bad"])), "退出码"),
            (mock.Mock(return_value=(0, [])), "未找到"),
        ]
        for run_stream, expected in scenarios:
            with tempfile.TemporaryDirectory() as tmp:
                _write(os.path.join(tmp, L.OFFICIAL_EXE))
                with (
                    mock.patch.object(L, "supported", return_value=True),
                    mock.patch.object(L, "lip_installed", return_value=True),
                    mock.patch.object(L, "_run_stream", run_stream),
                ):
                    L.enable_loader(tmp, "1.26.60.6")
                    self.assertTrue(_wait_until(lambda: L.job.status()["done"]))
                self.assertFalse(L.job.status()["ok"])
                self.assertIn(expected, L.job.status()["error"])
        with tempfile.TemporaryDirectory() as tmp:
            _write(os.path.join(tmp, L.OFFICIAL_EXE))
            with (
                mock.patch.object(L, "supported", return_value=True),
                mock.patch.object(L, "lip_installed", return_value=True),
                mock.patch.object(L, "_run_stream", side_effect=RuntimeError("thread boom")),
            ):
                L.enable_loader(tmp, "1.26.60.6")
                self.assertTrue(_wait_until(lambda: L.job.status()["done"]))
            self.assertIn("thread boom", L.job.status()["error"])

    def test_enable_loader_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(L, "supported", return_value=False):
                with self.assertRaises(L.LeviLaminaError):
                    L.enable_loader(tmp, "1.26.60.6")
            with (
                mock.patch.object(L, "supported", return_value=True),
                mock.patch.object(L, "lip_installed", return_value=False),
                self.assertRaises(L.LeviLaminaError),
            ):
                L.enable_loader(tmp, "1.26.60.6")
            with (
                mock.patch.object(L, "supported", return_value=True),
                mock.patch.object(L, "lip_installed", return_value=True),
                self.assertRaises(L.LeviLaminaError),
            ):
                L.enable_loader(os.path.join(tmp, "missing"), "1.26.60.6")
            _write(os.path.join(tmp, L.MODDED_EXE))
            with (
                mock.patch.object(L, "supported", return_value=True),
                mock.patch.object(L, "lip_installed", return_value=True),
                self.assertRaises(L.LeviLaminaError),
            ):
                L.enable_loader(tmp, "1.26.60.6")

    def test_disable_loader(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(L.LeviLaminaError):
                L.disable_loader(tmp)
            _write(os.path.join(tmp, L.MODDED_EXE))
            _write(os.path.join(tmp, L.OFFICIAL_EXE))
            L.disable_loader(tmp)
            self.assertTrue(os.path.isfile(os.path.join(tmp, L.MODDED_EXE + ".disabled")))
        with tempfile.TemporaryDirectory() as tmp:
            _write(os.path.join(tmp, L.MODDED_EXE))
            _write(os.path.join(tmp, L.OFFICIAL_EXE + ".bak"))
            L.disable_loader(tmp)
            self.assertTrue(os.path.isfile(os.path.join(tmp, L.OFFICIAL_EXE)))
        with tempfile.TemporaryDirectory() as tmp:
            _write(os.path.join(tmp, L.MODDED_EXE))
            with self.assertRaises(L.LeviLaminaError) as ctx:
                L.disable_loader(tmp)
            self.assertIn("重新安装", str(ctx.exception))


class TestLeviLipCommands(_SettingsCase):
    def test_lip_list(self):
        payload = {"ExplicitInstalled": ["x"]}
        with mock.patch.object(L, "run_lip", return_value=(0, json.dumps(payload))):
            self.assertEqual(L.lip_list("/tmp"), payload)
        with mock.patch.object(L, "run_lip", return_value=(1, "failed")):
            with self.assertRaises(L.LeviLaminaError) as ctx:
                L.lip_list("/tmp")
        self.assertIn("failed", str(ctx.exception))
        with mock.patch.object(L, "run_lip", return_value=(0, "not json")):
            with self.assertRaises(L.LeviLaminaError):
                L.lip_list("/tmp")
        with mock.patch.object(L, "run_lip", return_value=(0, "[1, 2]")), self.assertRaises(L.LeviLaminaError):
            L.lip_list("/tmp")

    def test_lip_install(self):
        for bad in ("", "   ", "x" * 301, "bad package"):
            with self.assertRaises(L.LeviLaminaError):
                L.lip_install("/tmp", bad)
        with mock.patch.object(L, "run_lip", return_value=(0, "out")) as run:
            self.assertTrue(L.lip_install("/tmp", "github.com/LiteLDev/LeviLamina")["installed"])
        self.assertEqual(run.call_args[0][0][:1], ["install"])
        with mock.patch.object(L, "run_lip", return_value=(1, "error-output")):
            with self.assertRaises(L.LeviLaminaError) as ctx:
                L.lip_install("/tmp", "pkg")
        self.assertIn("error-output", str(ctx.exception))

    def test_lip_uninstall(self):
        for bad in ("", "x" * 301):
            with self.assertRaises(L.LeviLaminaError):
                L.lip_uninstall("/tmp", bad)
        with mock.patch.object(L, "run_lip", return_value=(0, "ok")):
            self.assertTrue(L.lip_uninstall("/tmp", "pkg")["uninstalled"])
        with mock.patch.object(L, "run_lip", return_value=(2, "fail")), self.assertRaises(L.LeviLaminaError):
            L.lip_uninstall("/tmp", "pkg")


# =========================================================================== web.handler


class _HttpCase(_SettingsCase):
    def setUp(self):
        super().setUp()
        AppContext.instance().api_token = "test-token"
        remote.sessions.clear()
        remote.login_limiter.record_success("127.0.0.1")
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H.Handler)
        self.port = self.httpd.server_port
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(3)
        remote.sessions.clear()
        remote.login_limiter.record_success("127.0.0.1")
        super().tearDown()

    def request(self, method, path, headers=None, body=None, host=None):
        conn = HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        values = {"Host": host or f"127.0.0.1:{self.port}"}
        values.update(headers or {})
        for key, value in values.items():
            conn.putheader(key, value)
        if body is not None:
            conn.putheader("Content-Length", str(len(body)))
        conn.endheaders(body)
        response = conn.getresponse()
        data = response.read()
        result = (response.status, dict(response.getheaders()), data)
        conn.close()
        return result

    def raw_request(self, payload):
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=10)
        try:
            sock.sendall(payload)
            chunks = []
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
        finally:
            sock.close()
        return b"".join(chunks)


class _Stub:
    def __init__(self, headers=None, ip="127.0.0.1", port=19100, server=True):
        self.headers = dict(headers or {})
        self.client_address = (ip, 1234) if ip is not None else None
        if server:
            self.server = types.SimpleNamespace(server_port=port)

    def _send(self, *args, **kwargs):
        self.sent = (args, kwargs)


def _stub_handler(headers=None, ip="127.0.0.1", port=19100):
    return _Stub(headers, ip, port)


class TestHandlerHostAndAuth(_HttpCase):
    def test_check_host_branches(self):
        self.assertFalse(H.Handler._check_host(_stub_handler({})))
        self.assertTrue(H.Handler._check_host(_stub_handler({"Host": "127.0.0.1:19100"})))
        self.assertTrue(H.Handler._check_host(_stub_handler({"Host": "localhost"})))
        self.assertTrue(H.Handler._check_host(_stub_handler({"Host": "127.0.0.1"})))
        self.assertFalse(H.Handler._check_host(_stub_handler({"Host": "127.0.0.1:1234"})))
        self.assertFalse(H.Handler._check_host(_stub_handler({"Host": "evil.example.com:19100"})))
        self.assertFalse(H.Handler._check_host(_stub_handler({"Host": "127.0.0.1:notaport"})))
        # server 不可用 -> server_port 为 None，端口不参与校验
        self.assertTrue(H.Handler._check_host(_Stub({"Host": "127.0.0.1:1234"}, server=False)))

    def test_handler_server_port_failure(self):
        self.assertIsNone(H._handler_server_port(_Stub(server=False)))
        broken = _Stub()
        broken.server = mock.Mock()
        type(broken.server).server_port = mock.PropertyMock(side_effect=RuntimeError("x"))
        self.assertIsNone(H._handler_server_port(broken))

    def test_client_ip_and_loopback(self):
        self.assertEqual(H._client_ip(_Stub(ip="10.0.0.2")), "10.0.0.2")
        self.assertEqual(H._client_ip(_Stub(ip=None)), "127.0.0.1")
        self.assertTrue(H._is_loopback_client(_Stub(ip="127.0.0.1")))
        self.assertFalse(H._is_loopback_client(_Stub(ip="10.0.0.2")))

    def test_check_auth_branches(self):
        token_handler = _stub_handler({"X-API-Token": "test-token"})
        self.assertTrue(H.Handler._check_auth(token_handler))
        self.assertFalse(H.Handler._check_auth(_stub_handler({"X-API-Token": "bad"})))
        self.assertFalse(H.Handler._check_auth(_stub_handler({"X-API-Token": "tök"})))
        self.assertTrue(H.Handler._check_auth(_stub_handler({"Origin": "http://127.0.0.1:19100"})))
        self.assertTrue(H.Handler._check_auth(_stub_handler({"Origin": "http://localhost:19100"})))
        self.assertFalse(H.Handler._check_auth(_stub_handler({"Origin": "http://127.0.0.1:1234"})))
        self.assertFalse(H.Handler._check_auth(_stub_handler({"Origin": "http://evil.example.com:19100"})))
        self.assertTrue(H.Handler._check_auth(_stub_handler({"Referer": "http://127.0.0.1:19100/index.html"})))
        self.assertFalse(H.Handler._check_auth(_stub_handler({"Referer": "http://evil.example.com:19100/"})))
        self.assertFalse(H.Handler._check_auth(_stub_handler({"Origin": "http://[bad"})))
        self.assertFalse(H.Handler._check_auth(_stub_handler({"Referer": "http://[bad"})))
        self.assertFalse(H.Handler._check_auth(_stub_handler()))

    def test_check_auth_session(self):
        token = remote.sessions.create("192.168.1.20")
        cookie = f"mbes_session={token}"
        with mock.patch.object(remote, "enabled", return_value=True):
            self.assertTrue(H.Handler._check_auth(_stub_handler({"Cookie": cookie})))
            self.assertTrue(
                H.Handler._check_auth(
                    _stub_handler(
                        {"Cookie": cookie, "Origin": "http://192.168.1.10:19100", "Host": "192.168.1.10:19100"}
                    )
                )
            )
            self.assertFalse(
                H.Handler._check_auth(
                    _stub_handler({"Cookie": cookie, "Origin": "http://evil.example.com", "Host": "192.168.1.10:19100"})
                )
            )
            self.assertFalse(H.Handler._check_auth(_stub_handler({"Cookie": "mbes_session=bad"})))
            self.assertFalse(H.Handler._check_auth(_stub_handler({"Cookie": cookie, "Origin": "http://[bad"})))
        with mock.patch.object(remote, "enabled", return_value=False):
            self.assertFalse(H.Handler._check_auth(_stub_handler({"Cookie": cookie})))

    def test_login_page_html_and_redirect(self):
        self.assertIn('<p class="err">bad password</p>', H._login_page_html(19100, "bad password"))
        self.assertNotIn('class="err"', H._login_page_html(19100))
        stub = _Stub()
        stub.send_response = mock.Mock()
        stub.send_header = mock.Mock()
        stub.end_headers = mock.Mock()
        H._redirect(stub, "/")
        stub.send_response.assert_called_once_with(302)
        headers = {call.args[0] for call in stub.send_header.call_args_list}
        self.assertIn("Location", headers)
        self.assertIn("Content-Length", headers)

    def test_serve_login_branches(self):
        stub = _Stub()
        stub.send_response = mock.Mock()
        stub.send_header = mock.Mock()
        stub.end_headers = mock.Mock()
        stub.wfile = io.BytesIO()
        stub._check_auth = mock.Mock(return_value=True)
        with mock.patch.object(remote, "enabled", return_value=True):
            H.Handler._serve_login(stub)
        stub.send_response.assert_called_with(302)
        stub2 = _Stub()
        stub2._send = mock.Mock()
        with mock.patch.object(remote, "enabled", return_value=False):
            H.Handler._serve_login(stub2)
        stub2._send.assert_called_once()
        self.assertEqual(stub2._send.call_args[0][0], 200)

    def test_handle_logout(self):
        token = remote.sessions.create("127.0.0.1")
        stub = _Stub({"Cookie": f"mbes_session={token}"})
        for name in ("send_response", "send_header", "end_headers"):
            setattr(stub, name, mock.Mock())
        stub.wfile = io.BytesIO()
        H.Handler._handle_logout(stub)
        self.assertFalse(remote.sessions.validate(token))
        cookies = [call.args for call in stub.send_header.call_args_list]
        self.assertTrue(any("Max-Age=0" in value for key, value in cookies if key == "Set-Cookie"))

    def test_send_json_and_post_data(self):
        stub = _Stub()
        for name in ("send_response", "send_header", "end_headers"):
            setattr(stub, name, mock.Mock())
        stub.wfile = io.BytesIO()
        stub._send = lambda code, body, ctype="application/json; charset=utf-8": H.Handler._send(
            stub, code, body, ctype
        )
        H.Handler._send(stub, 200, "text", "text/plain")
        self.assertEqual(stub.wfile.getvalue(), b"text")
        H.Handler._json(stub, {"ok": True}, code=201)
        self.assertIn(b'"ok": true', stub.wfile.getvalue())
        stub._send = mock.Mock()
        H.Handler._json(stub, {"ok": True})
        stub._send.assert_called_once()

    def test_post_data_branches(self):
        def data(headers, body=b""):
            return types.SimpleNamespace(headers=headers, rfile=io.BytesIO(body))

        self.assertEqual(H.Handler._post_data(data({})), {})
        self.assertIsNone(H.Handler._post_data(data({"Content-Length": "abc"})))
        self.assertEqual(H.Handler._post_data(data({"Content-Length": "-3"})), {})
        self.assertIsNone(H.Handler._post_data(data({"Content-Length": str(H.MAX_BODY_BYTES + 1)})))
        self.assertEqual(H.Handler._post_data(data({"Content-Length": "3"}, b"bad")), {})
        self.assertEqual(
            H.Handler._post_data(data({"Content-Length": "13"}, b'{"ok": true}')),
            {"ok": True},
        )

    def test_log_helpers(self):
        stub = _Stub()
        with mock.patch("bedrock_server_manager.app_logger.get_app_logger") as logger:
            H.Handler._log(stub, "hello", "info")
        logger.return_value.log.assert_called_once_with("info", "hello")
        with mock.patch("bedrock_server_manager.app_logger.get_app_logger", side_effect=RuntimeError("no logger")):
            H.Handler._log(stub, "hello")

    def test_get_index_html_paths(self):
        real = H.get_index_html()
        self.assertIn("__API_TOKEN__", real)
        with mock.patch.object(H.os.path, "exists", return_value=False):
            self.assertIn("前端资源缺失", H.get_index_html())
        with (
            mock.patch.object(H.os.path, "exists", return_value=True),
            mock.patch("builtins.open", side_effect=OSError("permission")),
        ):
            self.assertIn("前端资源缺失", H.get_index_html())
        with tempfile.TemporaryDirectory() as tmp:
            _write(os.path.join(tmp, "web", "index.html"), "FROZEN-INDEX")
            with (
                mock.patch.object(sys, "frozen", True, create=True),
                mock.patch.object(sys, "_MEIPASS", tmp, create=True),
            ):
                self.assertEqual(H.get_index_html(), "FROZEN-INDEX")


class TestHandlerHttp(_HttpCase):
    def test_get_host_mismatch_403(self):
        status, _, body = self.request("GET", "/", host="evil.example.com")
        self.assertEqual(status, 403)
        self.assertIn("请求来源不合法", json.loads(body)["error"])

    def test_get_login_and_auth_status(self):
        status, _, body = self.request("GET", "/login")
        self.assertEqual(status, 200)
        self.assertIn("远程登录", body.decode())
        with mock.patch.object(H, "check_rate_limit", return_value=(True, 0)):
            status, _, body = self.request("GET", "/api/auth/status")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["authenticated"])
        self.assertTrue(payload["loopback"])

    def test_get_index_injects_token(self):
        status, headers, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers.get("Content-Type", ""))
        text = body.decode("utf-8", "ignore")
        self.assertNotIn("__API_TOKEN__", text)
        self.assertIn("test-token", text)
        with mock.patch.object(H, "check_rate_limit", return_value=(True, 0)):
            status, _, body = self.request("GET", "/index.html")
        self.assertEqual(status, 200)
        self.assertNotIn("__API_TOKEN__", body.decode("utf-8", "ignore"))

    def test_get_requires_auth(self):
        status, _, _ = self.request("GET", "/api/definitely-unknown")
        self.assertEqual(status, 403)

    def test_get_rate_limit_429(self):
        with mock.patch.object(H, "check_rate_limit", return_value=(False, 2.5)):
            status, _, body = self.request("GET", "/api/status", headers={"X-API-Token": "test-token"})
        self.assertEqual(status, 429)
        self.assertIn("retry_after", json.loads(body))

    def test_get_unknown_route_404(self):
        with mock.patch.object(H, "check_rate_limit", return_value=(True, 0)):
            status, _, body = self.request("GET", "/api/definitely-unknown", headers={"X-API-Token": "test-token"})
        self.assertEqual(status, 404)
        self.assertEqual(body, b"not found")

    def test_get_health_no_auth(self):
        status, _, body = self.request("GET", "/api/health")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])

    def test_get_route_dispatch(self):
        def fake_route(handler_self, q):
            handler_self._send(200, "routed")

        with (
            mock.patch.object(H, "check_rate_limit", return_value=(True, 0)),
            mock.patch.object(H.Handler, "_get_theme", fake_route),
        ):
            status, _, body = self.request("GET", "/api/theme", headers={"X-API-Token": "test-token"})
        self.assertEqual((status, body), (200, b"routed"))

    def test_get_sse_slot_limit_429(self):
        with (
            mock.patch.object(H, "check_rate_limit", return_value=(True, 0)),
            mock.patch.object(H, "_acquire_stream_slot", return_value=False),
        ):
            status, _, body = self.request("GET", "/api/console/stream", headers={"X-API-Token": "test-token"})
        self.assertEqual(status, 429)
        self.assertIn("控制台连接数", json.loads(body)["error"])

    def test_get_sse_acquire_release(self):
        def fake_stream(handler_self, q):
            handler_self._send(200, "stream")

        with (
            mock.patch.object(H, "check_rate_limit", return_value=(True, 0)),
            mock.patch.object(H.Handler, "_serve_console_stream", fake_stream),
        ):
            before = H._stream_slots._value
            status, _, body = self.request("GET", "/api/console/stream", headers={"X-API-Token": "test-token"})
            after = H._stream_slots._value
        self.assertEqual((status, body), (200, b"stream"))
        self.assertEqual(before, after)

    def test_post_host_mismatch_403(self):
        status, _, _ = self.request("POST", "/api/auth/logout", host="evil.example.com")
        self.assertEqual(status, 403)

    def test_post_requires_auth_403(self):
        status, _, _ = self.request("POST", "/api/theme", body=b"{}")
        self.assertEqual(status, 403)

    def test_post_rate_limit_429(self):
        with mock.patch.object(H, "check_rate_limit", return_value=(False, 1.25)):
            status, _, _ = self.request("POST", "/api/theme", headers={"X-API-Token": "test-token"}, body=b"{}")
        self.assertEqual(status, 429)

    def test_post_oversized_or_bad_length_413(self):
        payload = (
            f"POST /api/theme HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{self.port}\r\n"
            "X-API-Token: test-token\r\n"
            "Content-Length: abc\r\n"
            "\r\n"
        ).encode()
        with mock.patch.object(H, "check_rate_limit", return_value=(True, 0)):
            raw = self.raw_request(payload)
        self.assertIn(b"413", raw.split(b"\r\n", 1)[0])

    def test_post_unknown_route_404(self):
        with mock.patch.object(H, "check_rate_limit", return_value=(True, 0)):
            status, _, body = self.request(
                "POST", "/api/definitely-unknown", headers={"X-API-Token": "test-token"}, body=b"{}"
            )
        self.assertEqual((status, body), (404, b"not found"))

    def test_post_route_dispatch(self):
        def fake_route(handler_self, data):
            handler_self._send(200, json.dumps(data))

        with (
            mock.patch.object(H, "check_rate_limit", return_value=(True, 0)),
            mock.patch.object(H.Handler, H.Handler._post_routes["/api/theme"], fake_route),
        ):
            status, _, body = self.request(
                "POST", "/api/theme", headers={"X-API-Token": "test-token"}, body=b'{"theme": "dark"}'
            )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"theme": "dark"})

    def test_login_flow(self):
        self.settings["remote_password_hash"] = remote.hash_password("goodpass123")
        status, headers, body = self.request(
            "POST",
            "/api/auth/login",
            headers={"Content-Type": "application/json"},
            body=json.dumps({"password": "goodpass123"}).encode(),
        )
        self.assertEqual(status, 200)
        cookie = headers.get("Set-Cookie", "")
        self.assertIn("mbes_session=", cookie)
        token = remote.parse_cookie(cookie)
        self.assertTrue(remote.sessions.validate(token))
        status, headers, _ = self.request("POST", "/api/auth/logout", headers={"Cookie": f"mbes_session={token}"})
        self.assertEqual(status, 200)
        self.assertIn("Max-Age=0", headers.get("Set-Cookie", ""))
        self.assertFalse(remote.sessions.validate(token))

    def test_login_bad_requests(self):
        status, _, body = self.request(
            "POST", "/api/auth/login", headers={"Content-Type": "application/json"}, body=b"{}"
        )
        self.assertEqual(status, 400)
        self.assertIn("请输入密码", json.loads(body)["error"])
        status, _, _ = self.request("POST", "/api/auth/login", headers={"Content-Type": "application/json"}, body=b"[]")
        self.assertEqual(status, 400)
        self.settings["remote_password_hash"] = remote.hash_password("goodpass123")
        status, _, body = self.request(
            "POST",
            "/api/auth/login",
            headers={"Content-Type": "application/json"},
            body=json.dumps({"password": "wrongpass"}).encode(),
        )
        self.assertEqual(status, 401)
        self.assertIn("密码错误", json.loads(body)["error"])

    def test_login_rate_limited_429(self):
        with mock.patch.object(remote.login_limiter, "retry_after", return_value=12.0):
            status, _, body = self.request(
                "POST",
                "/api/auth/login",
                headers={"Content-Type": "application/json"},
                body=json.dumps({"password": "whatever"}).encode(),
            )
        self.assertEqual(status, 429)
        self.assertEqual(json.loads(body)["retry_after"], 12.0)

    def test_login_lock_message(self):
        self.settings["remote_password_hash"] = remote.hash_password("goodpass123")
        with mock.patch.object(remote.login_limiter, "record_failure", return_value=600.0):
            status, _, body = self.request(
                "POST",
                "/api/auth/login",
                headers={"Content-Type": "application/json"},
                body=json.dumps({"password": "wrongpass"}).encode(),
            )
        self.assertEqual(status, 401)
        self.assertIn("已锁定 600 秒", json.loads(body)["error"])

    def test_login_page_redirect_when_authenticated(self):
        with mock.patch.object(remote, "enabled", return_value=True):
            status, headers, _ = self.request("GET", "/login", headers={"X-API-Token": "test-token"})
        self.assertEqual(status, 302)
        self.assertEqual(headers.get("Location"), "/")


class _FakeConsole:
    def __init__(self, read=("", 0), drops=False, waits=None, error=None):
        self.read = read
        self.drops = drops
        self.waits = list(waits or [])
        self.error = error

    def read_since(self, since):
        if self.error:
            raise self.error
        return self.read

    def dropped_since(self, since):
        return self.drops

    def wait_for_new(self, since, timeout=1.0):
        return self.waits.pop(0) if self.waits else ("", 0)


class _WFile:
    def __init__(self, fail_at=1):
        self.fail_at = fail_at
        self.calls = 0
        self.written = []

    def write(self, data):
        self.calls += 1
        if self.calls >= self.fail_at:
            raise BrokenPipeError("client gone")
        self.written.append(data)

    def flush(self):
        return None


class TestHandlerConsoleStream(_SettingsCase):
    def _handler(self, wfile):
        handler = H.Handler.__new__(H.Handler)
        handler.wfile = wfile
        handler.send_response = mock.Mock()
        handler.send_header = mock.Mock()
        handler.end_headers = mock.Mock()
        return handler

    def tearDown(self):
        AppContext.instance().console = None
        super().tearDown()

    def test_stream_ping_and_invalid_since(self):
        handler = self._handler(_WFile(fail_at=1))
        AppContext.instance().console = _FakeConsole()
        handler._serve_console_stream({"since": ["bad"]})
        self.assertTrue(handler.wfile.written == [])

    def test_stream_text_reset_and_update(self):
        handler = self._handler(_WFile(fail_at=3))
        AppContext.instance().console = _FakeConsole(read=("first\n", 7), drops=True, waits=[("second\n", 8)])
        handler._serve_console_stream({"since": ["5"]})
        self.assertEqual(handler.wfile.calls, 3)
        self.assertIn(b"first", handler.wfile.written[0])
        self.assertIn(b"second", handler.wfile.written[1])
        self.assertIn(b"reset", handler.wfile.written[0])

    def test_stream_cursor_advance(self):
        handler = self._handler(_WFile(fail_at=1))
        AppContext.instance().console = _FakeConsole(waits=[("", 4), ("", 0)])
        handler._serve_console_stream({"since": ["0"]})
        self.assertEqual(handler.wfile.calls, 1)

    def test_stream_ping_flushes(self):
        handler = self._handler(_WFile(fail_at=2))
        AppContext.instance().console = _FakeConsole()
        handler._serve_console_stream({"since": ["0"]})
        self.assertEqual(handler.wfile.calls, 2)

    def test_stream_unexpected_error(self):
        handler = self._handler(_WFile())
        AppContext.instance().console = _FakeConsole(error=RuntimeError("console broke"))
        handler._serve_console_stream({"since": ["0"]})
        self.assertTrue(handler.send_response.called)

    def test_stream_slot_helpers(self):
        before = H._stream_slots._value
        self.assertTrue(H._acquire_stream_slot())
        H._release_stream_slot()
        H._release_stream_slot()  # 重复释放被抑制
        self.assertEqual(H._stream_slots._value, before)


class TestHandlerShutdown(_SettingsCase):
    def test_shutdown_later_success(self):
        AppContext.instance()
        handler = H.Handler.__new__(H.Handler)
        handler._log = mock.Mock()
        with (
            mock.patch.object(H.time, "sleep"),
            mock.patch("bedrock_server_manager.scheduler.stop_scheduler"),
            mock.patch.object(H.os, "_exit", side_effect=SystemExit(0)) as exit_mock,
            self.assertRaises(SystemExit),
        ):
            handler._shutdown_later()
        exit_mock.assert_called_once_with(0)

    def test_shutdown_later_inner_failures(self):
        ctx = AppContext.instance()
        saved = (ctx.server_proc, ctx.tray, ctx.httpd)
        handler = H.Handler.__new__(H.Handler)
        handler._log = mock.Mock()
        try:
            ctx.server_proc = types.SimpleNamespace(running=True, stop=mock.Mock(side_effect=RuntimeError("proc")))
            ctx.tray = types.SimpleNamespace(stop=mock.Mock(side_effect=RuntimeError("tray")))
            ctx.httpd = types.SimpleNamespace(shutdown=mock.Mock(side_effect=RuntimeError("httpd")))
            with (
                mock.patch.object(H.time, "sleep"),
                mock.patch("bedrock_server_manager.scheduler.stop_scheduler", side_effect=RuntimeError("sched")),
                mock.patch.object(H.os, "_exit", side_effect=SystemExit(0)),
                self.assertRaises(SystemExit),
            ):
                handler._shutdown_later()
            messages = " ".join(str(call.args[0]) for call in handler._log.call_args_list if call.args)
            self.assertIn("停止服务器进程失败", messages)
            self.assertIn("停止系统托盘失败", messages)
            self.assertIn("关闭 HTTP 服务器失败", messages)
        finally:
            ctx.server_proc, ctx.tray, ctx.httpd = saved

    def test_shutdown_later_outer_failure(self):
        handler = H.Handler.__new__(H.Handler)
        handler._log = mock.Mock()
        with (
            mock.patch.object(H.time, "sleep"),
            mock.patch("bedrock_server_manager.scheduler.stop_scheduler", side_effect=RuntimeError("sched")),
            mock.patch.object(H.os, "_exit", side_effect=[RuntimeError("exit failed"), SystemExit(1)]) as exit_mock,
        ):
            with self.assertRaises(SystemExit):
                handler._shutdown_later()
        self.assertEqual(exit_mock.call_args_list[-1], mock.call(1))


if __name__ == "__main__":
    unittest.main(verbosity=2)
