"""前端资源模块：HELP_TEXT、INDEX_HTML、HELP_JS。"""

import json

HELP_TEXT = """【Minecraft 基岩版服务器管理器 — 使用帮助】

详细帮助请查看程序内「使用帮助」标签页。
"""

INDEX_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="utf-8"><title>资源缺失</title>
<style>body{font-family:system-ui;background:#0d1117;color:#e6edf3;display:flex;align-items:center;justify-content:center;height:100vh;margin:0}
.box{text-align:center;padding:40px;max-width:500px}
h1{color:#f85149;font-size:24px}p{color:#8b949e;line-height:1.6}
a{color:#58a6ff}</style></head>
<body><div class="box">
<h1>前端资源缺失</h1>
<p>程序未能找到 web/index.html 前端资源文件。<br>这可能是由于打包不完整或文件被误删。</p>
<p>请重新下载完整的程序包，确保 web 目录与 exe 在同一目录下。</p>
<p style="margin-top:20px;font-size:12px">管理界面地址：<span id="url"></span></p>
<script>document.getElementById('url').textContent=location.href</script>
</div></body></html>"""

HELP_JS = "const HELP=" + json.dumps(HELP_TEXT, ensure_ascii=False) + ";"
INDEX_HTML = INDEX_HTML.replace(
    "let tabs=document.querySelectorAll('.tab');", HELP_JS + "\nlet tabs=document.querySelectorAll('.tab');"
)
