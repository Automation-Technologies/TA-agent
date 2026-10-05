"""
appid лота на продажу берётся из адреса ассета, а не из полей описания.

g_rgAssets на странице маркета ключуется как {appid: {contextid: {assetid: описание}}},
поэтому appid есть всегда, даже если Steam не положил его в само описание. Сервер по нему
понимает, к какой игре относится лот, когда аккаунт торгует несколькими играми.
"""

import sys
from pathlib import Path

project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

from steampy.utils import merge_items_with_descriptions_from_listing


def test_sell_listing_appid_taken_from_asset_address():
    listings = {"sell_listings": {"L1": {"listing_id": "L1"}}}
    ids_to_assets_address = {"L1": ["440", "2", "A1"]}
    descriptions = {"440": {"2": {"A1": {"market_hash_name": "Mann Co. Key"}}}}

    merged = merge_items_with_descriptions_from_listing(listings, ids_to_assets_address, descriptions)

    assert merged["sell_listings"]["L1"]["description"]["appid"] == "440"


def test_sell_listing_keeps_steam_own_appid():
    """Если Steam сам прислал appid в описании — не перетираем его."""
    listings = {"sell_listings": {"L1": {"listing_id": "L1"}}}
    ids_to_assets_address = {"L1": ["730", "2", "A1"]}
    descriptions = {"730": {"2": {"A1": {"appid": 730, "market_hash_name": "AWP | Asiimov"}}}}

    merged = merge_items_with_descriptions_from_listing(listings, ids_to_assets_address, descriptions)

    assert str(merged["sell_listings"]["L1"]["description"]["appid"]) == "730"


def test_shared_description_is_not_mutated():
    """Описание копируется: один и тот же объект g_rgAssets не получает чужой appid."""
    shared = {"market_hash_name": "Mann Co. Key"}
    listings = {"sell_listings": {"L1": {"listing_id": "L1"}}}
    descriptions = {"440": {"2": {"A1": shared}}}

    merge_items_with_descriptions_from_listing(listings, {"L1": ["440", "2", "A1"]}, descriptions)

    assert "appid" not in shared
