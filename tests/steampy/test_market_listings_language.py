"""
Блок заявок на странице маркета находится по строкам mybuyorder_<id>, а не по заголовку.

16.09.2026 Steam переписал русскую локализацию маркета («запрос на покупку» → «заявка»).
Заголовок «Мои запросы на покупку», по которому get_market_listings_from_html искал блок,
пропал, и у аккаунтов с русским Steam агент стал отдавать серверу 0 заявок вместо реальных
(команда market_get_my_buy_orders), а английские аккаунты видели свои заявки как раньше.
Сервер считал, что заявок нет: не мог их снять и пытался ставить новые сверх лимита Steam.

Точный новый заголовок неизвестен (страница видна только под логином), поэтому тесты
проверяют, что заголовок вообще не влияет на разбор заявок.
"""

import sys
from pathlib import Path

import pytest

project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

from steampy.utils import get_market_listings_from_html


def _sell_table(heading: str, listing_id: str = "6001") -> str:
    return f'''
    <div class="my_listing_section market_content_block market_home_listing_table">
      <h3 class="my_market_header"><span class="my_market_header_active">{heading}</span></h3>
      <div class="market_listing_row market_recent_listing_row" id="mylisting_{listing_id}">
        <span class="market_listing_price" title="Цена покупателя">$1.15</span>
        <span class="market_listing_price" title="Вы получите">($1.00)</span>
        <div class="market_listing_listed_date">3 окт</div>
      </div>
    </div>
    '''


def _buy_table(heading: str, rows: str) -> str:
    return f'''
    <div class="my_listing_section market_content_block market_home_listing_table">
      <h3 class="my_market_header"><span class="my_market_header_active">{heading}</span></h3>
      {rows}
    </div>
    '''


BUY_ROWS = '''
  <div class="market_listing_row market_recent_listing_row" id="mybuyorder_7001">
    <span class="market_listing_price">7 @ $0.10</span>
    <a href="https://steamcommunity.com/market/listings/730/AWP%20%7C%20Safari%20Mesh%20%28Field-Tested%29">AWP | Safari Mesh (Field-Tested)</a>
  </div>
  <div class="market_listing_row market_recent_listing_row" id="mybuyorder_7002">
    <span class="market_listing_price">1 @ $95.92</span>
    <a href="https://steamcommunity.com/market/listings/730/Sticker%20%7C%20Stone%20Scales">Sticker | Stone Scales</a>
  </div>
'''


def _page(*tables: str) -> str:
    return f'<html><body><div id="myListings">{"".join(tables)}</div></body></html>'


@pytest.mark.parametrize(
    "heading",
    [
        "My buy orders",
        "Мои запросы на покупку",  # до 16.09.2026
        "Мои заявки на покупку",
        "Mes ordres d'achat",
        "",
    ],
)
def test_buy_orders_found_whatever_the_heading(heading):
    html = _page(_sell_table("Мои лоты на продажу"), _buy_table(heading, BUY_ROWS))

    listings = get_market_listings_from_html(html)

    assert set(listings["buy_orders"]) == {"7001", "7002"}
    assert listings["buy_orders"]["7001"]["quantity"] == 7
    assert listings["buy_orders"]["7001"]["item_name"] == "AWP | Safari Mesh (Field-Tested)"
    assert listings["buy_orders"]["7002"]["price"] == "$95.92"


def test_sell_listings_still_parsed_next_to_buy_orders():
    html = _page(_sell_table("Мои лоты на продажу"), _buy_table("Мои заявки на покупку", BUY_ROWS))

    listings = get_market_listings_from_html(html)

    assert set(listings["sell_listings"]) == {"6001"}
    assert listings["sell_listings"]["6001"]["need_confirmation"] is False


def test_listings_awaiting_confirmation_not_taken_for_buy_orders():
    html = _page(
        _sell_table("My sell listings", "6001"),
        _sell_table("My listings awaiting confirmation", "6002"),
        _buy_table("My buy orders", BUY_ROWS),
    )

    listings = get_market_listings_from_html(html)

    assert set(listings["sell_listings"]) == {"6001", "6002"}
    assert listings["sell_listings"]["6002"]["need_confirmation"] is True
    assert set(listings["buy_orders"]) == {"7001", "7002"}


def test_no_buy_orders_gives_empty_dict():
    html = _page(_sell_table("Мои лоты на продажу"), _buy_table("Мои заявки на покупку", ""))

    listings = get_market_listings_from_html(html)

    assert listings["buy_orders"] == {}
    assert set(listings["sell_listings"]) == {"6001"}
