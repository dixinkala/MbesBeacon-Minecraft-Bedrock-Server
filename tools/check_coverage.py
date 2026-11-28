#!/usr/bin/env python3
"""逐文件覆盖率门槛检查。

策略（用户要求）：

- **重要模块**（数据/安全/协议核心）：单文件覆盖率 ≥ 85%
- **其余业务模块**：单文件覆盖率 ≥ 80%
- **入口/OS 胶水**（``main.py`` / ``tray.py``）：不做逐文件要求，由冒烟测试覆盖

在债务清零前，脚本支持 ``--baseline``（默认读取 ``coverage_baseline.json``）：
对尚未达标的文件只要求"不低于基线"，防止继续恶化；已达标文件仍按目标值严格检查。
用 ``--strict`` 忽略基线（债务清零后 CI 切换到此模式）。

用法：
    pytest --cov=bedrock_server_manager --cov-report=json:.coverage.json
    python tools/check_coverage.py            # 防回退模式（读取基线）
    python tools/check_coverage.py --strict   # 严格模式（债务清零后使用）
    python tools/check_coverage.py --update-baseline  # 记录当前值为新基线
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
COVERAGE_JSON = ROOT / ".coverage.json"
BASELINE_JSON = ROOT / "coverage_baseline.json"
PACKAGE = "bedrock_server_manager"

#: 重要模块：数据完整性、安全、安装/备份/世界、协议与远程
IMPORTANT = {
    "install.py",
    "backup.py",
    "worlds.py",
    "packs.py",
    "verify.py",
    "security.py",
    "server.py",
    "levilamina.py",
    "remote.py",
    "upnp.py",
    "config.py",
    "utils.py",
    "version_sources.py",
    "scheduler.py",
    "console.py",
    "ratelimit.py",
}
#: 入口 / OS 胶水：由冒烟测试覆盖，不做逐文件门槛
EXEMPT = {"main.py", "tray.py"}
IMPORTANT_TARGET = 85.0
DEFAULT_TARGET = 80.0
TOLERANCE = 0.05


def _relative(path: str) -> str:
    """把 coverage.json 里的路径规范成相对包路径（如 ``install.py``）。"""
    normalized = path.replace("\\", "/")
    marker = f"/{PACKAGE}/"
    if marker in normalized:
        return normalized.split(marker, 1)[1]
    if normalized.startswith(f"{PACKAGE}/"):
        return normalized[len(PACKAGE) + 1 :]
    return normalized


def load_measurements() -> dict[str, float]:
    if not COVERAGE_JSON.is_file():
        raise SystemExit(f"Missing {COVERAGE_JSON}; run pytest with --cov-report=json first")
    data = json.loads(COVERAGE_JSON.read_text(encoding="utf-8"))
    result: dict[str, float] = {}
    for path, info in data.get("files", {}).items():
        rel = _relative(path)
        if rel.endswith(".py"):
            result[rel] = float(info["summary"]["percent_covered"])
    return result


def load_baseline() -> dict[str, float]:
    if not BASELINE_JSON.is_file():
        return {}
    try:
        data = json.loads(BASELINE_JSON.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return {str(k): float(v) for k, v in data.get("files", {}).items()}


def write_baseline(measurements: dict[str, float]) -> None:
    previous = load_baseline()

    def floor(name: str, pct: float) -> float:
        target = IMPORTANT_TARGET if name in IMPORTANT else DEFAULT_TARGET
        if pct >= target:
            return target
        # 债务文件只允许向上调整，避免一次跑低就永久放宽
        return max(pct, previous.get(name, 0.0))

    payload = {
        "note": "逐文件覆盖率基线（债务清单）。达标文件按目标值检查；未达标文件不得低于此值。",
        "important_target": IMPORTANT_TARGET,
        "default_target": DEFAULT_TARGET,
        "files": {name: round(floor(name, pct), 2) for name, pct in sorted(measurements.items())},
    }
    BASELINE_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="逐文件覆盖率门槛检查")
    parser.add_argument("--strict", action="store_true", help="忽略债务基线，严格按目标值检查")
    parser.add_argument("--update-baseline", action="store_true", help="把当前覆盖率写为基线")
    args = parser.parse_args(argv)

    measurements = load_measurements()
    if args.update_baseline:
        write_baseline(measurements)
        print(f"Baseline updated: {BASELINE_JSON.relative_to(ROOT)} ({len(measurements)} files)")
        return 0

    baseline = {} if args.strict else load_baseline()
    failures: list[str] = []
    debt: list[tuple[str, float, float]] = []
    rows: list[tuple[str, float, float, str]] = []

    for name in sorted(measurements):
        if name in EXEMPT:
            continue
        pct = measurements[name]
        target = IMPORTANT_TARGET if name in IMPORTANT else DEFAULT_TARGET
        # 债务文件只要求不低于基线（防回退），不直接判失败
        floor = target if args.strict or pct >= target else baseline.get(name, 0.0)
        if pct + TOLERANCE < floor:
            failures.append(f"{name}: {pct:.1f}% < required {floor:.1f}%")
            status = "FAIL"
        elif pct + TOLERANCE < target:
            debt.append((name, pct, target))
            status = "debt"
        else:
            status = "ok"
        rows.append((name, pct, target, status))

    width = max(len(name) for name, *_ in rows) if rows else 20
    for name, pct, target, status in rows:
        print(f"  {name:<{width}}  {pct:5.1f}%  target {target:5.1f}%  [{status}]")

    if debt:
        print(f"\n[WARN] {len(debt)} file(s) still below target (ratchet mode: no regression):")
        for name, pct, target in debt:
            print(f"    {name:<{width}} {pct:5.1f}% -> {target:.0f}%")
    if failures:
        print("\n[FAIL] coverage gate not met:", file=sys.stderr)
        for line in failures:
            print(f"    {line}", file=sys.stderr)
        return 1
    print(f"\n[OK] coverage gate passed (important {IMPORTANT_TARGET:.0f}% / others {DEFAULT_TARGET:.0f}%)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
