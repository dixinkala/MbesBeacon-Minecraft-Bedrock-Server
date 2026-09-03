"""
PyInstaller 打包入口脚本。
避免相对导入问题，直接导入包并运行 main。
"""

import os
import sys

# 确保包目录在 sys.path 中
script_dir = os.path.dirname(os.path.abspath(__file__))
if script_dir not in sys.path:
    sys.path.insert(0, script_dir)

# 导入包并运行 main
from bedrock_server_manager.main import main

if __name__ == "__main__":
    main()
