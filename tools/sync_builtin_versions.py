"""从自建版本清单生成内置快照模块（bedrock_server_manager/builtin_versions.py）。

内置快照是离线兜底：自建清单仓库与社区仓库都取不到时，程序用这份数据保证
版本下拉框和"是否官方版本"判断仍可用。

用法：
    python tools/sync_builtin_versions.py            # 拉清单并重写快照模块
    python tools/sync_builtin_versions.py --check     # 只检查快照是否落后（CI 用）
    python tools/sync_builtin_versions.py --from PATH # 用本地 versions.json 生成
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import urllib.request
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parent.parent
TARGET = ROOT / "bedrock_server_manager" / "builtin_versions.py"
MANIFEST_URL = "https://raw.githubusercontent.com/dixinkala/mbesbeacon-bds-versions/main/versions.json"
USER_AGENT = "MbesBeacon (+https://github.com/dixinkala/MbesBeacon-Minecraft-Bedrock-Server)"


def fetch_manifest(source: str) -> dict:
    if source.startswith(("http://", "https://")):
        req = urllib.request.Request(source, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
    else:
        raw = pathlib.Path(source).read_bytes()
    data = json.loads(raw.decode("utf-8", "ignore"))
    if not isinstance(data, dict):
        raise SystemExit("清单根节点必须是对象")
    for platform in ("windows", "linux"):
        if not isinstance(data.get(platform), dict):
            raise SystemExit(f"清单缺少 {platform} 段")
    return data


def render(manifest: dict) -> str:
    snapshot = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def lines(values: list[str], indent: str = "            ") -> str:
        return "\n".join(f'{indent}"{value}",' for value in values)

    blocks = []
    for platform in ("windows", "linux"):
        info = manifest[platform]
        blocks.append(
            f'    "{platform}": {{\n'
            f'        "stable": "{info.get("stable", "")}",\n'
            f'        "preview": "{info.get("preview", "")}",\n'
            f'        "versions": [\n{lines(list(info.get("versions") or []))}\n        ],\n'
            f'        "preview_versions": [\n{lines(list(info.get("preview_versions") or []))}\n        ],\n'
            f"    }},"
        )
    body = "\n".join(blocks)
    return (
        '"""内置 BDS 版本快照（离线兜底，请勿手改）。\n'
        "\n"
        "由 tools/sync_builtin_versions.py 从自建版本清单生成：\n"
        f"    {MANIFEST_URL}\n"
        "\n"
        "程序在自建清单与社区清单都取不到时使用本快照，保证版本列表与\n"
        '"是否官方版本"判断仍可用。更新方式：\n'
        "    python tools/sync_builtin_versions.py\n"
        '"""\n'
        "\n"
        f'BUILTIN_VERSIONS_SNAPSHOT = "{snapshot}"\n'
        "\n"
        "BUILTIN_VERSIONS = {\n"
        f"{body}\n"
        "}\n"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成内置版本快照")
    parser.add_argument("--check", action="store_true", help="只检查快照是否落后于线上清单")
    parser.add_argument("--from", dest="source", default=MANIFEST_URL, help="清单来源（URL 或本地路径）")
    args = parser.parse_args(argv)

    manifest = fetch_manifest(args.source)
    expected = render(manifest)

    if args.check:
        current = TARGET.read_text(encoding="utf-8") if TARGET.exists() else ""
        if current == expected:
            print(f"内置快照已是最新（stable={manifest['windows']['stable']}）")
            return 0
        print(
            f"内置快照落后：请运行 python tools/sync_builtin_versions.py\n"
            f"  线上 stable={manifest['windows']['stable']}，"
            f"versions={len(manifest['windows']['versions'])}",
            file=sys.stderr,
        )
        return 1

    TARGET.write_text(expected, encoding="utf-8", newline="\n")
    print(
        f"已写入 {TARGET.relative_to(ROOT)}: "
        f"windows stable={manifest['windows']['stable']} "
        f"({len(manifest['windows']['versions'])} 稳定版 / "
        f"{len(manifest['windows']['preview_versions'])} 预览版)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
