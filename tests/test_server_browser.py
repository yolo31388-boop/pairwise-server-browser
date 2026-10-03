"""
服务器浏览器与筛选系统 - 验收测试
运行方式：python -m pytest tests/test_server_browser.py -q
共 14 个测试用例
"""
import pytest
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from network.server_browser import ServerBrowser


def make_server(sid, name="srv", players=5, max_players=10, ping=50,
                map_name="de_dust2", region="asia", game_mode="casual"):
    return {
        "id": sid, "name": name, "players": players,
        "max_players": max_players, "ping": ping, "map_name": map_name,
        "region": region, "game_mode": game_mode,
    }


def fetcher_from(servers, fail_times=0, counter=None):
    state = {"calls": 0, "fail_left": fail_times}

    def fetch(page, page_size, timeout):
        state["calls"] += 1
        if counter is not None:
            counter.append(page)
        if state["fail_left"] > 0:
            state["fail_left"] -= 1
            raise ConnectionError("server not responding")
        start = page * page_size
        batch = servers[start:start + page_size]
        return {"servers": batch, "total": len(servers)}

    return fetch, state


class TestServerBrowser:
    def setup_method(self):
        self.system = ServerBrowser()

    # ---- 列表刷新：重试 / 超时 / 分页 ----

    def test_case_01(self):
        """刷新失败后自动重试，最终成功拿到列表"""
        servers = [make_server("s1")]
        fetch, state = fetcher_from(servers, fail_times=2)
        self.system.set_fetcher(fetch)
        result = self.system.refresh_list()
        assert len(result) == 1
        assert result[0]["id"] == "s1"
        assert state["calls"] == 3  # 2 次失败 + 1 次成功

    def test_case_02(self):
        """超过最大重试次数后放弃并记录错误"""
        fetch, state = fetcher_from([], fail_times=99)
        self.system.set_fetcher(fetch)
        result = self.system.refresh_list()
        assert result == []
        assert self.system.last_error == "fetch_failed"
        assert state["calls"] == self.system.config["retry_count"]

    def test_case_03(self):
        """单次请求超过 timeout 视为失败并重试"""
        clock = {"t": 0.0}

        def slow_fetch(page, page_size, timeout):
            clock["t"] += 100.0  # 模拟耗时远超超时
            return {"servers": [make_server("s1")], "total": 1}

        system = ServerBrowser({"timeout": 1.0, "retry_count": 2,
                                "clock": lambda: clock["t"]})
        system.set_fetcher(slow_fetch)
        result = system.refresh_list()
        assert result == []
        assert system.last_error == "fetch_failed"

    def test_case_04(self):
        """1000 个服务器按 page_size 分页拉取，全部收齐"""
        servers = [make_server("s%d" % i) for i in range(1000)]
        pages = []
        fetch, _ = fetcher_from(servers, counter=pages)
        system = ServerBrowser({"page_size": 100})
        system.set_fetcher(fetch)
        result = system.refresh_list()
        assert len(result) == 1000
        assert pages == list(range(10))  # 10 页，每页 100

    # ---- 筛选：多条件组合 + 排序 ----

    def test_case_05(self):
        """has_slots 筛选排除满人服务器"""
        self.system._servers = [
            make_server("s1", players=10, max_players=10),
            make_server("s2", players=5, max_players=10),
        ]
        result = self.system.filter_servers({"has_slots": True})
        assert [s["id"] for s in result] == ["s2"]

    def test_case_06(self):
        """多条件组合筛选：有空位 + 指定地区 + 指定模式 + ping 上限"""
        self.system._servers = [
            make_server("s1", players=5, max_players=10, region="asia",
                        game_mode="casual", ping=30),
            make_server("s2", players=5, max_players=10, region="asia",
                        game_mode="casual", ping=300),
            make_server("s3", players=10, max_players=10, region="asia",
                        game_mode="casual", ping=30),
            make_server("s4", players=5, max_players=10, region="eu",
                        game_mode="casual", ping=30),
            make_server("s5", players=5, max_players=10, region="asia",
                        game_mode="ranked", ping=30),
        ]
        result = self.system.filter_servers({
            "has_slots": True, "region": "asia",
            "game_mode": "casual", "max_ping": 100,
        })
        assert [s["id"] for s in result] == ["s1"]

    def test_case_07(self):
        """排序：按 ping、玩家数、地图名排序"""
        self.system._servers = [
            make_server("s1", players=3, ping=80, map_name="de_nuke"),
            make_server("s2", players=9, ping=20, map_name="de_dust2"),
            make_server("s3", players=5, ping=50, map_name="de_mirage"),
        ]
        by_ping = self.system.filter_servers(sort_by="ping")
        assert [s["id"] for s in by_ping] == ["s2", "s3", "s1"]
        by_players = self.system.filter_servers(sort_by="players",
                                                sort_order="desc")
        assert [s["id"] for s in by_players] == ["s2", "s3", "s1"]
        by_map = self.system.filter_servers(sort_by="map_name")
        assert [s["id"] for s in by_map] == ["s2", "s3", "s1"]

    # ---- ping 检测：多发取平均 + 定时更新 ----

    def test_case_08(self):
        """ping 检测发多个包并取平均值"""
        samples = iter([20.0, 30.0, 40.0, 50.0])
        system = ServerBrowser({"ping_samples": 4,
                                "pinger": lambda srv: next(samples)})
        system._servers = [make_server("s1")]
        avg = system.ping_server("s1")
        assert avg == pytest.approx(35.0)
        assert system.get_ping("s1") == pytest.approx(35.0)

    def test_case_09(self):
        """丢包（返回 None）被剔除，不拉低为 0ms"""
        samples = iter([None, 100.0, None, 120.0])
        system = ServerBrowser({"ping_samples": 4,
                                "pinger": lambda srv: next(samples)})
        system._servers = [make_server("s1")]
        avg = system.ping_server("s1")
        assert avg == pytest.approx(110.0)

    def test_case_10(self):
        """update(dt) 到达间隔后自动重测 ping"""
        pings = {"value": 50.0}
        system = ServerBrowser({"ping_interval": 10.0,
                                "info_refresh_interval": 9999.0,
                                "pinger": lambda srv: pings["value"]})
        system._servers = [make_server("s1")]
        system.ping_all()
        assert system.get_ping("s1") == pytest.approx(50.0)
        pings["value"] = 200.0
        system.update(5.0)   # 未到间隔，不更新
        assert system.get_ping("s1") == pytest.approx(50.0)
        system.update(5.0)   # 到达 10s，重新检测
        assert system.get_ping("s1") == pytest.approx(200.0)

    # ---- 收藏：持久化 + 分组 + 备注 ----

    def test_case_11(self):
        """收藏持久化：重启（新实例）后收藏列表仍在"""
        import tempfile
        path = os.path.join(tempfile.mkdtemp(), "favorites.json")
        s1 = ServerBrowser({"favorites_file": path})
        s1.add_favorite("s1", group="常玩", note="低延迟")
        s2 = ServerBrowser({"favorites_file": path})
        favs = s2.get_favorites()
        assert len(favs) == 1
        assert favs[0]["server_id"] == "s1"
        assert favs[0]["group"] == "常玩"
        assert favs[0]["note"] == "低延迟"

    def test_case_12(self):
        """收藏支持分组与备注，可按组查询"""
        self.system.add_favorite("s1", group="常玩", note="朋友开的")
        self.system.add_favorite("s2", group="比赛", note="")
        self.system.add_favorite("s3", group="常玩")
        assert self.system.get_favorite_groups() == ["常玩", "比赛"]
        casual = self.system.get_favorites(group="常玩")
        assert {f["server_id"] for f in casual} == {"s1", "s3"}
        note = [f for f in casual if f["server_id"] == "s1"][0]["note"]
        assert note == "朋友开的"

    # ---- 服务器信息：完整字段 + 定时更新 + 详细页面 ----

    def test_case_13(self):
        """服务器信息包含完整字段，且随刷新定时更新"""
        fetch, _ = fetcher_from([make_server("s1", players=3)])
        system = ServerBrowser({"info_refresh_interval": 5.0,
                                "ping_interval": 9999.0})
        system.set_fetcher(fetch)
        system.refresh_list()
        info = system.server_info("s1")
        for key in ("name", "map_name", "players", "max_players",
                    "ping", "game_mode", "region"):
            assert key in info
        assert info["players"] == 3
        # 玩家数变化后，定时刷新让信息更新
        fetch2, _ = fetcher_from([make_server("s1", players=8)])
        system.set_fetcher(fetch2)
        system.update(5.0)
        assert system.server_info("s1")["players"] == 8

    def test_case_14(self):
        """详细页面：包含扩展字段与收藏状态"""
        srv = make_server("s1")
        srv.update({"player_list": ["alice", "bob"],
                    "description": "welcome", "version": "1.2"})
        self.system._servers = [srv]
        self.system.add_favorite("s1", group="常玩")
        detail = self.system.get_server_detail("s1")
        assert detail["player_list"] == ["alice", "bob"]
        assert detail["description"] == "welcome"
        assert detail["version"] == "1.2"
        assert detail["favorite"] is True
        assert detail["players"] == 5
        assert self.system.get_server_detail("missing") is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
