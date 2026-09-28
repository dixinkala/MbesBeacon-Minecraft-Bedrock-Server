"""
Minecraft 基岩版（Bedrock）服务器自动搭建管理工具

模块化包结构：
  - config.py    配置模块（PROP_META、配置校验、properties读写、配置历史）
  - console.py   控制台日志模块（ConsoleBuffer）
  - server.py    服务器进程模块（ServerProcess、进程启停）
  - install.py   安装下载模块（InstallState、下载、解压、版本检测）
  - backup.py    备份模块（世界备份、恢复、删除服务器）
  - players.py   玩家管理模块（玩家校验、权限、封禁、白名单）
  - utils.py     通用工具模块（设置管理、服务器检测、工具函数）
  - security.py  安全模块（指令校验、审计日志、URL校验）
  - scheduler.py 定时任务调度模块
  - tray.py      系统托盘模块（SystemTray）
  - state.py     全局状态管理（常量、全局变量、API_TOKEN）
  - assets.py    前端资源模块（HELP_TEXT、INDEX_HTML、HELP_JS）
  - web/         Web 子包（Handler、AppContext）
  - main.py      程序入口（main、_main）
"""

# 从 state.py 导入全局状态和常量
from http.server import ThreadingHTTPServer  # noqa: F401

# 初始化全局单例状态
from . import (
    constants,
    state as _state,
)

# 初始化全局单例状态（通过 AppContext 管理）
from .app_context import AppContext as _GlobalAppContext
from .app_logger import (  # noqa: F401
    get_app_logger,
    get_log_file_path,
    log_critical,
    log_debug,
    log_error,
    log_exception,
    log_info,
    log_warning,
    read_recent_logs,
    safe_log_exception,
    set_log_level,
)
from .app_update import (  # noqa: F401
    check_app_update,
    check_app_update_async,
)

# 从 assets.py 导入前端资源
from .assets import HELP_JS, HELP_TEXT, INDEX_HTML  # noqa: F401
from .backup import (  # noqa: F401
    backup_worlds,
    backup_worlds_zip,
    delete_backup,
    delete_server,
    get_backup_root,
    list_backups,
    restore_backup,
)
from .commands import (  # noqa: F401
    COMMANDS,
    autocomplete_command,
    get_all_commands,
    get_categories,
    get_command_help,
    get_command_info,
    get_command_suggestions,
    get_commands_by_category,
    is_dangerous_command,
)

# 从各模块导入公共接口
from .config import (  # noqa: F401
    CONFIG_HISTORY_DIR,
    FULL_PROP_META,
    PROP_META,
    backup_config_history,
    detect_file_encoding,
    list_config_history,
    load_properties,
    properties_to_dict,
    rollback_config,
    save_properties,
    validate_config_updates,
)
from .console import (
    ConsoleBuffer,  # noqa: F401
    ConsoleBuffer as _ConsoleBuffer,
)

# 常量从 constants.py 导入（推荐）
from .constants import (  # noqa: F401
    APP_DATA_DIR,
    APP_MARKER,
    APP_TITLE,
    APP_VERSION,
    CDN_TEMPLATE,
    DANGEROUS_COMMANDS,
    DEFAULT_PORT,
    IS_WINDOWS,
    LINKS_API,
    SERVER_EXE,
)
from .install import (  # noqa: F401
    DOWNLOAD_SOURCES,
    FALLBACK_VERSIONS,
    VERIFIED_VERSIONS,
    VERSION_CACHE_TTL,
    InstallState,
    InstallState as _InstallState,
    _http_get,
    detect_server_version,
    do_install,
    download_file,
    extract_zip,
    get_bedrock_versions,
    get_cached_versions,
    get_latest_server_info,
    make_download_url,
    select_server_dir,
)
from .main import _main, main  # noqa: F401
from .performance import (  # noqa: F401
    PerformanceMonitor,
    get_performance_monitor,
    get_server_performance_full,
    performance_monitor,
    reset_performance_monitor,
)
from .players import (  # noqa: F401
    BANNED_IPS_FILE,
    add_allowlist,
    ban_ip,
    get_all_player_status,
    list_installed_packs,
    load_banned_ips,
    pardon_ip,
    parse_online_players,
    read_allowlist,
    read_banlist,
    read_permissions,
    remove_allowlist,
    send_command_capture,
    validate_player_name,
)
from .ratelimit import RateLimiter, check_rate_limit, rate_limiter  # noqa: F401
from .scheduler import (  # noqa: F401
    SCHEDULED_TASKS_FILE,
    load_scheduled_tasks,
    save_scheduled_tasks,
    start_scheduler,
    stop_scheduler,
)
from .security import (  # noqa: F401
    DANGEROUS_ENDPOINTS,
    SAFE_COMMANDS,
    audit_log,
    check_dangerous_operation,
    get_dangerous_endpoints_info,
    is_dangerous_endpoint,
    read_audit_log,
    validate_command,
    validate_custom_url,
)
from .server import ServerProcess, get_server_proc, server_running  # noqa: F401

# 全局状态通过 AppContext 访问（推荐）
# API_TOKEN / app_start_time / settings 通过包级 __getattr__ 惰性代理到
# AppContext.instance()（见本文件底部），保证与当前实例一致；
# 以下纯兼容变量保留顶层绑定
from .state import (  # noqa: F401  # 向后兼容
    httpd,
    server_lock,
    server_proc,
)
from .tray import SystemTray  # noqa: F401
from .utils import (  # noqa: F401
    SETTINGS_FILE,
    _detect_cache,
    add_server_dir_history,
    browse_directory,
    check_disk_space,
    detect_servers,
    get_lan_ip,
    installed,
    load_settings,
    load_theme,
    open_browser_with_retry,
    props_path,
    resolve_server_dir,
    save_settings,
    save_theme,
    sync_log_file,
)
from .verify import (  # noqa: F401
    MAX_BDS_SIZE,
    MIN_BDS_SIZE,
    calculate_sha256,
    get_known_hashes,
    register_known_hash,
    verify_download_full,
    verify_file_size,
    verify_pe_signature,
    verify_sha256,
    verify_zip_integrity,
)
from .web.app import AppContext as WebAppContext  # noqa: F401
from .web.handler import Handler, get_index_html
from .web.route_decorator import register_route  # noqa: F401
from .worlds import (  # noqa: F401
    delete_world,
    duplicate_world,
    get_active_world_name,
    get_world_info,
    list_worlds,
    rename_world,
    set_active_world,
)

_global_ctx = _GlobalAppContext.instance()

# 注意：console / install_state 为 property，未设置时返回 Null 对象而非 None，
# 必须使用 has_console / has_install_state 判断（修复 ConsoleBuffer 从未创建的问题）
if not _global_ctx.has_console:
    _global_ctx.console = _ConsoleBuffer()
if not _global_ctx.has_install_state:
    _global_ctx.install_state = _InstallState()

# P1-1 批4：不再向 state.py 同步镜像；以下顶层绑定为包导入时的快照兼容导出
# （覆盖同名子模块属性；state.console / state.install_state 走 state 的惰性代理）
console = _global_ctx.console
install_state = _global_ctx.install_state

__version__ = "1.0.22"


def get_app_context():
    """获取全局 AppContext 实例（推荐使用此函数访问全局状态）。

    Returns:
        AppContext: 全局应用上下文实例

    示例:
        >>> from bedrock_server_manager import get_app_context
        >>> ctx = get_app_context()
        >>> ctx.log("服务器启动")
        >>> server_dir = ctx.server_dir
    """
    from .app_context import AppContext

    return AppContext.instance()


# 弃用说明：以下模块级全局变量保留用于向后兼容
# 新代码请使用 get_app_context() 或 AppContext.instance() 访问全局状态
# - settings → AppContext.instance().settings
# - console → AppContext.instance().console
# - install_state → AppContext.instance().install_state
# - server_proc → AppContext.instance().server_proc
# - httpd → AppContext.instance().httpd
# - API_TOKEN → AppContext.instance().api_token
# - app_start_time → AppContext.instance().app_start_time
# - server_lock → AppContext.instance().server_lock


def __getattr__(name: str):
    """P1-1 惰性代理：API_TOKEN / app_start_time / settings 转发到 state 的惰性代理。

    保证 AppContext.reset() 后模块级导出仍与当前实例一致
    （bsm.API_TOKEN / bsm.app_start_time / bsm.settings）。
    注意：console / install_state 不在此名单——同名子模块会遮蔽惰性代理，
    由上方顶层快照绑定提供兼容导出。
    """
    if name in ("API_TOKEN", "app_start_time", "settings"):
        return getattr(_state, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
