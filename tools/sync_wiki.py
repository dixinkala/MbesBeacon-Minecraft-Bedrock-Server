"""把 docs/wiki 同步到 GitHub Wiki 仓库。

用法：
    python tools/sync_wiki.py                 # 同步并推送
    python tools/sync_wiki.py --dry-run       # 只显示将要发生的变化，不接触远端
    python tools/sync_wiki.py --no-push       # 在本地 wiki 克隆里提交，但不推送
    python tools/sync_wiki.py --prune         # 额外删除 Wiki 上已不在 docs/wiki 的页面

约定：
    docs/wiki/README.md  ->  Wiki 的 Home 页
    docs/wiki/01-xxx.md  ->  Wiki 的 01-xxx 页（页面名 = 文件名去掉 .md）
    页内链接   (01-xxx.md) -> (01-xxx)，(README.md) -> (Home)
    仓库文件   (../LICENSE) 等 -> GitHub 绝对链接（Wiki 里没有这些文件）

只依赖标准库与 git 命令，Python 3.10+。
"""

from __future__ import annotations

import argparse
import contextlib
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = REPO_ROOT / "docs" / "wiki"

PAGE_LINK_RE = re.compile(r"\]\(((?:en-)?\d{2}-[a-z0-9-]+)\.md(#[^)]+)?\)")

# 文档里指向仓库其他文件的相对链接：Wiki 没有这些文件，改为 main 分支绝对地址
REPO_FILE_LINKS = {
    "../LICENSE": "LICENSE",
    "../README.md": "README.md",
    "../README_EN.md": "README_EN.md",
    "../SECURITY.md": "SECURITY.md",
    "../SECURITY_EN.md": "SECURITY_EN.md",
    "../CONTRIBUTING.md": "CONTRIBUTING.md",
    "../CONTRIBUTING_EN.md": "CONTRIBUTING_EN.md",
    "../CODE_OF_CONDUCT.md": "CODE_OF_CONDUCT.md",
}

SIDEBAR_HEADER = "**MbesBeacon 文档 / Docs**"
FOOTER_TEXT = "文档源文件：仓库内 `docs/wiki/` · 许可证：MIT"

#: 英文页面前缀（docs/wiki/en-*.md -> Wiki 页面 en-*）
EN_PREFIX = "en-"


def page_name(filename: str) -> str:
    """文件名 -> Wiki 页面名（README 映射到 Home）。"""
    if filename == "README.md":
        return "Home"
    return filename[: -len(".md")] if filename.endswith(".md") else filename


def convert_links(text: str, blob_base: str) -> str:
    """把仓库内相对链接改成 Wiki 页面名或 GitHub 绝对地址。"""
    for relative, target in REPO_FILE_LINKS.items():
        text = text.replace(f"]({relative})", f"]({blob_base}/{target})")
    text = text.replace("](README.md)", "](Home)")
    text = text.replace("](English.md)", "](English)")
    text = PAGE_LINK_RE.sub(lambda m: f"]({m.group(1)}{m.group(2) or ''})", text)
    return text


def build_sidebar(pages: list[str], repo_url: str) -> str:
    """生成 _Sidebar.md：中文与英文分组，首页在首位。"""
    zh_pages = [name for name in pages if not name.startswith(EN_PREFIX) and name != "English"]
    en_pages = [name for name in pages if name.startswith(EN_PREFIX)]
    lines = [SIDEBAR_HEADER, "", "**中文**", "", "- [首页](Home)"]
    lines += [f"- [{name}]({name})" for name in zh_pages]
    lines += ["", "**English**", "", "- [English home](English)"]
    lines += [f"- [{name}]({name})" for name in en_pages]
    lines += ["", "---", "", f"- [源码仓库]({repo_url})", f"- [最新 Release]({repo_url}/releases/latest)"]
    return "\n".join(lines) + "\n"


def run(cmd: list[str], cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, text=True, encoding="utf-8", capture_output=True, check=check)


def remove_tree(path: Path, attempts: int = 4) -> None:
    """删除临时目录，失败时重试。

    git 会把 ``.git/objects/pack/*.pack`` 标成只读，Windows 上 ``shutil.rmtree``
    遇到只读文件会失败；而 ``ignore_errors=True`` 会把失败吞掉、静默留下半个目录。
    因此先清掉只读属性再删，并对句柄延迟释放的情况重试。
    """
    for attempt in range(attempts):
        if not path.exists():
            return
        for root, _dirs, files in os.walk(path):
            for name in files:
                with contextlib.suppress(OSError):
                    os.chmod(os.path.join(root, name), stat.S_IWRITE)
        shutil.rmtree(path, ignore_errors=True)
        if not path.exists():
            return
        time.sleep(0.2 * (attempt + 1))


def origin_url() -> str:
    """读取 origin 地址，并推导 GitHub 仓库主页与 wiki 仓库地址。"""
    remote = run(["git", "remote", "get-url", "origin"], cwd=REPO_ROOT).stdout.strip()
    if remote.startswith("git@"):
        # git@github.com:owner/repo.git
        path = remote.split(":", 1)[1]
        base = "https://github.com/" + path
    else:
        base = remote
    base = base.removesuffix(".git")
    if not base.startswith("https://github.com/"):
        raise SystemExit(f"无法从 origin 推导 GitHub 地址: {remote}")
    return base


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="把 docs/wiki 同步到 GitHub Wiki")
    parser.add_argument("--dry-run", action="store_true", help="只显示改动，不克隆/提交/推送")
    parser.add_argument("--no-push", action="store_true", help="只提交到本地克隆，不推送")
    parser.add_argument("--prune", action="store_true", help="删除 Wiki 上多余的页面")
    parser.add_argument("--keep-workdir", action="store_true", help="保留临时克隆目录便于排查")
    args = parser.parse_args(argv)

    if not SRC_DIR.is_dir():
        print(f"找不到文档目录: {SRC_DIR}", file=sys.stderr)
        return 2

    sources = sorted(p for p in SRC_DIR.glob("*.md"))
    if not sources:
        print(f"{SRC_DIR} 下没有 markdown 文件", file=sys.stderr)
        return 2

    repo_url = origin_url()
    wiki_url = repo_url + ".wiki.git"
    blob_base = repo_url + "/blob/main"
    chapter_pages = [page_name(p.name) for p in sources if p.name != "README.md"]
    zh_count = sum(1 for p in chapter_pages if not p.startswith(EN_PREFIX) and p != "English")
    en_count = sum(1 for p in chapter_pages if p.startswith(EN_PREFIX))

    if args.dry_run:
        print(f"源目录: {SRC_DIR}")
        print(f"目标:   {wiki_url}")
        for path in sources:
            text = convert_links(path.read_text(encoding="utf-8"), blob_base)
            print(f"  {path.name:<28} -> {page_name(path.name):<22} {len(text):>5} 字符")
        print(f"侧边栏: 中文 {zh_count} 章 / English {en_count} 章 + 首页")
        return 0

    work = Path(tempfile.mkdtemp(prefix="mbes_wiki_"))
    clone = work / "wiki"
    try:
        print(f"克隆 {wiki_url}")
        result = run(["git", "clone", "--depth", "1", wiki_url, str(clone)], check=False)
        if result.returncode != 0:
            print(result.stderr.strip(), file=sys.stderr)
            print("提示：仓库 Wiki 需先在网页端创建第一个页面，之后才能 clone。", file=sys.stderr)
            return 2

        written = []
        for path in sources:
            target = clone / f"{page_name(path.name)}.md"
            text = convert_links(path.read_text(encoding="utf-8"), blob_base)
            target.write_text(text, encoding="utf-8", newline="\n")
            written.append(target.name)

        (clone / "_Sidebar.md").write_text(build_sidebar(chapter_pages, repo_url), encoding="utf-8", newline="\n")
        (clone / "_Footer.md").write_text(FOOTER_TEXT + "\n", encoding="utf-8", newline="\n")

        if args.prune:
            keep = set(written) | {"_Sidebar.md", "_Footer.md"}
            for stale in clone.glob("*.md"):
                if stale.name not in keep:
                    stale.unlink()
                    print(f"删除多余页面: {stale.name}")

        run(["git", "add", "-A"], cwd=clone)
        status = run(["git", "status", "--porcelain"], cwd=clone).stdout.strip()
        if not status:
            print("Wiki 已是最新，无需提交")
            return 0
        print("变更:\n" + "\n".join(f"  {line}" for line in status.splitlines()))

        run(
            [
                "git",
                "-c",
                "user.name=MbesBeacon Docs",
                "-c",
                "user.email=mbesbeacon@local.dev",
                "commit",
                "-m",
                "docs: 同步 docs/wiki 到 Wiki",
            ],
            cwd=clone,
        )
        if args.no_push:
            print(f"已提交（未推送），克隆保留在 {clone}")
            return 0

        run(["git", "push", "origin", "HEAD"], cwd=clone)
        print("已推送到 GitHub Wiki")
        return 0
    finally:
        if args.keep_workdir:
            print(f"临时目录保留: {work}")
        else:
            remove_tree(work)


if __name__ == "__main__":
    sys.exit(main())
