"""
Кривой ответ инвентаря — внятная ApiException, а не TypeError/KeyError на разборе.

Под rate-limit Steam отдаёт на /inventory пустое тело или 'null'. Пустое тело агент уже
разбирал в понятную ошибку, а 'null' превращался в None и падал TypeError на ['success'] —
и такой текст ошибки уходил серверу.
"""

import json
import sys
from pathlib import Path

import pytest

project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

from steampy.client import SteamClient
from steampy.exceptions import ApiException
from steampy.models import GameOptions


class _Resp:
    def __init__(self, text: str, status_code: int = 200):
        self.text = text
        self.status_code = status_code

    def json(self):
        return json.loads(self.text)


class _Session:
    def __init__(self, resp: _Resp):
        self.resp = resp

    def get(self, url, params=None, **kwargs):
        return self.resp


def _client(text: str, status_code: int = 200) -> SteamClient:
    client = SteamClient("api-key")
    client._session = _Session(_Resp(text, status_code))
    client.was_login_executed = True
    return client


@pytest.mark.parametrize("body", ["null", "[]"])
def test_body_that_is_not_an_object_gives_api_exception(body):
    with pytest.raises(ApiException, match="не объект.*status=429"):
        _client(body, status_code=429).get_partner_inventory("76561198000000000", GameOptions.CS)


def test_non_json_body_gives_api_exception():
    with pytest.raises(ApiException, match="не-JSON"):
        _client("", status_code=502).get_partner_inventory("76561198000000000", GameOptions.CS)


def test_object_without_success_gives_api_exception():
    with pytest.raises(ApiException, match="Success value should be 1"):
        _client('{"error": "busy"}').get_partner_inventory("76561198000000000", GameOptions.CS)


def test_empty_inventory_is_still_a_valid_answer():
    assert _client('{"success": 1, "total_inventory_count": 0}').get_partner_inventory(
        "76561198000000000", GameOptions.CS) == {}
