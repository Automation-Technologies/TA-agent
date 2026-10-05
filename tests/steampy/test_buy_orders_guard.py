"""
get_my_buy_orders не отдаёт «0 заявок», когда страница маркета говорит, что заявки есть.

BUG-103: 16.09.2026 Steam сменил русский заголовок блока заявок, разбор перестал его
находить и молча возвращал {} — агент отдавал серверу 0 заявок, и тот считал, что заявок
нет. Сам разбор починен (блок ищется по строкам mybuyorder_<id>); здесь — защита на случай
следующей смены вёрстки: счётчик заявок на странице и строки mybuyorder_ в сыром HTML
сверяются с результатом разбора, и расхождение «есть → 0» — это ошибка, а не пустой ответ.
"""

import sys
from pathlib import Path

import pytest

project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

import steampy.market as market_module
from steampy.exceptions import ApiException
from steampy.market import SteamMarket, expected_buy_orders_count


def _page(buy_block: str) -> bytes:
    return f'''<html><body>
    <div id="myListings">
      <div id="tabContentsMyListings">
        {buy_block}
      </div>
    </div>
    </body></html>'''.encode("utf-8")


def _buy_block(heading: str, counter: str, rows: str) -> str:
    return f'''
    <div class="my_listing_section market_content_block market_home_listing_table">
      <h3 class="my_market_header">
        <span class="my_market_header_active">{heading}</span>
        <span class="my_market_header_count">(<span id="my_market_buylistings_number">{counter}</span>)</span>
      </h3>
      {rows}
    </div>'''


ROWS = '''
  <div class="market_listing_row market_recent_listing_row" id="mybuyorder_7001">
    <span class="market_listing_price">2 @ 1,36₴</span>
    <a href="https://steamcommunity.com/market/listings/730/P250%20%7C%20Sand%20Dune%20%28Field-Tested%29">P250 | Sand Dune (Field-Tested)</a>
  </div>
  <div class="market_listing_row market_recent_listing_row" id="mybuyorder_7002">
    <span class="market_listing_price">1 @ 3,06₴</span>
    <a href="https://steamcommunity.com/market/listings/730/Sticker%20%7C%20Stone%20Scales">Sticker | Stone Scales</a>
  </div>
'''

# Будущая вёрстка: строки заявок переименованы, разбор их не узнаёт, но счётчик на месте.
ROWS_RENAMED = ROWS.replace('id="mybuyorder_', 'id="buyorder_row_')


class _Resp:
    def __init__(self, content: bytes):
        self.status_code = 200
        self.content = content


class _Session:
    def __init__(self, content: bytes):
        self.content = content
        self.calls = 0

    def get(self, url, timeout=None, **kwargs):
        self.calls += 1
        return _Resp(self.content)


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(market_module.time, "sleep", lambda *_: None)


def _market(content: bytes) -> SteamMarket:
    m = SteamMarket(_Session(content))
    m.was_login_executed = True
    return m


def test_russian_page_with_new_heading_returns_orders():
    m = _market(_page(_buy_block("Мои заявки на покупку", "2", ROWS)))
    orders = m.get_my_buy_orders()
    assert sorted(o["item_name"] for o in orders.values()) == [
        "P250 | Sand Dune (Field-Tested)", "Sticker | Stone Scales"]
    assert m._session.calls == 1


def test_counter_says_orders_exist_but_nothing_parsed_raises():
    m = _market(_page(_buy_block("Мои заявки на покупку", "957", ROWS_RENAMED)))
    with pytest.raises(ApiException, match="957"):
        m.get_my_buy_orders()
    # разовый кривой ответ переживается ретраями; стабильно кривой — ошибка после всех попыток
    assert m._session.calls == 5


def test_counter_with_thousands_separator_is_understood():
    assert expected_buy_orders_count(_page(_buy_block("x", "1,000", ""))) == 1000
    assert expected_buy_orders_count(_page(_buy_block("x", "1 000", ""))) == 1000


def test_rows_without_counter_also_count():
    # счётчик пропал, но строки mybuyorder_ в HTML есть — тоже «заявки есть»
    html = _page(f'<div class="market_home_listing_table">{ROWS}</div>')
    assert expected_buy_orders_count(html) == 2


def test_account_without_orders_is_still_a_valid_empty_answer():
    # у аккаунта без заявок блока может не быть вовсе, или счётчик 0 — это не ошибка
    assert _market(_page("")).get_my_buy_orders() == {}
    assert _market(_page(_buy_block("Мои заявки на покупку", "0", ""))).get_my_buy_orders() == {}
