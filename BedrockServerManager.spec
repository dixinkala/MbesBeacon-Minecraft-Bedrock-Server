# -*- mode: python ; coding: utf-8 -*-
# UPX 压缩：安装 UPX 后自动生效（可将 exe 从 8MB 压缩到约 3MB）
# 安装方法：下载 UPX (https://upx.github.io/) 并将 upx.exe 加入 PATH
# 注意：UPX 可能被部分杀毒软件误报
# 可通过环境变量 MBES_NO_UPX=1 构建非 UPX 版本：set MBES_NO_UPX=1 && python -m PyInstaller BedrockServerManager.spec
import os
_UPX_ENABLED = os.environ.get('MBES_NO_UPX', '0') != '1'

# 包结构入口：run.py（避免相对导入问题）
# PyInstaller 会自动收集包内所有模块
a = Analysis(
    ['run.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('bedrock_server_manager/web/index.html', 'web'),  # 前端页面
        ('bedrock_server_manager/assets/mbesbeacon_icon.ico', 'assets'),  # 应用图标
    ],
    hiddenimports=[
        'bedrock_server_manager',
        'bedrock_server_manager.config',
        'bedrock_server_manager.console',
        'bedrock_server_manager.server',
        'bedrock_server_manager.install',
        'bedrock_server_manager.backup',
        'bedrock_server_manager.players',
        'bedrock_server_manager.utils',
        'bedrock_server_manager.security',
        'bedrock_server_manager.tray',
        'bedrock_server_manager.state',
        'bedrock_server_manager.web',
        'bedrock_server_manager.web.app',
        'bedrock_server_manager.web.handler',
        'bedrock_server_manager.worlds',
        'bedrock_server_manager.commands',
        'bedrock_server_manager.performance',
        'bedrock_server_manager.verify',
        'bedrock_server_manager.ratelimit',
        'bedrock_server_manager.app_logger',
        'bedrock_server_manager.scheduler',
        'bedrock_server_manager.app_context',
        'bedrock_server_manager.app_update',
        'bedrock_server_manager.constants',
        'bedrock_server_manager.packs',
        'bedrock_server_manager.web.route_decorator',
        'bedrock_server_manager.web.routes',
        'bedrock_server_manager.web.routes.backups',
        'bedrock_server_manager.web.routes.commands',
        'bedrock_server_manager.web.routes.config',
        'bedrock_server_manager.web.routes.console',
        'bedrock_server_manager.web.routes.get_routes',
        'bedrock_server_manager.web.routes.misc',
        'bedrock_server_manager.web.routes.packs',
        'bedrock_server_manager.web.routes.players',
        'bedrock_server_manager.web.routes.post_extra',
        'bedrock_server_manager.web.routes.server',
        'bedrock_server_manager.web.routes.worlds',
        'psutil',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    # 产物名必须固定为 MbesBeacon：build.ps1、ci.yml 的上传/签名/Release
    # 都按 dist/MbesBeacon.exe 定位产物。版本号通过 version_info.txt 写入
    # 文件属性，不要拼进文件名（早期写成 MbesBeacon1.0.24 导致构建链路全断）。
    name='MbesBeacon',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=_UPX_ENABLED,
    upx_exclude=[
        'vcruntime140.dll',  # UPX 压缩此 DLL 可能导致运行时错误
        'python3*.dll',
    ],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    version='version_info.txt',
    icon='bedrock_server_manager/assets/mbesbeacon_icon.ico',
)
