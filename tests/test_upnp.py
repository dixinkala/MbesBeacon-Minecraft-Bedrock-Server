"""UPnP 客户端测试：SSDP 解析、设备描述解析、SOAP 增删查（假网关）。"""

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import TestCase, main, mock

from bedrock_server_manager import upnp

SERVICE = "urn:schemas-upnp-org:service:WANIPConnection:1"

DESCRIPTION = """<?xml version="1.0"?>
<root xmlns="urn:schemas-upnp-org:device-1-0">
  <device>
    <friendlyName>Test Router</friendlyName>
    <deviceList>
      <device>
        <deviceType>urn:schemas-upnp-org:device:WANDevice:1</deviceType>
        <deviceList>
          <device>
            <deviceType>urn:schemas-upnp-org:device:WANConnectionDevice:1</deviceType>
            <serviceList>
              <service>
                <serviceType>urn:schemas-upnp-org:service:WANIPConnection:1</serviceType>
                <controlURL>/ctl/IPConn</controlURL>
                <eventSubURL>/evt/IPConn</eventSubURL>
                <SCPDURL>/WANIPCn.xml</SCPDURL>
              </service>
            </serviceList>
          </device>
        </deviceList>
      </device>
    </deviceList>
  </device>
</root>"""

SOAP_OK = (
    '<?xml version="1.0"?>'
    '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">'
    '<s:Body><u:{action}Response xmlns:u="{svc}">{extra}</u:{action}Response></s:Body>'
    "</s:Envelope>"
)

SOAP_FAULT = (
    '<?xml version="1.0"?>'
    '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body>'
    "<s:Fault><faultcode>s:Client</faultcode><detail><UPnPError>"
    "<errorCode>714</errorCode><errorDescription>NoSuchEntryInArray</errorDescription>"
    "</UPnPError></detail></s:Fault></s:Body></s:Envelope>"
)


class FakeGateway(BaseHTTPRequestHandler):
    mappings: dict = {}
    requests: list = []

    def log_message(self, *args):  # noqa: A003 - 静默日志
        pass

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler 约定
        if self.path.endswith("root.xml"):
            body = DESCRIPTION.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/xml")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_error(404)

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8", "ignore")
        action = (self.headers.get("SOAPACTION") or "").strip('"').split("#")[-1]
        FakeGateway.requests.append((action, raw))
        root = upnp.safe_fromstring(raw)

        def arg(name):
            for node in root.iter():
                if node.tag.rsplit("}", 1)[-1] == name:
                    return node.text or ""
            return ""

        extra = ""
        if action == "GetExternalIPAddress":
            extra = "<NewExternalIPAddress>203.0.113.7</NewExternalIPAddress>"
        elif action == "AddPortMapping":
            FakeGateway.mappings[(arg("NewProtocol"), arg("NewExternalPort"))] = arg("NewInternalClient")
            body = (SOAP_OK.format(action=action, svc=SERVICE, extra="")).encode()
            self._send(body)
            return
        elif action == "DeletePortMapping":
            FakeGateway.mappings.pop((arg("NewProtocol"), arg("NewExternalPort")), None)
        elif action == "GetSpecificPortMappingEntry":
            key = (arg("NewProtocol"), arg("NewExternalPort"))
            if key not in FakeGateway.mappings:
                body = SOAP_FAULT.encode()
                self.send_response(500)
                self.send_header("Content-Type", "text/xml")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            extra = (
                f"<NewInternalPort>{key[1]}</NewInternalPort>"
                f"<NewInternalClient>{FakeGateway.mappings[key]}</NewInternalClient>"
                "<NewEnabled>1</NewEnabled>"
                "<NewPortMappingDescription>MbesBeacon-BDS</NewPortMappingDescription>"
            )
        body = (SOAP_OK.format(action=action, svc=SERVICE, extra=extra)).encode()
        self._send(body)

    def _send(self, body: bytes):
        self.send_response(200)
        self.send_header("Content-Type", "text/xml")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class TestParsing(TestCase):
    def test_parse_ssdp_response(self):
        text = (
            "HTTP/1.1 200 OK\r\n"
            "CACHE-CONTROL: max-age=120\r\n"
            "LOCATION: http://192.168.1.1:5000/rootDesc.xml\r\n"
            "SERVER: Linux/3.4 UPnP/1.0 Router/1.0\r\n"
            "ST: urn:schemas-upnp-org:device:InternetGatewayDevice:1\r\n"
            "USN: uuid:abc::urn:schemas-upnp-org:device:InternetGatewayDevice:1\r\n\r\n"
        )
        parsed = upnp.parse_ssdp_response(text)
        self.assertEqual(parsed["location"], "http://192.168.1.1:5000/rootDesc.xml")
        self.assertIn("InternetGatewayDevice", parsed["st"])

    def test_parse_description_finds_service(self):
        info = upnp.parse_description(DESCRIPTION, base_url="http://192.168.1.1:5000/rootDesc.xml")
        self.assertEqual(info["service_type"], SERVICE)
        self.assertEqual(info["control_url"], "http://192.168.1.1:5000/ctl/IPConn")
        self.assertEqual(info["device_name"], "Test Router")

    def test_parse_description_without_service(self):
        xml = "<root><device><friendlyName>x</friendlyName></device></root>"
        with self.assertRaises(upnp.UpnpError):
            upnp.parse_description(xml)

    def test_parse_description_invalid_xml(self):
        with self.assertRaises(upnp.UpnpError):
            upnp.parse_description("not xml")

    def test_validation(self):
        for port in (0, 70000, "19132"):
            with self.assertRaises(upnp.UpnpError):
                upnp.add_mapping(port, "192.168.1.10")
        with self.assertRaises(upnp.UpnpError):
            upnp.add_mapping(19132, "192.168.1.10", protocol="SCTP")
        with self.assertRaises(upnp.UpnpError):
            upnp.add_mapping(19132, "not-an-ip")

    def test_safe_fromstring_rejects_entities(self):
        bomb = (
            '<?xml version="1.0"?>'
            '<!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;&lol;">]>'
            "<root>&lol2;</root>"
        )
        with self.assertRaises(upnp.UpnpError):
            upnp.safe_fromstring(bomb)

    def test_safe_fromstring_rejects_oversize(self):
        with self.assertRaises(upnp.UpnpError):
            upnp.safe_fromstring("<root>" + ("x" * (upnp.MAX_XML_BYTES + 1)) + "</root>")

    def test_safe_fromstring_accepts_plain_xml(self):
        self.assertIsNotNone(upnp.safe_fromstring("<root><a>1</a></root>"))

    def test_placeholder_ip(self):
        self.assertTrue(upnp.is_placeholder_ip("0.0.0.0"))
        self.assertFalse(upnp.is_placeholder_ip("203.0.113.7"))
        self.assertTrue(upnp.looks_like_ipv4("203.0.113.7"))
        self.assertFalse(upnp.looks_like_ipv4("example.com"))


class TestFakeGatewayFlow(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeGateway)
        cls.port = cls.server.server_port
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.gateway = {
            "control_url": f"http://127.0.0.1:{cls.port}/ctl/IPConn",
            "service_type": SERVICE,
            "device_name": "Test Router",
            "external_ip": "203.0.113.7",
        }

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        FakeGateway.mappings = {}
        FakeGateway.requests = []

    def test_external_ip(self):
        self.assertEqual(upnp.get_external_ip(self.gateway), "203.0.113.7")

    def test_add_query_delete(self):
        result = upnp.add_mapping(19132, "192.168.1.10", gateway=self.gateway)
        self.assertEqual(result["port"], 19132)
        self.assertEqual(result["external_ip"], "203.0.113.7")

        mapping = upnp.get_mapping(19132, gateway=self.gateway)
        self.assertIsNotNone(mapping)
        self.assertEqual(mapping["internal_client"], "192.168.1.10")
        self.assertTrue(mapping["enabled"])

        upnp.delete_mapping(19132, gateway=self.gateway)
        self.assertIsNone(upnp.get_mapping(19132, gateway=self.gateway))

    def test_soap_request_contains_fields(self):
        upnp.add_mapping(19133, "192.168.1.11", protocol="UDP", gateway=self.gateway)
        action, raw = [item for item in FakeGateway.requests if item[0] == "AddPortMapping"][-1]
        self.assertIn("<NewExternalPort>19133</NewExternalPort>", raw)
        self.assertIn("<NewProtocol>UDP</NewProtocol>", raw)
        self.assertIn("<NewInternalClient>192.168.1.11</NewInternalClient>", raw)
        self.assertIn("<NewLeaseDuration>0</NewLeaseDuration>", raw)

    def test_fault_code_714_returns_none(self):
        self.assertIsNone(upnp.get_mapping(65000, gateway=self.gateway))

    def test_router_specific_501_means_missing(self):
        """部分家用路由器用 501 表示"条目不存在"，应按未映射处理。"""
        with mock.patch.object(upnp, "_soap", side_effect=upnp.UpnpError("网关返回错误 501: Entry not found")):
            self.assertIsNone(upnp.get_mapping(19132, gateway=self.gateway))

    def test_mapping_state(self):
        upnp.add_mapping(19132, "192.168.1.10", gateway=self.gateway)
        with mock.patch.object(upnp, "gateway", return_value=self.gateway):
            state = upnp.mapping_state(19132)
        self.assertTrue(state["available"])
        self.assertTrue(state["mapped"])
        self.assertEqual(state["external_ip"], "203.0.113.7")


if __name__ == "__main__":
    main(verbosity=2)
