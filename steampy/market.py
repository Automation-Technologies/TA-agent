import json
import re
import time
import urllib.parse
from decimal import Decimal

from curl_cffi.requests import Session

from steampy.confirmation import ConfirmationExecutor
from steampy.exceptions import ApiException, TooManyRequests, LoginRequired, ConfirmationExpected, SessionNeedsAuth
from steampy.models import Currency, SteamUrl, GameOptions
from steampy.utils import get_listing_id_to_assets_address_from_html, get_market_listings_from_html, \
    merge_items_with_descriptions_from_listing, get_market_sell_listings_from_api


_BUY_ORDERS_COUNTER = re.compile(rb'id="my_market_buylistings_number"[^>]*>([^<]*)<')
_BUY_ORDER_ROW = re.compile(rb'id="mybuyorder_\d+"')


def expected_buy_orders_count(html: bytes) -> int:
    """Сколько заявок на покупку сама страница маркета говорит, что есть.

    Берём то, что не зависит от языка и от того, как мы разбираем блоки: счётчик
    в заголовке блока заявок (id="my_market_buylistings_number", «(957)») и число
    строк mybuyorder_<id> в сыром HTML. Счётчика нет и строк нет — 0 (у аккаунта
    без заявок блока может не быть вовсе, это нормальный ответ)."""
    counter = 0
    m = _BUY_ORDERS_COUNTER.search(html)
    if m:
        digits = re.sub(rb'\D', b'', m.group(1))
        counter = int(digits) if digits else 0
    return max(counter, len(_BUY_ORDER_ROW.findall(html)))


def login_required(func):
    def func_wrapper(self, *args, **kwargs):
        if not self.was_login_executed:
            raise LoginRequired('Use login method first on SteamClient')
        else:
            return func(self, *args, **kwargs)

    return func_wrapper


class SteamMarket:
    def __init__(self, session: Session):
        self._session = session
        self._steam_guard = None
        self._session_id = None
        self.was_login_executed = False

    def __getstate__(self):
        state = self.__dict__.copy()
        if "_session" in state:
            del state["_session"]
        return state

    def __setstate__(self, state):
        self.__dict__.update(state)
        self._session = None

    def _set_login_executed(self, steamguard: dict, session_id: str):
        self._steam_guard = steamguard
        self._session_id = session_id
        self.was_login_executed = True

    def fetch_price(self, item_hash_name: str, game: GameOptions, currency: Currency) -> dict:
        url = SteamUrl.COMMUNITY_URL + '/market/priceoverview/'
        params = {'country': 'PL',
                  'currency': currency.value,
                  'appid': game.app_id,
                  'market_hash_name': item_hash_name}
        response = self._session.get(url, params=params, timeout=60)
        if response.status_code == 429:
            raise TooManyRequests("You can fetch maximum 20 prices in 60s period")
        return response.json()

    @login_required
    def fetch_price_history(self, item_hash_name: str, game: GameOptions) -> dict:
        url = SteamUrl.COMMUNITY_URL + '/market/pricehistory/'
        params = {'country': 'PL',
                  'appid': game.app_id,
                  'market_hash_name': item_hash_name}
        response = self._session.get(url, params=params, timeout=60)
        if response.status_code == 429:
            raise TooManyRequests("You can fetch maximum 20 prices in 60s period")
        return response.json()

    @login_required
    def get_my_buy_orders(self) -> dict:
        """Получение только buy-ордеров без загрузки sell-листингов"""
        max_retries = 5
        buy_orders = None
        last_error = None

        for attempt in range(max_retries):
            try:
                response = self._session.get("%s/market" % SteamUrl.COMMUNITY_URL, timeout=60)
                if response.status_code != 200:
                    last_error = f"HTTP code: {response.status_code}"
                    if attempt < max_retries - 1:
                        delay = 2 ** attempt
                        time.sleep(delay)
                        continue
                    else:
                        raise ApiException(f"There was a problem getting buy orders. {last_error}")

                pattern = re.search(rb'var\s+g_rgAssets\s*=\s*(\{.*?});\n', response.content, re.DOTALL)
                if pattern:
                    json_bytes = pattern.group(1)
                    assets_descriptions = json.loads(json_bytes.decode('utf-8'))
                else:
                    assets_descriptions = {}
                
                listing_id_to_assets_address = get_listing_id_to_assets_address_from_html(response.content)
                listings = get_market_listings_from_html(response.content)
                listings = merge_items_with_descriptions_from_listing(
                    listings, 
                    listing_id_to_assets_address,
                    assets_descriptions
                )

                time.sleep(5.4)

                buy_orders = listings.get('buy_orders')

                # Страница говорит «заявки есть», а разбор не нашёл ни одной — это поломка
                # разбора, а не пустой аккаунт. Отдать {} нельзя: сервер примет его за
                # «0 заявок» — сочтёт их исполненными, не сможет снять и распланирует бюджет
                # заново. Ровно так после смены русского заголовка блока 16.09.2026 агент
                # отдавал серверу 0 заявок (BUG-103). Падаем громко, а ретрай ниже переживёт
                # разовый кривой ответ.
                expected = expected_buy_orders_count(response.content)
                if expected and not buy_orders:
                    raise ApiException(
                        f"Steam показывает заявок на покупку: {expected}, а разобрано 0 — "
                        f"разметка страницы маркета изменилась, список заявок неизвестен"
                    )

                if buy_orders is not None:
                    break

                if attempt < max_retries - 1:
                    delay = 2 ** attempt
                    time.sleep(delay)
                else:
                    raise ApiException(
                        f"Steam вернул пустой ответ после {max_retries} попыток. buy_orders={buy_orders}"
                    )
            except ApiException as e:
                if attempt == max_retries - 1:
                    raise
                last_error = str(e)
                delay = 2 ** attempt
                time.sleep(delay)
            except Exception as e:
                last_error = str(e)
                if attempt < max_retries - 1:
                    delay = 2 ** attempt
                    time.sleep(delay)
                else:
                    raise ApiException(
                        f"Ошибка при получении buy-ордеров после {max_retries} попыток. "
                        f"Последняя ошибка: {last_error}"
                    )

        return buy_orders

    @login_required
    def get_my_recent_sell_listings(self) -> dict:
        """Получение только 10 самых свежих sell-листингов (последняя страница)"""
        response = self._session.get("%s/market" % SteamUrl.COMMUNITY_URL, timeout=60)
        if response.status_code != 200:
            raise ApiException(f"There was a problem getting sell listings. HTTP code: {response.status_code}")
        
        end_pattern = re.search(rb'<span id="tabContentsMyActiveMarketListings_end">(\d+)</span>', response.content)
        total_pattern = re.search(rb'<span id="tabContentsMyActiveMarketListings_total">([\d,]+)</span>', response.content)
        
        if end_pattern and total_pattern:
            n_showing = int(end_pattern.group(1).decode('utf-8'))
            n_total = int(total_pattern.group(1).decode('utf-8').replace(',', ''))
            
            if n_total > n_showing:
                start_offset = max(0, n_total - 10)
                url = "%s/market/mylistings/render/?query=&start=%s&count=%s" % (SteamUrl.COMMUNITY_URL, start_offset, 10)
                response = self._session.get(url, timeout=60)
                if response.status_code != 200:
                    raise ApiException(f"There was a problem getting recent sell listings. HTTP code: {response.status_code}")
                jresp = response.json()
                listing_id_to_assets_address = get_listing_id_to_assets_address_from_html(jresp.get("hovers"))
                listings = get_market_sell_listings_from_api(jresp.get("results_html"))
                listings = merge_items_with_descriptions_from_listing(
                    listings, 
                    listing_id_to_assets_address,
                    jresp.get("assets")
                )
                time.sleep(5.4)
                return listings.get('sell_listings', {})
        
        pattern = re.search(rb'var\s+g_rgAssets\s*=\s*(\{.*?});\n', response.content, re.DOTALL)
        if pattern:
            json_bytes = pattern.group(1)
            assets_descriptions = json.loads(json_bytes.decode('utf-8'))
        else:
            assets_descriptions = {}
        
        listing_id_to_assets_address = get_listing_id_to_assets_address_from_html(response.content)
        listings = get_market_listings_from_html(response.content)
        listings = merge_items_with_descriptions_from_listing(
            listings, 
            listing_id_to_assets_address,
            assets_descriptions
        )
        time.sleep(5.4)
        return listings.get('sell_listings', {})

    @login_required
    def get_my_sell_listings(self) -> dict:
        """Все sell-листинги через render-эндпоинт с пагинацией и ретраями.

        Раньше метод парсил спаны _total/_end со страницы /market, которые Steam
        отдаёт НЕСТАБИЛЬНО (часто пусто) → возвращались только лоты с самой страницы
        (или {}) даже при сотнях активных листингов, и сервер не находил listing_id
        только что выставленных предметов. Render-эндпоинт (/market/mylistings/render)
        отдаёт total_count и results_html надёжно — идём постранично, ретраим флап
        Steam (иногда отдаёт [] вместо объекта). Возврат: {listing_id: {...,
        'description': {'id': asset_id, ...}}}.
        """
        all_sell: dict = {}
        start = 0
        page = 100
        total = None
        MAX_PAGES = 500  # предохранитель от бесконечного цикла (до ~50000 листингов)

        for _ in range(MAX_PAGES):
            jresp = None
            for attempt in range(4):
                try:
                    url = "%s/market/mylistings/render/?query=&start=%s&count=%s" % (
                        SteamUrl.COMMUNITY_URL, start, page
                    )
                    response = self._session.get(url, timeout=60)
                    if response.status_code == 200:
                        j = response.json()
                        # под троттлингом Steam иногда отдаёт [] вместо объекта
                        if isinstance(j, dict) and j.get("results_html") is not None:
                            jresp = j
                            break
                except Exception:
                    pass
                time.sleep(3.0 + attempt * 2.0)

            if jresp is None:
                # страница не догрузилась после ретраев — отдаём собранное
                break

            if total is None:
                total = jresp.get("total_count") or 0

            listing_id_to_assets_address = get_listing_id_to_assets_address_from_html(jresp.get("hovers") or "")
            listings = get_market_sell_listings_from_api(jresp.get("results_html"))
            listings = merge_items_with_descriptions_from_listing(
                listings,
                listing_id_to_assets_address,
                jresp.get("assets") or {},
            )
            page_sell = listings.get("sell_listings", {})
            if not page_sell:
                break

            all_sell.update(page_sell)
            start += page
            if start >= (total or 0):
                break
            time.sleep(5.4)  # обязательная пауза между запросами к Steam

        return all_sell

    @login_required
    def get_my_market_listings(self) -> dict:
        """Получение и buy-ордеров, и sell-листингов (обратная совместимость)"""
        buy_orders = self.get_my_buy_orders()
        sell_listings = self.get_my_sell_listings()
        
        return {
            'buy_orders': buy_orders,
            'sell_listings': sell_listings
        }

    @staticmethod
    def _sell_listing_awaits_confirmation(response: dict) -> bool:
        if response.get("success") is not False:
            return False
        message = response.get("message")
        if not isinstance(message, str):
            return False
        message_lower = message.lower()
        return (
            "listing for this item pending confirmation" in message_lower
            or "ожидает вашего согласия" in message_lower
        )

    @login_required
    def create_sell_order(self, assetid: str, game: GameOptions, money_to_receive: str) -> dict:
        data = {
            "assetid": assetid,
            "sessionid": self._session_id,
            "contextid": game.context_id,
            "appid": game.app_id,
            "amount": 1,
            "price": money_to_receive
        }
        headers = {'Referer': "%s/profiles/%s/inventory" % (SteamUrl.COMMUNITY_URL, self._steam_guard['steamid'])}
        response = self._session.post(SteamUrl.COMMUNITY_URL + "/market/sellitem/", data=data, headers=headers)
        try:
            response = response.json()
        except json.JSONDecodeError as e:
            text = (response.text or "")[:500]
            raise ApiException(
                f"Steam вернул не-JSON при create_sell_order: status={response.status_code}, body={text!r}, error={e}"
            ) from e
        if response.get("needs_mobile_confirmation") or self._sell_listing_awaits_confirmation(response):
            return self._confirm_sell_listing_with_retry(assetid)
        return response

    def create_buy_order(self, market_name: str, price_single_item: str, quantity: int, game: GameOptions,
                         currency: Currency = Currency.USD) -> dict:
        headers = {
            "Referer": f"{SteamUrl.COMMUNITY_URL}/market/listings/{game.app_id}/{urllib.parse.quote(market_name)}"
        }

        # First request to place the order
        data = {
            "sessionid": self._session_id,
            "currency": currency.value,
            "appid": game.app_id,
            "market_hash_name": market_name,
            "price_total": str(Decimal(price_single_item) * Decimal(quantity)),
            "quantity": quantity,
            "confirmation": 0  # initial value is 0
        }

        response = self._session.post(
            SteamUrl.COMMUNITY_URL + "/market/createbuyorder/",
            data=data,
            headers=headers
        ).json()

        # If the order is successful, return immediately
        if response.get("success") == 1:
            return response

        # If mobile confirmation is required
        if response.get("need_confirmation"):
            if not self._steam_guard:
                raise ApiException("Order requires mobile confirmation, but steam_guard info is not provided")

            confirmation_id = response["confirmation"]["confirmation_id"]

            # Execute mobile confirmation
            confirmation_executor = ConfirmationExecutor(self._steam_guard['identity_secret'],
                                                         self._steam_guard['steamid'],
                                                         self._session)

            time.sleep(1)
            success = confirmation_executor.confirm_by_id(confirmation_id)
            if not success:
                raise ApiException("Mobile confirmation failed")

            # Second request, update confirmation to the confirmed ID
            data["confirmation"] = confirmation_id
            time.sleep(1)
            response = self._session.post(
                SteamUrl.COMMUNITY_URL + "/market/createbuyorder/",
                data=data,
                headers=headers
            ).json()

            if response.get("success") == 1:
                return response
            else:
                raise ApiException(f"Order failed after confirmation: {response}")

        # Other exceptions
        raise ApiException(f"Buy order failed: {response}")

    @login_required
    def buy_item(self, market_name: str, market_id: str, price: int, fee: int, game: GameOptions,
                 currency: Currency = Currency.USD) -> dict:
        data = {
            "sessionid": self._session_id,
            "currency": currency.value,
            "subtotal" : price - fee,
            "fee" : fee,
            "total" : price,
            "quantity": '1'
        }
        headers = {'Referer': "%s/market/listings/%s/%s" % (SteamUrl.COMMUNITY_URL, game.app_id,
                                                            urllib.parse.quote(market_name))}
        response = self._session.post(SteamUrl.COMMUNITY_URL + "/market/buylisting/" + market_id, data=data,
                                      headers=headers).json()
        try:
            if response["wallet_info"]["success"] != 1:
                raise ApiException("There was a problem buying this item. Are you using the right currency? success: %s"
                                   % response['wallet_info']['success'])
        except:
            raise ApiException("There was a problem buying this item. Message: %s"
                               % response.get("message"))
        return response

    @login_required
    def cancel_sell_order(self, sell_listing_id: str) -> None:
        data = {"sessionid": self._session_id}
        headers = {'Referer': SteamUrl.COMMUNITY_URL + "/market/"}
        url = "%s/market/removelisting/%s" % (SteamUrl.COMMUNITY_URL, sell_listing_id)
        response = self._session.post(url, data=data, headers=headers)
        if response.status_code != 200:
            raise ApiException("There was a problem removing the listing. http code: %s" % response.status_code)

    @login_required
    def cancel_buy_order(self, buy_order_id) -> dict:
        data = {
            "sessionid": self._session_id,
            "buy_orderid": buy_order_id
        }
        headers = {"Referer": SteamUrl.COMMUNITY_URL + "/market"}
        response = self._session.post(SteamUrl.COMMUNITY_URL + "/market/cancelbuyorder/", data=data, headers=headers).json()
        if response.get("success") != 1:
            raise ApiException("There was a problem canceling the order. success: %s" % response.get("success"))
        return response

    @login_required
    def get_market_history(self, start: int, count: int) -> dict:
        """Сырой ответ Steam API: история покупок/продаж на маркете (results_html, hovers, assets, total_count, success)."""
        url = SteamUrl.COMMUNITY_URL + "/market/myhistory/render"
        params = {"query": "", "start": start, "count": count}
        response = self._session.get(url, params=params, timeout=60)
        response.encoding = "utf-8-sig"
        response_dict = response.json()
        if not response_dict.get("success"):
            raise ApiException("Market history request returned success=false.")
        return response_dict

    def _confirm_sell_listing(self, asset_id: str) -> dict:
        con_executor = ConfirmationExecutor(self._steam_guard['identity_secret'], self._steam_guard['steamid'],
                                            self._session)
        return con_executor.confirm_sell_listing(asset_id)

    def _confirm_sell_listing_with_retry(self, asset_id: str, attempts: int = 6, base_delay: float = 3.0) -> dict:
        # Подтверждение листинга может появиться в mobileconf не мгновенно,
        # а при массовом выставлении (или троттлинге Steam) список подтверждений
        # отдаётся с заметной задержкой. Поэтому ждём с нарастающим бэкоффом.
        # ВАЖНО: повторяем ТОЛЬКО подтверждение, листинг повторно НЕ создаём,
        # поэтому дублей листингов не возникает. Задержка добавляется лишь на
        # пути сбоя — при успешном подтверждении возврат происходит сразу.
        last_exc = None
        for attempt in range(1, attempts + 1):
            try:
                return self._confirm_sell_listing(asset_id)
            except SessionNeedsAuth:
                # needauth ретраями не лечится (нужен новый вход в аккаунт) —
                # пробрасываем сразу, чтобы не ждать впустую.
                raise
            except ConfirmationExpected as exc:
                last_exc = exc
                if attempt < attempts:
                    # Нарастающий бэкофф: 3, 6, 9, 12, 15с (ограничен 20с)
                    time.sleep(min(base_delay + (attempt - 1) * 3.0, 20.0))
        raise last_exc
