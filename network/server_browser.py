"""
服务器浏览器与筛选系统 - 核心模块（修复版）

修复内容：
- refresh_list(): 失败重试、超时控制、分页拉取
- filter_servers(): 多条件组合筛选、多字段排序
- ping 检测: 多发包取平均值、统计丢包、定时更新
- 收藏服务器: 持久化到磁盘、分组、备注
- server_info(): 完整字段、定时刷新、详细页面
"""
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional


class RefreshError(Exception):
    """服务器列表刷新失败（重试耗尽或超时）。"""


@dataclass
class Config:
    page_size: int = 50
    max_retries: int = 3
    retry_delay: float = 0.05
    timeout: float = 5.0
    ping_attempts: int = 4
    ping_interval: float = 5.0
    info_refresh_interval: float = 10.0
    favorites_file: Optional[str] = None


DEFAULT_GROUP = "默认"


class ServerBrowser:
    DEFAULTS = {
        "page_size": Config.page_size,
        "max_retries": Config.max_retries,
        "retry_delay": Config.retry_delay,
        "timeout": Config.timeout,
        "ping_attempts": Config.ping_attempts,
        "ping_interval": Config.ping_interval,
        "info_refresh_interval": Config.info_refresh_interval,
        "favorites_file": Config.favorites_file,
    }

    SORT_KEYS = {
        "ping": lambda s: s.get("ping") if s.get("ping") is not None else float("inf"),
        "players": lambda s: s.get("players", 0),
        "max_players": lambda s: s.get("max_players", 0),
        "map": lambda s: str(s.get("map", "")),
        "name": lambda s: str(s.get("name", "")),
        "region": lambda s: str(s.get("region", "")),
        "game_mode": lambda s: str(s.get("game_mode", "")),
    }

    def __init__(self, config: Optional[Dict] = None):
        self.config = dict(self.DEFAULTS)
        if config:
            self.config.update(config)
        # 可注入的外部依赖（便于测试与替换真实网络层）
        self._fetch_page = self.config.get("fetch_page")      # (page, page_size) -> List[dict]
        self._fetch_info = self.config.get("fetch_info")      # (server_id) -> dict
        self._pinger = self.config.get("pinger")              # (address) -> Optional[float]

        self._state = {}
        self._history = []
        self._time = 0.0
        self._servers: List[Dict] = []
        self._server_index: Dict = {}
        self._ping_cache: Dict[str, Dict] = {}
        self._info_updated: Dict = {}
        self._favorites: Dict = {}
        self._executor: Optional[ThreadPoolExecutor] = None
        self.last_refresh_stats: Dict = {}
        self._load_favorites()

    # ------------------------------------------------------------------
    # 列表刷新：重试 + 超时 + 分页
    # ------------------------------------------------------------------
    def refresh_list(self) -> List[Dict]:
        """分页拉取全部服务器；每页失败会重试，单次请求有超时限制。"""
        if self._fetch_page is None:
            raise RefreshError("未配置服务器列表数据源 fetch_page")
        page_size = int(self.config["page_size"])
        servers: List[Dict] = []
        total_retries = 0
        page = 0
        while True:
            batch, used_retries = self._fetch_page_with_retry(page, page_size)
            total_retries += used_retries
            servers.extend(batch)
            page += 1
            if len(batch) < page_size:
                break
        self._servers = servers
        self._server_index = {s.get("id"): s for s in servers if s.get("id") is not None}
        now = self._now()
        for server in servers:
            self._info_updated[server.get("id")] = now
            if self._pinger is not None and server.get("address"):
                self.ping_server(server["address"])
        self.last_refresh_stats = {
            "pages": page,
            "retries": total_retries,
            "count": len(servers),
        }
        return list(servers)

    def _fetch_page_with_retry(self, page: int, page_size: int):
        attempts = int(self.config["max_retries"]) + 1
        last_error: Optional[Exception] = None
        for attempt in range(attempts):
            try:
                result = self._call_with_timeout(self._fetch_page, page, page_size)
                return list(result or []), attempt
            except Exception as exc:  # 包括超时与数据源异常
                last_error = exc
                if attempt < attempts - 1 and self.config["retry_delay"] > 0:
                    time.sleep(float(self.config["retry_delay"]))
        raise RefreshError("获取第 %d 页服务器列表失败（已重试 %d 次）" % (page, attempts - 1)) from last_error

    def _call_with_timeout(self, func: Callable, *args):
        future = self._get_executor().submit(func, *args)
        return future.result(timeout=float(self.config["timeout"]))

    def _get_executor(self) -> ThreadPoolExecutor:
        if self._executor is None:
            self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="server-browser")
        return self._executor

    # ------------------------------------------------------------------
    # 筛选：多条件组合 + 排序
    # ------------------------------------------------------------------
    def filter_servers(self, servers: Optional[List[Dict]] = None,
                       filters: Optional[Dict] = None,
                       sort_by: Optional[str] = None,
                       reverse: bool = False) -> List[Dict]:
        """按多个条件组合筛选（条件之间为 AND），并可选排序。"""
        source = self._servers if servers is None else servers
        filters = filters or {}
        result = [s for s in source if self._matches(s, filters)]
        if sort_by is not None:
            if sort_by not in self.SORT_KEYS:
                raise ValueError("不支持的排序字段: %r" % (sort_by,))
            result.sort(key=self.SORT_KEYS[sort_by], reverse=reverse)
        return result

    @staticmethod
    def _matches(server: Dict, filters: Dict) -> bool:
        players = server.get("players", 0)
        max_players = server.get("max_players", 0)
        if filters.get("has_slots") and players >= max_players:
            return False
        if "min_players" in filters and players < filters["min_players"]:
            return False
        if "max_players" in filters and players > filters["max_players"]:
            return False
        if filters.get("map") and server.get("map") != filters["map"]:
            return False
        if filters.get("game_mode") and server.get("game_mode") != filters["game_mode"]:
            return False
        if filters.get("region") and server.get("region") != filters["region"]:
            return False
        if "max_ping" in filters:
            ping = server.get("ping")
            if ping is None or ping > filters["max_ping"]:
                return False
        if filters.get("name_contains"):
            if str(filters["name_contains"]).lower() not in str(server.get("name", "")).lower():
                return False
        if filters.get("no_password") and server.get("password"):
            return False
        return True

    # ------------------------------------------------------------------
    # ping 检测：多发包取平均 + 丢包统计 + 定时更新
    # ------------------------------------------------------------------
    def ping_server(self, address: str) -> Optional[float]:
        """向服务器发送多个探测包，返回平均延迟（ms）；全部丢包返回 None。"""
        if self._pinger is None:
            return None
        attempts = max(1, int(self.config["ping_attempts"]))
        samples: List[float] = []
        lost = 0
        for _ in range(attempts):
            try:
                result = self._pinger(address)
            except Exception:
                result = None
            if result is None:
                lost += 1
            else:
                samples.append(float(result))
        average = sum(samples) / len(samples) if samples else None
        self._ping_cache[address] = {
            "ping": average,
            "loss": lost / attempts,
            "samples": len(samples),
            "updated_at": self._now(),
        }
        for server in self._servers:
            if server.get("address") == address:
                server["ping"] = average
        return average

    def ping_stats(self, address: str) -> Optional[Dict]:
        stats = self._ping_cache.get(address)
        return dict(stats) if stats else None

    # ------------------------------------------------------------------
    # 定时更新：ping 与服务器信息
    # ------------------------------------------------------------------
    def update(self, dt: float):
        self._time += float(dt)
        now = self._time
        if self._pinger is not None:
            interval = float(self.config["ping_interval"])
            for server in self._servers:
                address = server.get("address")
                if not address:
                    continue
                last = self._ping_cache.get(address, {}).get("updated_at")
                if last is None or now - last >= interval:
                    self.ping_server(address)
        if self._fetch_info is not None:
            interval = float(self.config["info_refresh_interval"])
            for server in self._servers:
                server_id = server.get("id")
                last = self._info_updated.get(server_id)
                if last is None or now - last >= interval:
                    self.refresh_server_info(server_id)

    # ------------------------------------------------------------------
    # 服务器信息：完整字段 + 定时刷新 + 详细页面
    # ------------------------------------------------------------------
    def refresh_server_info(self, server_id) -> Optional[Dict]:
        """从数据源重新拉取单个服务器的信息并合并到缓存。"""
        if self._fetch_info is None:
            return None
        info = self._fetch_info(server_id)
        if info is None:
            return None
        info = dict(info)
        info.setdefault("id", server_id)
        existing = self._server_index.get(server_id)
        if existing is not None:
            existing.update(info)
            info = existing
        else:
            self._servers.append(info)
            self._server_index[server_id] = info
        self._info_updated[server_id] = self._now()
        return info

    def server_info(self, server_id) -> Optional[Dict]:
        """服务器摘要信息：名称、地图、玩家数、ping、游戏模式、地区。"""
        server = self._server_index.get(server_id)
        if server is None:
            return None
        return {
            "id": server.get("id"),
            "name": server.get("name"),
            "map": server.get("map"),
            "players": server.get("players"),
            "max_players": server.get("max_players"),
            "ping": server.get("ping"),
            "game_mode": server.get("game_mode"),
            "region": server.get("region"),
        }

    def server_detail(self, server_id) -> Optional[Dict]:
        """服务器详细页面：完整字段 + ping 统计 + 收藏信息。"""
        server = self._server_index.get(server_id)
        if server is None:
            return None
        detail = dict(server)
        detail.setdefault("description", "")
        detail.setdefault("version", "")
        detail.setdefault("tags", [])
        detail.setdefault("player_list", [])
        stats = self._ping_cache.get(server.get("address"), {})
        detail["ping_stats"] = {
            "ping": stats.get("ping"),
            "loss": stats.get("loss"),
            "samples": stats.get("samples"),
        }
        favorite = self._favorites.get(server_id)
        detail["favorite"] = dict(favorite) if favorite else None
        return detail

    # ------------------------------------------------------------------
    # 收藏：持久化 + 分组 + 备注
    # ------------------------------------------------------------------
    def add_favorite(self, server_id, group: str = DEFAULT_GROUP, note: str = "") -> Dict:
        entry = {
            "server_id": server_id,
            "group": group or DEFAULT_GROUP,
            "note": note or "",
        }
        self._favorites[server_id] = entry
        self._save_favorites()
        return dict(entry)

    def remove_favorite(self, server_id) -> bool:
        if server_id in self._favorites:
            del self._favorites[server_id]
            self._save_favorites()
            return True
        return False

    def is_favorite(self, server_id) -> bool:
        return server_id in self._favorites

    def set_favorite_group(self, server_id, group: str) -> bool:
        entry = self._favorites.get(server_id)
        if entry is None:
            return False
        entry["group"] = group or DEFAULT_GROUP
        self._save_favorites()
        return True

    def set_favorite_note(self, server_id, note: str) -> bool:
        entry = self._favorites.get(server_id)
        if entry is None:
            return False
        entry["note"] = note or ""
        self._save_favorites()
        return True

    def get_favorites(self, group: Optional[str] = None) -> List[Dict]:
        entries = list(self._favorites.values())
        if group is not None:
            entries = [e for e in entries if e["group"] == group]
        return [dict(e) for e in entries]

    def favorite_servers(self, group: Optional[str] = None) -> List[Dict]:
        return self.get_favorites(group=group)

    def favorite_groups(self) -> List[str]:
        return sorted({e["group"] for e in self._favorites.values()})

    def _favorites_path(self) -> Optional[str]:
        path = self.config.get("favorites_file")
        return str(path) if path else None

    def _load_favorites(self):
        path = self._favorites_path()
        if not path or not os.path.exists(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return
        for entry in data.get("favorites", []):
            server_id = entry.get("server_id")
            if server_id is not None:
                self._favorites[server_id] = {
                    "server_id": server_id,
                    "group": entry.get("group") or DEFAULT_GROUP,
                    "note": entry.get("note") or "",
                }

    def _save_favorites(self):
        path = self._favorites_path()
        if not path:
            return
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        payload = {"favorites": list(self._favorites.values())}
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def reset(self):
        self._state = {}
        self._history = []
        self._time = 0.0
        self._servers = []
        self._server_index = {}
        self._ping_cache = {}
        self._info_updated = {}
        self.last_refresh_stats = {}

    def close(self):
        if self._executor is not None:
            self._executor.shutdown(wait=False)
            self._executor = None

    def _now(self) -> float:
        return self._time
