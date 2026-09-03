"""
commands 相关路由处理方法
"""

from ..route_decorator import register_route


class CommandsRoutesMixin:
    """commands 相关路由处理方法 mixin。"""

    from ...commands import get_all_commands, get_categories, get_command_info, get_commands_by_category, autocomplete_command, get_command_suggestions, get_command_help

    @register_route("/api/commands/list")
    def _route_commands_list(self, data):
        category = data.get("category", "")
        if category:
            cmds = get_commands_by_category(category)
        else:
            cmds = [{"name": name, **get_command_info(name)} for name in get_all_commands()]
        self._json({"ok": True, "commands": cmds, "categories": get_categories()})

    @register_route("/api/commands/autocomplete")
    def _route_commands_autocomplete(self, data):
        prefix = (data.get("prefix") or data.get("input") or "").strip()
        online_players = data.get("players", [])
        if data.get("input"):
            result = get_command_suggestions(prefix, online_players)
            self._json({"ok": True, **result})
        else:
            suggestions = autocomplete_command(prefix)
            self._json({"ok": True, "suggestions": suggestions})

    @register_route("/api/commands/help")
    def _route_commands_help(self, data):
        command = (data.get("command") or "").strip()
        help_text = get_command_help(command)
        self._json({"ok": True, "help": help_text})
