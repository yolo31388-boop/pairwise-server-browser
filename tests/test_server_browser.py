"""
服务器浏览器与筛选系统 - 验收测试
运行方式：python -m pytest tests/test_server_browser.py -q
共 14 个测试用例
"""
import time

import pytest
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from network.server_browser import RefreshError, ServerBrowser


def make_server(server_id=1, name="测试服务器", map="de_dust2", players=5,
                max_players=10, ping=30.0, game_mode="竞技", region="亚洲",
                address="1.2.3.4:27015", **extra):
    server = {
        "id": server_id,
        "name": name,
        "map": map,
        "players": players,
        "max_players": max_players,
        "ping": ping,
        "game_mode": game_mode,
        "region": region,
        "address": address,
    }
    server.update(extra)
    return server


def make_fetcher(pages):
    """构造分页数据源，pages 为每页返回的列表。"""
    calls = []

    def fetch(page, page_size):
        calls.append(page)
        return list(pages[page]) if page < len(pages) else []

    fetch.calls = calls
    return fetch


class TestServerBrowser:
    def setup_method(self):
        self.system = ServerBrowser()

    # ---- 列表刷新：重试 / 超时 / 分页 ----

    def test_case_01(self):
        """刷新失败后会自动重试，重试成功后返回完整列表。"""
        attempts = []

        def flaky_fetch(page, page_size):
            attempts.append(page)
            if len(attempts) <= 2:
                raise ConnectionError("服务器无响应")
            return [make_server(server_id=1)]

        browser = ServerBrowser({
            "fetch_page": flaky_fetch,
            "max_retries": 3,
            "retry_delay": 0,
        })
        servers = browser.refresh_list()
        assert len(servers) == 1
        assert len(attempts) == 3  # 1 次原始请求 + 2 次重试
        assert browser.last_refresh_stats["retries"] == 2

    def test_case_02(self):
        """单次请求有超时限制，超时后重试，重试耗尽抛出 RefreshError。"""
        def hanging_fetch(page, page_size):
            time.sleep(0.5)
            return []

        browser = ServerBrowser({
            "fetch_page": hanging_fetch,
            "timeout": 0.05,
            "max_retries": 1,
            "retry_delay": 0,
        })
        start = time.monotonic()
        with pytest.raises(RefreshError):
            browser.refresh_list()
        elapsed = time.monotonic() - start
        # 2 次尝试各 0.05s 超时即放弃，不会一直等挂起的服务器
        assert elapsed < 0.4

    def test_case_03(self):
        """列表按页拉取，1000 个服务器不会一次返回。"""
        all_servers = [make_server(server_id=i, address="10.0.0.%d:27015" % i)
                       for i in range(120)]
        pages = [all_servers[i:i + 50] for i in range(0, 120, 50)]
        fetcher = make_fetcher(pages)
        browser = ServerBrowser({"fetch_page": fetcher, "page_size": 50})
        servers = browser.refresh_list()
        assert len(servers) == 120
        assert fetcher.calls == [0, 1, 2]  # 分 3 页拉取
        assert browser.last_refresh_stats["pages"] == 3

    # ---- 筛选：多条件组合 + 排序 ----

    def test_case_04(self):
        """筛选"有空位"时满人服务器被排除。"""
        servers = [
            make_server(server_id=1, players=10, max_players=10),  # 满人
            make_server(server_id=2, players=5, max_players=10),
        ]
        result = self.system.filter_servers(servers, {"has_slots": True})
        assert [s["id"] for s in result] == [2]

    def test_case_05(self):
        """多个筛选条件可组合（AND 关系）。"""
        servers = [
            make_server(server_id=1, players=5, max_players=10, region="亚洲", ping=50),
            make_server(server_id=2, players=5, max_players=10, region="欧洲", ping=40),
            make_server(server_id=3, players=5, max_players=10, region="亚洲", ping=300),
            make_server(server_id=4, players=10, max_players=10, region="亚洲", ping=50),
        ]
        result = self.system.filter_servers(
            servers,
            {"has_slots": True, "region": "亚洲", "max_ping": 100},
        )
        assert [s["id"] for s in result] == [1]

    def test_case_06(self):
        """筛选结果支持按 ping、玩家数、地图名排序。"""
        servers = [
            make_server(server_id=1, ping=80, players=3, map="cs_office"),
            make_server(server_id=2, ping=20, players=9, map="de_dust2"),
            make_server(server_id=3, ping=50, players=6, map="awp_lego"),
        ]
        by_ping = self.system.filter_servers(servers, sort_by="ping")
        assert [s["id"] for s in by_ping] == [2, 3, 1]
        by_players = self.system.filter_servers(servers, sort_by="players", reverse=True)
        assert [s["id"] for s in by_players] == [2, 3, 1]
        by_map = self.system.filter_servers(servers, sort_by="map")
        assert [s["id"] for s in by_map] == [3, 1, 2]

    # ---- ping 检测：多发取平均 / 丢包 / 定时更新 ----

    def test_case_07(self):
        """ping 检测发送多个探测包并取平均值。"""
        samples = iter([10.0, 20.0, 30.0, 40.0])
        calls = []

        def pinger(address):
            calls.append(address)
            return next(samples)

        browser = ServerBrowser({"pinger": pinger, "ping_attempts": 4})
        average = browser.ping_server("1.2.3.4:27015")
        assert average == pytest.approx(25.0)
        assert len(calls) == 4  # 不止发一个包

    def test_case_08(self):
        """丢包时用成功包的平均值，并统计丢包率，不会显示 0ms。"""
        samples = iter([50.0, None, 70.0, None])

        browser = ServerBrowser({
            "pinger": lambda address: next(samples),
            "ping_attempts": 4,
        })
        average = browser.ping_server("1.2.3.4:27015")
        assert average == pytest.approx(60.0)
        stats = browser.ping_stats("1.2.3.4:27015")
        assert stats["loss"] == pytest.approx(0.5)
        assert stats["samples"] == 2

    def test_case_09(self):
        """进入列表后 ping 会按间隔定时更新。"""
        results = iter([10.0, 99.0])
        calls = []

        def pinger(address):
            calls.append(address)
            return next(results)

        server = make_server(server_id=1, address="1.2.3.4:27015")
        browser = ServerBrowser({
            "fetch_page": make_fetcher([[server]]),
            "pinger": pinger,
            "ping_attempts": 1,
            "ping_interval": 5.0,
        })
        browser.refresh_list()
        assert len(calls) == 1
        assert browser.server_info(1)["ping"] == 10.0
        browser.update(4.9)  # 未到更新间隔
        assert len(calls) == 1
        browser.update(0.2)  # 到达更新间隔，ping 刷新
        assert len(calls) == 2
        assert browser.server_info(1)["ping"] == 99.0

    # ---- 收藏：持久化 / 分组 / 备注 ----

    def test_case_10(self, tmp_path):
        """收藏持久化到磁盘，重启后收藏列表不丢失。"""
        favorites_file = str(tmp_path / "favorites.json")
        config = {"favorites_file": favorites_file}
        browser = ServerBrowser(config)
        browser.add_favorite(1, group="常玩", note="低延迟")
        browser.add_favorite(2)
        # 模拟重启：用同一收藏文件创建新实例
        restarted = ServerBrowser(config)
        favorites = restarted.get_favorites()
        assert len(favorites) == 2
        assert restarted.is_favorite(1)
        entry = {f["server_id"]: f for f in favorites}[1]
        assert entry["group"] == "常玩"
        assert entry["note"] == "低延迟"

    def test_case_11(self):
        """收藏支持分组与备注。"""
        self.system.add_favorite(1, group="竞技", note="队友常来")
        self.system.add_favorite(2, group="休闲")
        self.system.add_favorite(3)
        assert self.system.favorite_groups() == ["休闲", "竞技", "默认"]
        competitive = self.system.get_favorites(group="竞技")
        assert [f["server_id"] for f in competitive] == [1]
        assert competitive[0]["note"] == "队友常来"
        # 修改分组与备注
        assert self.system.set_favorite_group(2, "竞技")
        assert self.system.set_favorite_note(2, "周末开黑")
        entry = {f["server_id"]: f for f in self.system.get_favorites(group="竞技")}[2]
        assert entry["note"] == "周末开黑"
        assert self.system.remove_favorite(3)
        assert not self.system.is_favorite(3)

    # ---- 服务器信息：完整字段 / 定时刷新 / 详细页面 ----

    def test_case_12(self):
        """服务器信息包含名称、地图、玩家数、ping、游戏模式、地区。"""
        server = make_server(server_id=1, name="练枪服", map="aim_redline",
                             players=7, max_players=16, ping=45.0,
                             game_mode="死亡竞赛", region="欧洲")
        browser = ServerBrowser({"fetch_page": make_fetcher([[server]])})
        browser.refresh_list()
        info = browser.server_info(1)
        assert info == {
            "id": 1,
            "name": "练枪服",
            "map": "aim_redline",
            "players": 7,
            "max_players": 16,
            "ping": 45.0,
            "game_mode": "死亡竞赛",
            "region": "欧洲",
        }

    def test_case_13(self):
        """服务器信息定时刷新，玩家数变化会更新。"""
        server = make_server(server_id=1, players=5)
        player_counts = iter([9, 12])

        def fetch_info(server_id):
            return {"id": server_id, "players": next(player_counts)}

        browser = ServerBrowser({
            "fetch_page": make_fetcher([[server]]),
            "fetch_info": fetch_info,
            "info_refresh_interval": 10.0,
        })
        browser.refresh_list()
        assert browser.server_info(1)["players"] == 5
        browser.update(9.9)  # 未到刷新间隔
        assert browser.server_info(1)["players"] == 5
        browser.update(0.2)  # 到达刷新间隔
        assert browser.server_info(1)["players"] == 9
        browser.update(10.0)
        assert browser.server_info(1)["players"] == 12

    def test_case_14(self):
        """服务器详细页面包含完整信息与扩展字段。"""
        server = make_server(
            server_id=1,
            description="24 小时练枪",
            version="1.38",
            tags=["练枪", "无密码"],
            player_list=["玩家A", "玩家B"],
        )
        browser = ServerBrowser({
            "fetch_page": make_fetcher([[server]]),
            "pinger": lambda address: 33.0,
            "ping_attempts": 2,
        })
        browser.refresh_list()
        browser.add_favorite(1, group="常玩", note="ping 低")
        detail = browser.server_detail(1)
        assert detail["name"] == server["name"]
        assert detail["description"] == "24 小时练枪"
        assert detail["version"] == "1.38"
        assert detail["tags"] == ["练枪", "无密码"]
        assert detail["player_list"] == ["玩家A", "玩家B"]
        assert detail["ping_stats"]["ping"] == pytest.approx(33.0)
        assert detail["ping_stats"]["samples"] == 2
        assert detail["favorite"]["group"] == "常玩"
        assert detail["favorite"]["note"] == "ping 低"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
