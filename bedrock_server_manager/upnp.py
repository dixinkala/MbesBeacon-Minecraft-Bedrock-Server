"""UPnP IGD 客户端：自动为 BDS 的 UDP 端口做端口映射。

只依赖标准库：SSDP 组播发现网关 -> 读取设备描述 -> SOAP 调用
WANIPConnection / WANPPPConnection 服务完成增删查映射与查询公网 IP。

限制与安全：

- 只映射**游戏端口**（默认 UDP 19132），不碰管理端口的暴露面
- 只允许 UDP/TCP、端口 1-65535，内部地址必须是本机网卡地址
- 路由器未开启 UPnP 时抛 :class:`NoGatewayError`，由界面给出引导
"""

from __future__ import annotations

import ipaddress
import re
import socket
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from urllib.parse import urljoin
from xml.parsers import expat

SSDP_ADDR = "239.255.255.250"
SSDP_PORT = 1900
#: 依次尝试的 SSDP 搜索目标（先找网关设备，再找 WAN 连接服务）
SSDP_TARGETS = (
    "urn:schemas-upnp-org:device:InternetGatewayDevice:1",
    "urn:schemas-upnp-org:service:WANIPConnection:1",
    "urn:schemas-upnp-org:service:WANPPPConnection:1",
)
SERVICE_TYPES = (
    "urn:schemas-upnp-org:service:WANIPConnection:1",
    "urn:schemas-upnp-org:service:WANPPPConnection:1",
)
#: 网关返回的外部 IP 占位（未连接时常见值）
_UNUSED_IPS = frozenset({"0.0.0.0", ""})

_USER_AGENT = "MbesBeacon/UPnP"
_TIMEOUT = 5


class UpnpError(Exception):
    """UPnP 操作失败（网关返回错误或响应不可解析）。"""


class NoGatewayError(UpnpError):
    """没有发现支持 UPnP IGD 的网关。"""


def _local_name(tag: str) -> str:
    """去掉 XML 命名空间，返回本地标签名。"""
    return tag.rsplit("}", 1)[-1]


def _find_text(node, name: str) -> str:
    """在子树里按本地名找第一个非空文本。"""
    for child in node.iter():
        if _local_name(child.tag) == name and (child.text or "").strip():
            return child.text.strip()
    return ""


#: 网关 XML 体积上限，超过直接拒绝（正常描述/响应只有几 KB）
MAX_XML_BYTES = 256 * 1024


def safe_fromstring(text: str) -> ET.Element:
    """解析来自网络的 XML，拒绝 DTD / 实体声明与超大文档。

    用 ``xml.parsers.expat`` 直接构建元素树：注册 EntityDeclHandler 与
    ExternalEntityRefHandler 一律拒绝，并关闭参数实体解析，因此指数级实体
    （billion laughs）在声明阶段就失败，不会进入展开。
    """
    if not isinstance(text, str):
        raise UpnpError("XML 内容类型不正确")
    if len(text) > MAX_XML_BYTES:
        raise UpnpError(f"XML 超过 {MAX_XML_BYTES // 1024} KB，已拒绝")

    parser = expat.ParserCreate()
    parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
    holder: dict = {"root": None}
    stack: list[ET.Element] = []

    def start_element(name: str, attrs: dict) -> None:
        element = ET.Element(name, attrs)
        if stack:
            stack[-1].append(element)
        else:
            holder["root"] = element
        stack.append(element)

    def end_element(name: str) -> None:
        if stack:
            stack.pop()

    def char_data(data: str) -> None:
        if stack:
            stack[-1].text = (stack[-1].text or "") + data

    def reject_entity(*args, **kwargs):
        raise UpnpError("XML 包含实体声明，已拒绝解析")

    parser.StartElementHandler = start_element
    parser.EndElementHandler = end_element
    parser.CharacterDataHandler = char_data
    parser.EntityDeclHandler = reject_entity
    parser.UnparsedEntityDeclHandler = reject_entity
    parser.ExternalEntityRefHandler = reject_entity

    try:
        parser.Parse(text, True)
    except expat.ExpatError as exc:
        raise UpnpError(f"XML 解析失败: {exc}") from exc
    if holder["root"] is None:
        raise UpnpError("XML 没有根节点")
    return holder["root"]


def parse_ssdp_response(text: str) -> dict:
    """解析 SSDP 响应头（只取需要的字段）。"""
    headers: dict[str, str] = {}
    for line in text.splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            headers[key.strip().upper()] = value.strip()
    return {
        "location": headers.get("LOCATION", ""),
        "st": headers.get("ST", ""),
        "usn": headers.get("USN", ""),
        "server": headers.get("SERVER", ""),
    }


def ssdp_search(timeout: float = 3.0, targets: tuple = SSDP_TARGETS) -> list[dict]:
    """发出 M-SEARCH 并收集网关响应（去重后按 LOCATION 分组）。"""
    results: list[dict] = []
    seen: set[str] = set()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.settimeout(0.5)
        for target in targets:
            message = (
                "M-SEARCH * HTTP/1.1\r\n"
                f"HOST: {SSDP_ADDR}:{SSDP_PORT}\r\n"
                'MAN: "ssdp:discover"\r\n'
                "MX: 2\r\n"
                f"ST: {target}\r\n"
                "\r\n"
            ).encode("ascii", "ignore")
            try:
                sock.sendto(message, (SSDP_ADDR, SSDP_PORT))
            except OSError:
                continue
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                data, addr = sock.recvfrom(65507)
            except TimeoutError:
                continue
            except OSError:
                break
            text = data.decode("utf-8", "ignore")
            if not text.upper().startswith("HTTP/1.1 200"):
                continue
            parsed = parse_ssdp_response(text)
            location = parsed["location"]
            if not location or location in seen:
                continue
            seen.add(location)
            parsed["source"] = addr[0]
            results.append(parsed)
    finally:
        sock.close()
    return results


# --------------------------------------------------------------------------- 描述


def parse_description(xml_text: str, base_url: str = "") -> dict:
    """从设备描述里找出 WAN 连接服务的控制地址。

    Returns:
        dict: ``{"control_url", "service_type", "device_name"}``

    Raises:
        UpnpError: 描述里没有可用的 WAN 连接服务
    """
    root = safe_fromstring(xml_text)

    device_name = _find_text(root, "friendlyName")
    # 兼容两种结构：serviceList 直接挂在 device 下，或挂在嵌套的 WANDevice/WANConnectionDevice 下
    for service in root.iter():
        if _local_name(service.tag) != "service":
            continue
        service_type = _find_text(service, "serviceType")
        control_url = _find_text(service, "controlURL")
        if service_type in SERVICE_TYPES and control_url:
            return {
                "control_url": urljoin(base_url, control_url),
                "service_type": service_type,
                "device_name": device_name,
            }
    raise UpnpError("网关描述里没有 WANIPConnection / WANPPPConnection 服务")


def discover(timeout: float = 3.0) -> dict:
    """发现网关并返回可用的控制信息。

    Returns:
        dict: ``{"control_url", "service_type", "device_name", "external_ip", "source"}``

    Raises:
        NoGatewayError: 没有响应或响应里没有可用服务
    """
    responses = ssdp_search(timeout=timeout)
    if not responses:
        raise NoGatewayError("没有发现支持 UPnP 的网关（路由器可能未开启 UPnP）")
    last_error = ""
    for response in responses:
        try:
            xml_text = _fetch(response["location"])
            info = parse_description(xml_text, base_url=response["location"])
        except (UpnpError, OSError) as exc:
            last_error = str(exc)
            continue
        info["source"] = response.get("source", "")
        try:
            info["external_ip"] = get_external_ip(info)
        except UpnpError:
            info["external_ip"] = ""
        return info
    raise NoGatewayError(f"网关响应里没有可用的 UPnP 服务: {last_error}")


def _fetch(url: str, timeout: int = _TIMEOUT) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - 地址来自 SSDP 响应，仅内网
        return resp.read().decode("utf-8", "ignore")


# 网关信息缓存：每次发现要发组播并等 2-3 秒，界面轮询时不能每次都做
_gateway_cache: dict = {"time": 0.0, "info": None, "error": ""}


def gateway(force: bool = False, ttl: float = 60.0) -> dict:
    """带缓存的网关发现（失败也会缓存一小段时间，避免反复等超时）。"""
    now = time.time()
    age = now - float(_gateway_cache["time"] or 0.0)
    if not force and _gateway_cache["time"] and age < ttl:
        if _gateway_cache["info"]:
            return dict(_gateway_cache["info"])
        raise NoGatewayError(_gateway_cache["error"] or "没有发现支持 UPnP 的网关")
    try:
        info = discover()
    except UpnpError as exc:
        _gateway_cache.update({"time": now, "info": None, "error": str(exc)})
        raise
    _gateway_cache.update({"time": now, "info": dict(info), "error": ""})
    return info


def mapping_state(port: int, protocol: str = "UDP") -> dict:
    """返回端口映射状态：网关、是否已映射、公网 IP。"""
    result = {"available": False, "mapped": False, "external_ip": "", "internal_client": "", "error": ""}
    try:
        info = gateway()
        result["available"] = True
        result["external_ip"] = info.get("external_ip", "")
    except UpnpError as exc:
        result["error"] = str(exc)
        return result
    try:
        existing = get_mapping(port, protocol, gateway=info)
    except UpnpError as exc:
        result["error"] = str(exc)
        return result
    if existing:
        result["mapped"] = bool(existing.get("enabled"))
        result["internal_client"] = existing.get("internal_client", "")
    return result


# --------------------------------------------------------------------------- SOAP


def _soap(control_url: str, service_type: str, action: str, args: dict, timeout: int = _TIMEOUT) -> ET.Element:
    """调用一次 SOAP action，返回响应 XML 根节点。"""
    body_args = "".join(f"<{key}>{value}</{key}>" for key, value in args.items())
    body = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
        f'<s:Body><u:{action} xmlns:u="{service_type}">{body_args}</u:{action}></s:Body>'
        "</s:Envelope>"
    ).encode()
    req = urllib.request.Request(
        control_url,
        data=body,
        headers={
            "Content-Type": 'text/xml; charset="utf-8"',
            "SOAPACTION": f'"{service_type}#{action}"',
            "User-Agent": _USER_AGENT,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - 网关控制地址
            raw = resp.read().decode("utf-8", "ignore")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "ignore")
        if "UPnPError" in raw:
            fault = safe_fromstring(raw)
            code = _find_text(fault, "errorCode")
            desc = _find_text(fault, "errorDescription")
            raise UpnpError(f"网关返回错误 {code}: {desc}") from exc
        raise UpnpError(f"网关 HTTP {exc.code}") from exc
    except (TimeoutError, urllib.error.URLError, OSError) as exc:
        raise UpnpError(f"无法连接网关控制地址: {exc}") from exc
    return safe_fromstring(raw)


def _require_gateway(gateway: dict | None) -> dict:
    if gateway:
        return gateway
    return discover()


def _validate(port: int, protocol: str) -> None:
    if not isinstance(port, int) or not 1 <= port <= 65535:
        raise UpnpError(f"端口不合法: {port!r}")
    if protocol.upper() not in ("UDP", "TCP"):
        raise UpnpError(f"协议只能是 UDP 或 TCP: {protocol!r}")


def get_external_ip(gateway: dict | None = None) -> str:
    """查询网关的公网 IP（未连接时为 ``0.0.0.0``）。"""
    info = _require_gateway(gateway)
    root = _soap(info["control_url"], info["service_type"], "GetExternalIPAddress", {})
    value = _find_text(root, "NewExternalIPAddress")
    return "" if value in _UNUSED_IPS else value


def add_mapping(
    port: int,
    internal_ip: str,
    protocol: str = "UDP",
    description: str = "MbesBeacon-BDS",
    lease: int = 0,
    gateway: dict | None = None,
) -> dict:
    """添加端口映射（lease=0 表示永久，直到手动删除）。"""
    _validate(port, protocol)
    try:
        ipaddress.ip_address(internal_ip)
    except ValueError as exc:
        raise UpnpError(f"内部地址不合法: {internal_ip!r}") from exc
    info = _require_gateway(gateway)
    _soap(
        info["control_url"],
        info["service_type"],
        "AddPortMapping",
        {
            "NewRemoteHost": "",
            "NewExternalPort": port,
            "NewProtocol": protocol.upper(),
            "NewInternalPort": port,
            "NewInternalClient": internal_ip,
            "NewEnabled": 1,
            "NewPortMappingDescription": description,
            "NewLeaseDuration": lease,
        },
    )
    return {
        "port": port,
        "protocol": protocol.upper(),
        "internal_ip": internal_ip,
        "external_ip": get_external_ip(info),
    }


def delete_mapping(port: int, protocol: str = "UDP", gateway: dict | None = None) -> None:
    """删除端口映射。"""
    _validate(port, protocol)
    info = _require_gateway(gateway)
    _soap(
        info["control_url"],
        info["service_type"],
        "DeletePortMapping",
        {"NewRemoteHost": "", "NewExternalPort": port, "NewProtocol": protocol.upper()},
    )


def get_mapping(port: int, protocol: str = "UDP", gateway: dict | None = None) -> dict | None:
    """查询端口映射；不存在返回 None。"""
    _validate(port, protocol)
    info = _require_gateway(gateway)
    try:
        root = _soap(
            info["control_url"],
            info["service_type"],
            "GetSpecificPortMappingEntry",
            {"NewRemoteHost": "", "NewExternalPort": port, "NewProtocol": protocol.upper()},
        )
    except UpnpError as exc:
        # 714 = NoSuchEntryInArray；部分家用路由器（如返回 501 Entry not found）用 501 表示同义
        message = str(exc)
        if "714" in message or "501" in message:
            return None
        raise
    return {
        "port": port,
        "protocol": protocol.upper(),
        "internal_client": _find_text(root, "NewInternalClient"),
        "internal_port": _find_text(root, "NewInternalPort"),
        "enabled": _find_text(root, "NewEnabled") in ("1", "true", "True"),
        "description": _find_text(root, "NewPortMappingDescription"),
    }


def is_placeholder_ip(value: str) -> bool:
    """判断公网 IP 是否是占位值。"""
    return value.strip() in _UNUSED_IPS


_IP_RE = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")


def looks_like_ipv4(value: str) -> bool:
    return bool(_IP_RE.match(value.strip()))
