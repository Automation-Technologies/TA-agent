"""
get_my_sell_listings читает все свои лоты через /market/mylistings/render постранично.

Раньше метод брал лоты со страницы /market и догружал остальные, только если на ней были
спаны tabContentsMyActiveMarketListings_end/_total. Steam отдаёт их нестабильно, и тогда
возвращались только лоты с самой страницы (или {}) даже при сотнях активных — сервер не
находил listing_id только что выставленных предметов. Render-эндпоинт сам говорит total_count.
"""

import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

import steampy.market as market_module
from steampy.market import SteamMarket


def _render_page(listing_ids, total: int) -> dict:
    rows = "".join(
        f'''<div class="market_listing_row market_recent_listing_row" id="mylisting_{lid}">
              <span class="market_listing_price" title="Цена покупателя">$1.15</span>
              <span class="market_listing_price" title="Вы получите">($1.00)</span>
              <div class="market_listing_listed_date">3 окт</div>
            </div>'''
        for lid in listing_ids
    )
    hovers = "".join(
        f"CreateItemHoverFromContainer( g_rgAssets, 'mylisting_{lid}_name', 730, '2', '9{lid}', 0 );\n"
        for lid in listing_ids
    )
    assets = {"730": {"2": {f"9{lid}": {"id": f"9{lid}", "market_hash_name": f"Item {lid}"} for lid in listing_ids}}}
    return {"success": True, "total_count": total, "results_html": rows, "hovers": hovers, "assets": assets}


class _Resp:
    def __init__(self, payload, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


class _Session:
    """Отвечает по start из URL; script[start] — список ответов по очереди (payload или _Resp)."""

    def __init__(self, script: dict):
        self.script = {start: list(answers) for start, answers in script.items()}
        self.starts = []

    def get(self, url, timeout=None, **kwargs):
        query = parse_qs(urlparse(url).query)
        assert urlparse(url).path == "/market/mylistings/render/"
        assert query["count"] == ["100"]
        start = int(query["start"][0])
        self.starts.append(start)
        answer = self.script[start].pop(0)
        return answer if isinstance(answer, _Resp) else _Resp(answer)


@pytest.fixture
def sleeps(monkeypatch):
    calls = []
    monkeypatch.setattr(market_module.time, "sleep", lambda seconds: calls.append(seconds))
    return calls


def _market(script: dict) -> SteamMarket:
    market = SteamMarket(_Session(script))
    market.was_login_executed = True
    return market


def test_all_pages_are_collected(sleeps):
    first, second = [str(6000 + i) for i in range(100)], [str(6100 + i) for i in range(50)]
    market = _market({0: [_render_page(first, 150)], 100: [_render_page(second, 150)]})

    listings = market.get_my_sell_listings()

    assert set(listings) == set(first) | set(second)
    assert market._session.starts == [0, 100]
    assert sleeps == [5.4]


def test_listing_carries_description_and_appid(sleeps):
    market = _market({0: [_render_page(["6001"], 1)]})

    listing = market.get_my_sell_listings()["6001"]

    assert listing["you_receive"] == "$1.00"
    assert listing["description"]["id"] == "96001"
    assert listing["description"]["appid"] == "730"


def test_throttled_answer_is_retried(sleeps):
    """Под троттлингом Steam иногда отдаёт [] вместо объекта или не-200 — страница перезапрашивается."""
    market = _market({0: [[], _Resp(None, status_code=429), _render_page(["6001", "6002"], 2)]})

    assert set(market.get_my_sell_listings()) == {"6001", "6002"}
    assert market._session.starts == [0, 0, 0]
    assert sleeps == [3.0, 5.0]


def test_page_that_never_loads_ends_the_walk(sleeps):
    """Как в серверной копии: страница не догрузилась за 4 попытки — отдаём собранное до неё."""
    first = [str(6000 + i) for i in range(100)]
    market = _market({0: [_render_page(first, 250)], 100: [[], [], [], []]})

    assert set(market.get_my_sell_listings()) == set(first)
    assert market._session.starts == [0, 100, 100, 100, 100]


def test_empty_page_stops_even_if_total_says_more(sleeps):
    market = _market({0: [_render_page(["6001"], 500)], 100: [_render_page([], 500)]})

    assert set(market.get_my_sell_listings()) == {"6001"}
    assert market._session.starts == [0, 100]


def test_account_without_listings(sleeps):
    market = _market({0: [_render_page([], 0)]})

    assert market.get_my_sell_listings() == {}
    assert sleeps == []
