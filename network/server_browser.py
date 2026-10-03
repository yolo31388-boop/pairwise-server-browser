"""
服务器浏览器与筛选系统 - 核心模块

修复内容：
- refresh_list(): 失败重试、超时控制、分页拉取
- filter_servers(): 多条件组合筛选 + 多字段排序
- ping 检测: 多发包取平均值、丢包剔除、定时更新
- 收藏: 持久化到磁盘、分组、备注
- 服务器信息: 完整字段、定时刷新、详细页面
"""
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional
import json
import os
import time


DEFAULTS = {
    "retry_count": 3,          # 刷新失败最大重试次数
    "timeout": 5.0,            # 单次请求超时（秒）
    "page_size": 50,           # 分页大小
    "ping_samples": 4,         # ping 检测发包数
    "ping_interval": 10.0,     # ping 定时刷新间隔（秒）
    "info_refresh_interval": 5.0,  # 服务器信息定时刷新间隔（秒）
    "favorites_file": None,    # 收藏持久化文件路径
}

# 筛选支持的字段
FILTER_KEYS = ("has_slots", "map_name", "region", "game_mode",
               "max_ping", "min_players", "not_full")

# 排序支持的字段
SORT_KEYS = ("ping", "players", "map_name", "name", "region")


@dataclass
class Config:
    retry_count: int = 3
    timeout: float = 5.0
    page_size: int = 50
    ping_samples: int = 4
    ping_interval: float = 10.0
    info_refresh_interval: float = 5.0
    favorites_file: Optional[str] = None


class ServerBrowser:
    def __init__(self, config: Optional[Dict] = None):
        cfg = dict(DEFAULTS)
        cfg.update(config or {})
        self.config = cfg

        # 可注入依赖（便于测试与接入真实网络层）
        self._fetcher: Optional[Callable] = cfg.get("fetcher")
        self._pinger: Optional[Callable] = cfg.get("pinger")
        self._clock: Callable[[], float] = cfg.get("clock", time.monotonic)

        self._servers: List[Dict] = []
        self._pings: Dict[str, float] = {}
        self._info_cache: Dict[str, Dict] = {}
        self._favorites: Dict[str, Dict] = {}
        self._last_error: Optional[str] = None

        self._ping_timer = 0.0
        self._info_timer = 0.0

        self._state = {}
        self._history = []

        self._load_favorites()

    # ------------------------------------------------------------------
    # 依赖注入
    # ------------------------------------------------------------------
    def set_fetcher(self, fetcher: Callable):
        """设置服务器列表拉取函数: fetcher(page, page_size, timeout) -> dict"""
        self._fetcher = fetcher

    def set_pinger(self, pinger: Callable):
        """设置 ping 函数: pinger(server) -> float | None（None 表示丢包）"""
        self._pinger = pinger

    # ------------------------------------------------------------------
    # 列表刷新：重试 + 超时 + 分页
    # ------------------------------------------------------------------
    def refresh_list(self) -> List[Dict]:
        """分页拉取全部服务器。每页失败按 retry_count 重试，单次请求受 timeout 约束。"""
        if self._fetcher is None:
            self._last_error = "no_fetcher"
            return self._servers

        servers: List[Dict] = []
        page = 0
        page_size = int(self.config["page_size"])
        retries = int(self.config["retry_count"])
        timeout = float(self.config["timeout"])

        while True:
            result = self._fetch_page_with_retry(page, page_size, timeout, retries)
            if result is None:
                self._last_error = "fetch_failed"
                break
            batch = result.get("servers", [])
            servers.extend(batch)
            total = result.get("total")
            has_more = result.get("has_more")
            if has_more is None:
                has_more = (total is None and len(batch) == page_size) or \
                           (total is not None and len(servers) < total)
            if not has_more or not batch:
                break
            page += 1

        if self._last_error != "fetch_failed":
            self._last_error = None
        self._servers = servers
        self._history.append({"action": "refresh", "count": len(servers)})
        return list(self._servers)

    def _fetch_page_with_retry(self, page, page_size, timeout, retries):
        attempts = max(1, retries)
        for _ in range(attempts):
            start = self._clock()
            try:
                result = self._fetcher(page, page_size, timeout)
            except Exception:
                result = None
            elapsed = self._clock() - start
            if elapsed > timeout:
                continue  # 超时视为失败，重试
            if result is None:
                continue
            return result
        return None

    @property
    def last_error(self) -> Optional[str]:
        return self._last_error

    @property
    def servers(self) -> List[Dict]:
        return list(self._servers)

    # ------------------------------------------------------------------
    # 筛选与排序
    # ------------------------------------------------------------------
    def filter_servers(self, filters: Optional[Dict] = None,
                       sort_by: Optional[str] = None,
                       sort_order: str = "asc") -> List[Dict]:
        """多条件组合筛选 + 排序。

        filters 支持: has_slots, map_name, region, game_mode, max_ping, min_players
        sort_by 支持: ping, players, map_name, name, region
        """
        filters = filters or {}
        result = []
        for srv in self._servers:
            if not self._match(srv, filters):
                continue
            result.append(srv)

        if sort_by:
            if sort_by not in SORT_KEYS:
                raise ValueError("unsupported sort key: %s" % sort_by)
            reverse = sort_order == "desc"
            result.sort(key=lambda s: self._sort_value(s, sort_by),
                        reverse=reverse)
        return result

    def _match(self, srv: Dict, filters: Dict) -> bool:
        for key, want in filters.items():
            if want is None:
                continue
            if key in ("has_slots", "not_full"):
                if want and srv.get("players", 0) >= srv.get("max_players", 0):
                    return False
            elif key == "max_ping":
                ping = self._pings.get(srv.get("id"), srv.get("ping"))
                if ping is None or ping > want:
                    return False
            elif key == "min_players":
                if srv.get("players", 0) < want:
                    return False
            elif key in ("map_name", "region", "game_mode", "name"):
                if srv.get(key) != want:
                    return False
            else:
                raise ValueError("unsupported filter key: %s" % key)
        return True

    def _sort_value(self, srv: Dict, key):
        if key == "ping":
            value = self._pings.get(srv.get("id"), srv.get("ping"))
            return float("inf") if value is None else value
        return srv.get(key) if srv.get(key) is not None else ""

    # ------------------------------------------------------------------
    # ping 检测：多发包取平均 + 定时更新
    # ------------------------------------------------------------------
    def ping_server(self, server) -> Optional[float]:
        """对单个服务器发 ping_samples 个包，剔除丢包后取平均值。"""
        if isinstance(server, dict):
            srv = server
        else:
            srv = self._find_server(server)
            if srv is None:
                return None
        samples = max(1, int(self.config["ping_samples"]))
        results = []
        for _ in range(samples):
            if self._pinger is None:
                break
            try:
                value = self._pinger(srv)
            except Exception:
                value = None
            if value is not None:
                results.append(value)
        if not results:
            self._pings[srv.get("id")] = None
            return None
        avg = sum(results) / len(results)
        self._pings[srv.get("id")] = avg
        srv["ping"] = avg
        return avg

    def ping_all(self) -> Dict[str, Optional[float]]:
        for srv in self._servers:
            self.ping_server(srv)
        return dict(self._pings)

    def get_ping(self, server_id: str) -> Optional[float]:
        return self._pings.get(server_id)

    def _find_server(self, server_id: str) -> Optional[Dict]:
        for srv in self._servers:
            if srv.get("id") == server_id:
                return srv
        return None

    # ------------------------------------------------------------------
    # 收藏：持久化 + 分组 + 备注
    # ------------------------------------------------------------------
    def add_favorite(self, server_id: str, group: str = "默认",
                     note: str = "") -> Dict:
        fav = {"server_id": server_id, "group": group, "note": note}
        self._favorites[server_id] = fav
        self._save_favorites()
        return dict(fav)

    def remove_favorite(self, server_id: str) -> bool:
        if server_id in self._favorites:
            del self._favorites[server_id]
            self._save_favorites()
            return True
        return False

    def update_favorite(self, server_id: str, group: Optional[str] = None,
                        note: Optional[str] = None) -> Optional[Dict]:
        fav = self._favorites.get(server_id)
        if fav is None:
            return None
        if group is not None:
            fav["group"] = group
        if note is not None:
            fav["note"] = note
        self._save_favorites()
        return dict(fav)

    def get_favorites(self, group: Optional[str] = None) -> List[Dict]:
        favs = list(self._favorites.values())
        if group is not None:
            favs = [f for f in favs if f.get("group") == group]
        return [dict(f) for f in favs]

    def get_favorite_groups(self) -> List[str]:
        return sorted({f.get("group", "默认") for f in self._favorites.values()})

    def is_favorite(self, server_id: str) -> bool:
        return server_id in self._favorites

    def _favorites_path(self) -> Optional[str]:
        return self.config.get("favorites_file")

    def _save_favorites(self):
        path = self._favorites_path()
        if not path:
            return
        directory = os.path.dirname(os.path.abspath(path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(list(self._favorites.values()), fh, ensure_ascii=False)

    def _load_favorites(self):
        path = self._favorites_path()
        if not path or not os.path.exists(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (json.JSONDecodeError, OSError):
            return
        for fav in data:
            self._favorites[fav["server_id"]] = fav

    # ------------------------------------------------------------------
    # 服务器信息：完整字段 + 定时刷新 + 详细页面
    # ------------------------------------------------------------------
    def server_info(self, server_id: str) -> Optional[Dict]:
        """返回完整服务器信息：名称、地图、玩家数、ping、游戏模式、地区。"""
        srv = self._find_server(server_id)
        if srv is None:
            return self._info_cache.get(server_id)
        info = {
            "id": srv.get("id"),
            "name": srv.get("name"),
            "map_name": srv.get("map_name"),
            "players": srv.get("players"),
            "max_players": srv.get("max_players"),
            "ping": self._pings.get(server_id, srv.get("ping")),
            "game_mode": srv.get("game_mode"),
            "region": srv.get("region"),
        }
        self._info_cache[server_id] = info
        return dict(info)

    def get_server_detail(self, server_id: str) -> Optional[Dict]:
        """详细页面：完整信息 + 玩家列表、描述、版本等扩展字段。"""
        srv = self._find_server(server_id)
        if srv is None:
            return None
        detail = self.server_info(server_id)
        detail.update({
            "player_list": list(srv.get("player_list", [])),
            "description": srv.get("description", ""),
            "version": srv.get("version", ""),
            "password_protected": bool(srv.get("password_protected", False)),
            "favorite": self.is_favorite(server_id),
        })
        return detail

    # ------------------------------------------------------------------
    # 定时更新
    # ------------------------------------------------------------------
    def update(self, dt: float):
        """推进内部计时器，到期后自动重测 ping、刷新服务器信息。"""
        self._ping_timer += dt
        self._info_timer += dt

        if self._ping_timer >= float(self.config["ping_interval"]):
            self._ping_timer = 0.0
            self.ping_all()

        if self._info_timer >= float(self.config["info_refresh_interval"]):
            self._info_timer = 0.0
            self.refresh_list()

    def reset(self):
        self._servers = []
        self._pings = {}
        self._info_cache = {}
        self._ping_timer = 0.0
        self._info_timer = 0.0
        self._last_error = None
        self._state = {}
        self._history = []
