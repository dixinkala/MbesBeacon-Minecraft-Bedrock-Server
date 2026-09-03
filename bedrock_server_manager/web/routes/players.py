"""
players 相关路由处理方法
"""

from ..route_decorator import register_route


class PlayersRoutesMixin:
    """players 相关路由处理方法 mixin。"""

    @register_route("/api/ipban/list")
    def _route_ipban_list(self, data):
        from ...players import load_banned_ips

        banned = load_banned_ips()
        self._json({"ok": True, "banned": banned, "count": len(banned)})

    @register_route("/api/ipban/add")
    def _route_ipban_add(self, data):
        from ...players import ban_ip, load_banned_ips

        ip = (data.get("ip") or "").strip()
        reason = (data.get("reason") or "").strip()
        if not ip:
            self._json({"ok": False, "error": "请输入IP地址"})
            return
        ok, err = ban_ip(ip, reason)
        if ok:
            self._json({"ok": True, "banned": load_banned_ips()})
        else:
            self._json({"ok": False, "error": err})

    @register_route("/api/ipban/remove")
    def _route_ipban_remove(self, data):
        from ...players import load_banned_ips, pardon_ip

        ip = (data.get("ip") or "").strip()
        if not ip:
            self._json({"ok": False, "error": "请输入IP地址"})
            return
        ok, err = pardon_ip(ip)
        if ok:
            self._json({"ok": True, "banned": load_banned_ips()})
        else:
            self._json({"ok": False, "error": err})

    @register_route("/api/players/all")
    def _route_players_all(self, data):
        from ...players import get_all_player_status

        players = get_all_player_status()
        self._json({"ok": True, "players": players, "count": len(players)})
