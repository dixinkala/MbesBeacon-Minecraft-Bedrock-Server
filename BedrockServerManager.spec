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
        'bedrock_server_manager.assets',
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
    name='MbesBeacon1.0.23',
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
