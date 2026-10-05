"""
Подтверждение лота на продажу: понятные ошибки, needauth без ретраев, повтор подтверждения.

  • mobileconf/getlist при неавторизованной сессии отдаёт 200 с {"success":false,"needauth":true}.
    Раньше это падало в KeyError 'conf', а при не-200 — в пустой ConfirmationExpected, и по тексту
    ошибки было не понять, что случилось. Теперь — SessionNeedsAuth и ошибки с телом ответа.
  • Подтверждение может появиться в mobileconf не сразу — оно повторяется с нарастающей паузой,
    сам лот повторно не создаётся. needauth ретраями не лечится и пробрасывается сразу.
  • Если у предмета уже есть лот, ждущий подтверждения, Steam отвечает success=false с текстом
    об этом — такой лот подтверждается, а не считается ошибкой.
"""

import sys
from pathlib import Path

import pytest

project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

import steampy.market as market_module
from steampy.confirmation import ConfirmationExecutor
from steampy.exceptions import ConfirmationExpected, SessionNeedsAuth
from steampy.market import SteamMarket
from steampy.models import GameOptions


class _Page:
    def __init__(self, text: str, status_code: int = 200):
        self.text = text
        self.status_code = status_code


def _executor_with_getlist(page: _Page) -> ConfirmationExecutor:
    executor = ConfirmationExecutor("aWRlbnRpdHk=", "76561198000000000", session=None)
    executor._fetch_confirmations_page = lambda: page
    return executor


def test_needauth_is_reported_as_session_needs_auth():
    executor = _executor_with_getlist(_Page('{"success":false,"needauth":true}'))

    with pytest.raises(SessionNeedsAuth, match="needauth"):
        executor._get_confirmations()


def test_session_needs_auth_is_still_a_confirmation_expected():
    """Старые обработчики except ConfirmationExpected его тоже ловят."""
    assert issubclass(SessionNeedsAuth, ConfirmationExpected)


def test_http_error_names_the_status():
    executor = _executor_with_getlist(_Page("", status_code=502))

    with pytest.raises(ConfirmationExpected, match="HTTP 502"):
        executor._get_confirmations()


def test_non_json_body_is_quoted_in_the_error():
    executor = _executor_with_getlist(_Page("<html>Access Denied</html>"))

    with pytest.raises(ConfirmationExpected, match="Access Denied"):
        executor._get_confirmations()


def test_json_without_conf_is_quoted_in_the_error():
    executor = _executor_with_getlist(_Page('{"success":false}'))

    with pytest.raises(ConfirmationExpected, match="conf"):
        executor._get_confirmations()


def test_confirmations_still_parsed():
    executor = _executor_with_getlist(
        _Page('{"success":true,"conf":[{"id":"11","nonce":"22","creator_id":"33"}]}')
    )

    confirmations = executor._get_confirmations()

    assert [(c.data_confid, c.nonce, c.creator_id) for c in confirmations] == [("11", "22", "33")]


class _SellResponse:
    def __init__(self, payload: dict):
        self._payload = payload
        self.status_code = 200
        self.text = ""

    def json(self):
        return self._payload


class _SellSession:
    def __init__(self, payload: dict):
        self.payload = payload

    def post(self, url, data=None, headers=None):
        return _SellResponse(self.payload)


@pytest.fixture
def sleeps(monkeypatch):
    calls = []
    monkeypatch.setattr(market_module.time, "sleep", lambda seconds: calls.append(seconds))
    return calls


def _market(sell_payload: dict, confirm_results: list) -> SteamMarket:
    """confirm_results — что по очереди «возвращает» подтверждение: dict или исключение."""
    market = SteamMarket(_SellSession(sell_payload))
    market._set_login_executed({"steamid": "76561198000000000", "identity_secret": "aWRlbnRpdHk="}, "sid")
    market.confirm_calls = 0

    def fake_confirm(asset_id):
        result = confirm_results[market.confirm_calls]
        market.confirm_calls += 1
        if isinstance(result, Exception):
            raise result
        return result

    market._confirm_sell_listing = fake_confirm
    return market


@pytest.mark.parametrize(
    "message",
    [
        "You already have a listing for this item pending confirmation. "
        "Please confirm or cancel the existing listing.",
        "Лот на этот предмет уже ожидает вашего согласия.",
    ],
    ids=["en", "ru"],
)
def test_listing_pending_confirmation_gets_confirmed(sleeps, message):
    market = _market({"success": False, "message": message}, [{"success": True}])

    result = market.create_sell_order("123", GameOptions.CS, "100")

    assert result == {"success": True}
    assert market.confirm_calls == 1


def test_needs_mobile_confirmation_gets_confirmed(sleeps):
    market = _market({"success": True, "needs_mobile_confirmation": True}, [{"success": True}])

    assert market.create_sell_order("123", GameOptions.CS, "100") == {"success": True}


def test_other_failure_is_returned_as_is(sleeps):
    payload = {"success": False, "message": "The price entered plus fees exceeds the maximum allowed."}
    market = _market(payload, [])

    assert market.create_sell_order("123", GameOptions.CS, "100") == payload
    assert market.confirm_calls == 0


def test_confirmation_retried_until_it_appears(sleeps):
    market = _market(
        {"success": True, "needs_mobile_confirmation": True},
        [ConfirmationExpected(), ConfirmationExpected(), {"success": True}],
    )

    assert market.create_sell_order("123", GameOptions.CS, "100") == {"success": True}
    assert market.confirm_calls == 3
    assert sleeps == [3.0, 6.0]


def test_confirmation_gives_up_after_all_attempts(sleeps):
    market = _market({"success": True, "needs_mobile_confirmation": True}, [ConfirmationExpected("nope")] * 6)

    with pytest.raises(ConfirmationExpected, match="nope"):
        market.create_sell_order("123", GameOptions.CS, "100")
    assert market.confirm_calls == 6
    assert sleeps == [3.0, 6.0, 9.0, 12.0, 15.0]


def test_needauth_is_not_retried(sleeps):
    market = _market({"success": True, "needs_mobile_confirmation": True}, [SessionNeedsAuth("needauth")])

    with pytest.raises(SessionNeedsAuth):
        market.create_sell_order("123", GameOptions.CS, "100")
    assert market.confirm_calls == 1
    assert sleeps == []
