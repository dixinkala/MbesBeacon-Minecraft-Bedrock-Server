"""
命令自动补全模块：提供 Minecraft 基岩版服务器命令的自动补全和帮助信息。
"""

from .constants import DANGEROUS_COMMANDS

# Minecraft 基岩版服务器常用命令列表
COMMANDS = {
    # 玩家管理
    "list": {"desc": "列出在线玩家", "usage": "list", "category": "玩家"},
    "kick": {"desc": "踢出玩家", "usage": "kick <玩家名> [原因]", "category": "玩家", "args": ["<玩家名>", "[原因]"]},
    "op": {"desc": "给予玩家管理员权限", "usage": "op <玩家名>", "category": "玩家", "args": ["<玩家名>"]},
    "deop": {"desc": "移除玩家管理员权限", "usage": "deop <玩家名>", "category": "玩家", "args": ["<玩家名>"]},
    "permission": {
        "desc": "设置玩家权限等级",
        "usage": "permission <玩家名> <visitor|member|operator>",
        "category": "玩家",
        "args": ["<玩家名>", "<visitor|member|operator>"],
    },
    "gamemode": {
        "desc": "设置游戏模式",
        "usage": "gamemode <survival|creative|adventure|spectator> [玩家名]",
        "category": "玩家",
        "args": ["<survival|creative|adventure|spectator>", "[玩家名]"],
    },
    # 白名单/黑名单
    "whitelist": {
        "desc": "白名单管理",
        "usage": "whitelist <add|remove|list|on|off> [玩家名]",
        "category": "权限",
        "args": ["<add|remove|list|on|off>", "[玩家名]"],
    },
    "allowlist": {
        "desc": "允许列表管理（新版白名单）",
        "usage": "allowlist <add|remove|list|on|off> [玩家名]",
        "category": "权限",
        "args": ["<add|remove|list|on|off>", "[玩家名]"],
    },
    "ban": {"desc": "封禁玩家", "usage": "ban <玩家名> [原因]", "category": "权限", "args": ["<玩家名>", "[原因]"]},
    "pardon": {"desc": "解封玩家", "usage": "pardon <玩家名>", "category": "权限", "args": ["<玩家名>"]},
    "ban-ip": {
        "desc": "封禁IP地址",
        "usage": "ban-ip <IP地址> [原因]",
        "category": "权限",
        "args": ["<IP地址>", "[原因]"],
    },
    "pardon-ip": {"desc": "解封IP地址", "usage": "pardon-ip <IP地址>", "category": "权限", "args": ["<IP地址>"]},
    # 服务器管理
    "stop": {"desc": "停止服务器", "usage": "stop", "category": "服务器", "dangerous": True},
    "restart": {"desc": "重启服务器", "usage": "restart", "category": "服务器", "dangerous": True},
    "save": {
        "desc": "保存世界",
        "usage": "save <hold|resume|query>",
        "category": "服务器",
        "args": ["<hold|resume|query>"],
    },
    "save-all": {"desc": "立即保存所有世界数据", "usage": "save-all", "category": "服务器"},
    "save-off": {"desc": "禁用自动保存", "usage": "save-off", "category": "服务器"},
    "save-on": {"desc": "启用自动保存", "usage": "save-on", "category": "服务器"},
    "status": {"desc": "查看服务器状态", "usage": "status", "category": "服务器"},
    "tickingarea": {
        "desc": "管理常加载区域",
        "usage": "tickingarea <add|remove|list|remove_all> ...",
        "category": "服务器",
    },
    # 游戏设置
    "difficulty": {
        "desc": "设置难度",
        "usage": "difficulty <peaceful|easy|normal|hard>",
        "category": "游戏",
        "args": ["<peaceful|easy|normal|hard>"],
    },
    "time": {
        "desc": "设置时间",
        "usage": "time <set|add|query> <值>",
        "category": "游戏",
        "args": ["<set|add|query>", "<值>"],
    },
    "weather": {
        "desc": "设置天气",
        "usage": "weather <clear|rain|thunder> [持续时间]",
        "category": "游戏",
        "args": ["<clear|rain|thunder>", "[持续时间]"],
    },
    "gamerule": {
        "desc": "设置游戏规则",
        "usage": "gamerule <规则名> <值>",
        "category": "游戏",
        "args": ["<规则名>", "<值>"],
    },
    "setworldspawn": {
        "desc": "设置世界出生点",
        "usage": "setworldspawn [x y z]",
        "category": "游戏",
        "args": ["[x y z]"],
    },
    "defaultgamemode": {
        "desc": "设置默认游戏模式",
        "usage": "defaultgamemode <survival|creative|adventure>",
        "category": "游戏",
        "args": ["<survival|creative|adventure>"],
    },
    # 玩家动作
    "give": {
        "desc": "给予玩家物品",
        "usage": "give <玩家名> <物品名> [数量] [数据值]",
        "category": "物品",
        "args": ["<玩家名>", "<物品名>", "[数量]", "[数据值]"],
    },
    "tp": {
        "desc": "传送玩家",
        "usage": "tp <玩家名> <目标玩家|x y z>",
        "category": "传送",
        "args": ["<玩家名>", "<目标玩家|x y z>"],
    },
    "teleport": {
        "desc": "传送（同tp）",
        "usage": "teleport <玩家名> <目标玩家|x y z>",
        "category": "传送",
        "args": ["<玩家名>", "<目标玩家|x y z>"],
    },
    "kill": {
        "desc": "击杀实体",
        "usage": "kill [玩家名|目标选择器]",
        "category": "玩家",
        "args": ["[玩家名|目标选择器]"],
        "dangerous": True,
    },
    "effect": {
        "desc": "给予/移除状态效果",
        "usage": "effect <玩家名> <clear|效果名> [持续时间] [强度]",
        "category": "玩家",
        "args": ["<玩家名>", "<clear|效果名>", "[持续时间]", "[强度]"],
    },
    "xp": {
        "desc": "给予/设置经验",
        "usage": "xp <add|set|query> <玩家名> <数量>",
        "category": "玩家",
        "args": ["<add|set|query>", "<玩家名>", "<数量>"],
    },
    # 消息
    "say": {"desc": "发送服务器公告", "usage": "say <消息内容>", "category": "消息", "args": ["<消息内容>"]},
    "tell": {
        "desc": "私聊玩家",
        "usage": "tell <玩家名> <消息内容>",
        "category": "消息",
        "args": ["<玩家名>", "<消息内容>"],
    },
    "me": {"desc": "发送动作消息", "usage": "me <动作描述>", "category": "消息", "args": ["<动作描述>"]},
    "title": {
        "desc": "显示标题",
        "usage": "title <玩家名> <clear|reset|title|subtitle|actionbar> ...",
        "category": "消息",
    },
    # 其他
    "help": {"desc": "显示帮助", "usage": "help [命令名]", "category": "其他", "args": ["[命令名]"]},
    "?": {"desc": "显示帮助（同help）", "usage": "? [命令名]", "category": "其他", "args": ["[命令名]"]},
    "getgameproperty": {
        "desc": "获取游戏属性",
        "usage": "getgameproperty <属性名>",
        "category": "其他",
        "args": ["<属性名>"],
    },
    "setgameproperty": {
        "desc": "设置游戏属性",
        "usage": "setgameproperty <属性名> <值>",
        "category": "其他",
        "args": ["<属性名>", "<值>"],
    },
    "changesetting": {
        "desc": "更改服务器设置",
        "usage": "changesetting <设置名> <值>",
        "category": "其他",
        "args": ["<设置名>", "<值>"],
    },
    "function": {"desc": "执行函数", "usage": "function <函数名>", "category": "其他", "args": ["<函数名>"]},
    "locate": {"desc": "定位结构", "usage": "locate <结构名>", "category": "其他", "args": ["<结构名>"]},
    "playsound": {"desc": "播放声音", "usage": "playsound <声音名> <玩家名> [x y z] [音量] [音调]", "category": "其他"},
    "particle": {"desc": "生成粒子效果", "usage": "particle <粒子名> <x y z>", "category": "其他"},
    "fill": {"desc": "填充区域", "usage": "fill <x1 y1 z1> <x2 y2 z2> <方块名>", "category": "其他"},
    "setblock": {"desc": "设置方块", "usage": "setblock <x y z> <方块名>", "category": "其他"},
    "clone": {"desc": "克隆区域", "usage": "clone <x1 y1 z1> <x2 y2 z2> <x y z>", "category": "其他"},
    "testfor": {"desc": "检测实体", "usage": "testfor <目标选择器>", "category": "其他"},
    "testforblock": {"desc": "检测方块", "usage": "testforblock <x y z> <方块名>", "category": "其他"},
    "testforblocks": {
        "desc": "检测方块区域",
        "usage": "testforblocks <x1 y1 z1> <x2 y2 z2> <x y z>",
        "category": "其他",
    },
    "clear": {"desc": "清空玩家物品栏", "usage": "clear [玩家名] [物品名] [数据值]", "category": "物品"},
    "replaceitem": {"desc": "替换物品", "usage": "replaceitem <block|entity> ... <物品名> [数量]", "category": "物品"},
    "enchant": {"desc": "附魔物品", "usage": "enchant <玩家名> <附魔名> <等级>", "category": "物品"},
    "ride": {"desc": "骑乘实体", "usage": "ride <玩家名> <start|stop|summon_ride> ...", "category": "其他"},
    "structure": {"desc": "结构管理", "usage": "structure <save|load|delete|list> ...", "category": "其他"},
    "mobevent": {"desc": "生物事件控制", "usage": "mobevent <事件名> <true|false>", "category": "其他"},
    "camerashake": {"desc": "相机震动", "usage": "camerashake <add|stop> <玩家名> ...", "category": "其他"},
    "fog": {"desc": "雾气效果", "usage": "fog <玩家名> <push|pop|remove> <雾气ID>", "category": "其他"},
    "inputpermission": {
        "desc": "输入权限",
        "usage": "inputpermission <set|query> <玩家名> <camera|movement> <enabled|disabled>",
        "category": "其他",
    },
    "agent": {"desc": "代理操作（教育版）", "usage": "agent <动作> ...", "category": "其他"},
    "immutableworld": {"desc": "设置世界不可变", "usage": "immutableworld <true|false>", "category": "其他"},
    "alwaysday": {"desc": "锁定为白天", "usage": "alwaysday <true|false>", "category": "游戏"},
    "daylock": {"desc": "锁定时间（同alwaysday）", "usage": "daylock <true|false>", "category": "游戏"},
    "compass": {"desc": "显示指南针方向", "usage": "compass", "category": "其他"},
    "transferserver": {"desc": "转移服务器", "usage": "transferserver <IP> <端口>", "category": "服务器"},
    "script": {"desc": "脚本管理", "usage": "script <debug|profiler|watchdog> ...", "category": "其他"},
}

# 危险命令标记与 constants.DANGEROUS_COMMANDS 保持单一来源：
# 凡在统一清单中的命令，其元数据补齐 dangerous 标记（帮助文本与安全校验一致）
for _dangerous_name in DANGEROUS_COMMANDS:
    if _dangerous_name in COMMANDS and not COMMANDS[_dangerous_name].get("dangerous"):
        COMMANDS[_dangerous_name]["dangerous"] = True


def get_all_commands() -> list[str]:
    """获取所有命令名称列表。

    Returns:
        list: 命令名称列表
    """
    return sorted(COMMANDS.keys())


def get_command_info(command: str) -> dict | None:
    """获取命令详细信息。

    Args:
        command: 命令名称

    Returns:
        dict: 命令信息，不存在返回 None
    """
    return COMMANDS.get(command.lower())


def get_commands_by_category(category: str) -> list[dict]:
    """按分类获取命令列表。

    Args:
        category: 分类名称

    Returns:
        list: 命令信息列表
    """
    return [{"name": name, **info} for name, info in COMMANDS.items() if info.get("category") == category]


def get_categories() -> list[str]:
    """获取所有命令分类。

    Returns:
        list: 分类名称列表
    """
    return sorted({info.get("category", "其他") for info in COMMANDS.values()})


def autocomplete_command(prefix: str, limit: int = 10) -> list[dict]:
    """根据前缀自动补全命令。

    Args:
        prefix: 命令前缀
        limit: 返回结果数量限制

    Returns:
        list: 匹配的命令信息列表
    """
    if not prefix:
        return []
    prefix_lower = prefix.lower()
    matches = []
    for name, info in COMMANDS.items():
        if name.startswith(prefix_lower):
            matches.append({"name": name, **info})
            if len(matches) >= limit:
                break
    return matches


def get_command_suggestions(input_text: str, online_players: list[str] = None) -> dict:
    """根据当前输入获取命令建议。

    Args:
        input_text: 当前输入文本
        online_players: 在线玩家列表（用于玩家名补全）

    Returns:
        dict: 包含 suggestions, command_info, is_complete
    """
    if not input_text or not input_text.strip():
        return {
            "suggestions": [{"name": name, **info} for name, info in list(COMMANDS.items())[:10]],
            "command_info": None,
            "is_complete": False,
        }

    parts = input_text.strip().split()
    if not parts:
        return {"suggestions": [], "command_info": None, "is_complete": False}

    cmd_name = parts[0].lower()
    cmd_info = COMMANDS.get(cmd_name)

    # 如果命令不完整，提供命令补全
    if not cmd_info and len(parts) == 1:
        suggestions = autocomplete_command(cmd_name)
        return {"suggestions": suggestions, "command_info": None, "is_complete": False}

    # 如果命令已完整，提供参数提示
    if cmd_info:
        args = cmd_info.get("args", [])
        current_arg_index = len(parts) - 1  # 减去命令本身

        # 如果是玩家名参数，提供在线玩家补全
        if online_players and current_arg_index < len(args):
            arg_desc = args[current_arg_index]
            if "玩家名" in arg_desc or "目标玩家" in arg_desc:
                player_prefix = parts[current_arg_index + 1] if current_arg_index + 1 < len(parts) else ""
                player_matches = [p for p in online_players if p.lower().startswith(player_prefix.lower())]
                return {
                    "suggestions": [{"name": p, "desc": "在线玩家", "category": "玩家"} for p in player_matches[:10]],
                    "command_info": cmd_info,
                    "is_complete": True,
                    "current_arg": current_arg_index,
                    "args": args,
                }

        return {
            "suggestions": [],
            "command_info": cmd_info,
            "is_complete": True,
            "current_arg": current_arg_index,
            "args": args,
        }

    return {"suggestions": [], "command_info": None, "is_complete": False}


def is_dangerous_command(command: str) -> bool:
    """检查命令是否为危险命令。

    优先依据 constants.DANGEROUS_COMMANDS 单一清单判断，
    兼容命令库元数据（dangerous 标记已与清单同步）。

    Args:
        command: 命令名称

    Returns:
        bool: 是否为危险命令
    """
    cmd_name = command.lower().split()[0] if command else ""
    if cmd_name in DANGEROUS_COMMANDS:
        return True
    cmd_info = COMMANDS.get(cmd_name)
    return bool(cmd_info and cmd_info.get("dangerous", False))


def get_command_help(command: str = "") -> str:
    """获取命令帮助文本。

    Args:
        command: 命令名称（为空时返回所有命令概览）

    Returns:
        str: 帮助文本
    """
    if not command:
        lines = ["可用命令列表（按分类）："]
        for category in get_categories():
            cmds = get_commands_by_category(category)
            lines.append(f"\n【{category}】")
            for cmd in cmds:
                lines.append("  {} - {}".format(cmd["name"], cmd["desc"]))
        return "\n".join(lines)

    cmd_info = get_command_info(command)
    if not cmd_info:
        return f"未知命令: {command}"

    lines = [
        f"命令: {command}",
        "描述: {}".format(cmd_info["desc"]),
        "用法: {}".format(cmd_info["usage"]),
        "分类: {}".format(cmd_info.get("category", "其他")),
    ]
    if cmd_info.get("dangerous"):
        lines.append("注意: 这是危险命令，执行前请确认")
    return "\n".join(lines)


__all__ = [
    "COMMANDS",
    "get_all_commands",
    "get_command_info",
    "get_commands_by_category",
    "get_categories",
    "autocomplete_command",
    "get_command_suggestions",
    "is_dangerous_command",
    "get_command_help",
]
