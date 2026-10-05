"""
Разбор списка buy-ордеров: имя предмета берётся из ссылки, а не из HTML тега.

Прежний разбор в get_buy_orders_from_node резал строку тега по тексту ссылки:

    named = str(order.a).split(order.a.text)[0]

и врал в двух случаях:

  • имя без спецсимволов встречается прямо в href — split резал по вхождению ВНУТРИ
    адреса, и после split('/') имя выходило пустым. Так ломались короткие имена Rust
    («Metal», «Hazma», «Skull»);
  • имя содержит HTML-сущность: BeautifulSoup отдаёт «Dreams & Nightmares Case», а в
    разметке стоит «Dreams &amp; Nightmares Case» — разделитель не находился, split
    возвращал тег целиком, и именем становился обломок закрывающего тега — «a>».

Цена вопроса: заявку с испорченным именем сервер не узнаёт как уже выставленную, каждый
цикл планирует на предмет новую и получает от Steam success=29 («You already have an
active buy order for this item») — слот из 1000 залипает навсегда.
"""

import sys
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

from steampy.utils import get_buy_orders_from_node


def _order_html(order_id: str, appid: str, href_name: str, link_text: str, price: str = "5 @ 38,69 руб.") -> str:
    return f'''
    <div id="mybuyorder_{order_id}" class="market_listing_row">
      <span class="market_listing_price">{price}</span>
      <a href="https://steamcommunity.com/market/listings/{appid}/{href_name}">{link_text}</a>
    </div>
    '''


def _parse(html: str) -> dict:
    return get_buy_orders_from_node(BeautifulSoup(html, "lxml"))


def test_short_name_matching_url_is_not_lost():
    """«Metal» встречается в href как есть — раньше имя выходило пустым."""
    orders = _parse(_order_html("8625299838", "252490", "Metal", "Metal"))

    assert orders["8625299838"]["item_name"] == "Metal"


def test_name_with_html_entity_is_decoded():
    """«&» в имени экранирован в разметке — раньше именем становился обломок «a>»."""
    html = _order_html(
        "8625405999",
        "730",
        "Dreams%20%26%20Nightmares%20Case",
        "Dreams &amp; Nightmares Case",
    )

    orders = _parse(html)

    assert orders["8625405999"]["item_name"] == "Dreams & Nightmares Case"


def test_ordinary_name_still_parsed():
    """Обычные имена со скобками и вертикальной чертой работали и раньше — не сломать."""
    html = _order_html(
        "8625405111",
        "730",
        "AK-47%20%7C%20Redline%20%28Field-Tested%29",
        "AK-47 | Redline (Field-Tested)",
    )

    orders = _parse(html)

    assert orders["8625405111"]["item_name"] == "AK-47 | Redline (Field-Tested)"


@pytest.mark.parametrize(
    "href_name, link_text, expected",
    [
        ("Metal", "Metal", "Metal"),
        ("Hazma", "Hazma", "Hazma"),
        ("Skull", "Skull", "Skull"),
        ("Dreams%20%26%20Nightmares%20Case", "Dreams &amp; Nightmares Case", "Dreams & Nightmares Case"),
        ("Sawed-Off%20%7C%20Kiss%E2%99%A5Love%20%28Well-Worn%29", "Sawed-Off | Kiss♥Love (Well-Worn)",
         "Sawed-Off | Kiss♥Love (Well-Worn)"),
        ("Charm%20%7C%20Die-cast%20AK", "Charm | Die-cast AK", "Charm | Die-cast AK"),
    ],
)
def test_names_seen_in_real_orders(href_name, link_text, expected):
    """Имена, реально встречавшиеся в заявках."""
    orders = _parse(_order_html("1", "730", href_name, link_text))

    assert orders["1"]["item_name"] == expected


def test_localized_link_text_does_not_change_the_name():
    """На русской странице текст ссылки может быть переведён — имя всё равно из href."""
    html = _order_html("3", "730", "Dreams%20%26%20Nightmares%20Case", "Кейс «Грёзы и кошмары»")

    orders = _parse(html)

    assert orders["3"]["item_name"] == "Dreams & Nightmares Case"


def test_appid_and_quantity_still_read():
    """Соседние поля разбора не задеты."""
    orders = _parse(_order_html("42", "252490", "Metal", "Metal", price="7 @ 12,34 руб."))

    order = orders["42"]
    assert order["appid"] == "252490"
    assert order["quantity"] == 7
    assert order["price"] == "12,34 руб."
    assert order["order_id"] == "42"


def test_query_string_in_href_is_stripped():
    """Хвост запроса в ссылке не должен попадать в имя."""
    html = '''
    <div id="mybuyorder_7" class="market_listing_row">
      <span class="market_listing_price">1 @ 10,00 руб.</span>
      <a href="https://steamcommunity.com/market/listings/730/Fever%20Case?filter=x#scroll">Fever Case</a>
    </div>
    '''

    orders = _parse(html)

    assert orders["7"]["item_name"] == "Fever Case"


def test_unexpected_markup_falls_back_to_link_text():
    """Если Steam сменит вёрстку ссылки — берём текст, заявка не теряется."""
    html = '''
    <div id="mybuyorder_9" class="market_listing_row">
      <span class="market_listing_price">2 @ 50,00 руб.</span>
      <a href="/some/other/path">Snakebite Case</a>
    </div>
    '''

    orders = _parse(html)

    assert orders["9"]["item_name"] == "Snakebite Case"
    assert orders["9"]["appid"] == ""
