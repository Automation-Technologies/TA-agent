"""
Клиент Smart Ingestion для связи с AgentGateway (steam_bot).

Задачи:
- CHECK_EXISTENCE: узнать, какие логины уже есть в steam_accounts
- REGISTER: зарегистрировать новые аккаунты с балансом
"""

import ssl
from typing import List, Dict, Any

import aiohttp
import certifi


class IngestionClient:
    """HTTP‑клиент для Smart Ingestion."""

    _HTTP_TOTAL_TIMEOUT_SECONDS = 120
    _HTTP_CONNECT_TIMEOUT_SECONDS = 30
    # Cloudflare на tasbots.com банит библиотечные User-Agent (error 1010).
    # Представляемся браузером, как и WebSocket-клиент агента.
    _USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )

    def __init__(self, base_url: str, agent_token: str) -> None:
        self._base_url = base_url.rstrip("/")
        self._agent_token = agent_token
        self._ssl_context = ssl.create_default_context(cafile=certifi.where())
        self._timeout = aiohttp.ClientTimeout(
            total=self._HTTP_TOTAL_TIMEOUT_SECONDS,
            connect=self._HTTP_CONNECT_TIMEOUT_SECONDS,
        )
        self._headers = {"User-Agent": self._USER_AGENT}

    async def check_existence(self, accounts: List[Dict[str, str]]) -> Dict[str, Any]:
        """
        Проверка существования аккаунтов.

        Request:
            POST {base_url}/ingestion/check
            {
              "token": "<agent_token>",
              "accounts": [{"login": "..."}]
            }
        Response:
            {
              "existing": ["login1", ...],
              "new": ["login2", ...]
            }
        """
        url = f"{self._base_url}/ingestion/check"
        payload = {
            "token": self._agent_token,
            "accounts": accounts,
        }

        async with aiohttp.ClientSession(timeout=self._timeout, headers=self._headers) as session:
            async with session.post(url, json=payload, ssl=self._ssl_context) as resp:
                if resp.status == 401:
                    raise aiohttp.ClientResponseError(
                        request_info=resp.request_info,
                        history=resp.history,
                        status=401,
                        message="Unauthorized"
                    )
                data = await resp.json()
                return data

    async def register_accounts(self, accounts: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Регистрация новых аккаунтов в steam_accounts.

        Request:
            POST {base_url}/ingestion/register
            {
              "token": "<agent_token>",
              "accounts": [
                {
                  "login": "...",
                  "balance": 0.0,
                  "currency": "RUB"
                }
              ]
            }
        Response:
            {
              "created": ["login1", ...],
              "skipped": ["login2", ...]
            }
        """
        url = f"{self._base_url}/ingestion/register"
        payload = {
            "token": self._agent_token,
            "accounts": accounts,
        }

        async with aiohttp.ClientSession(timeout=self._timeout, headers=self._headers) as session:
            async with session.post(url, json=payload, ssl=self._ssl_context) as resp:
                if resp.status == 401:
                    raise aiohttp.ClientResponseError(
                        request_info=resp.request_info,
                        history=resp.history,
                        status=401,
                        message="Unauthorized"
                    )
                data = await resp.json()
                return data


