"""
WebSocket клиент для связи с AutoBase сервером.
"""
import asyncio
import json
import logging
import ssl
from decimal import Decimal
from typing import Callable, Optional, List

import certifi
import websockets
from websockets.client import WebSocketClientProtocol

# Класс отказа handshake: в websockets 12.x это InvalidStatusCode (есть .status_code),
# в 13+ его может не быть. Подстрахуемся через InvalidHandshake (общий родитель),
# чтобы except-клауза не падала с AttributeError при обновлении библиотеки.
_HandshakeRejected = getattr(
    websockets.exceptions, "InvalidStatusCode", websockets.exceptions.InvalidHandshake
)


# tasbots.com стоит за Cloudflare с включённой бот-защитой (Browser Integrity Check /
# managed rules), которая банит "пустые"/библиотечные User-Agent (Cloudflare error 1010)
# и придирчивее к IP дата-центров. Чтобы агент не спотыкался о наивные UA-правила,
# представляемся обычным браузером. Это НЕ заменяет allowlist IP сервера в Cloudflare,
# но снимает UA-зависимую часть блокировки.
_CLIENT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def _extract_handshake_status(exc) -> tuple:
    """Достаёт (status_code, headers) из исключения отказа handshake (12.x и 13+)."""
    status = getattr(exc, "status_code", None)
    headers = getattr(exc, "headers", None)
    if status is None or headers is None:
        resp = getattr(exc, "response", None)  # websockets 13+
        if status is None:
            status = getattr(resp, "status_code", None)
        if headers is None:
            headers = getattr(resp, "headers", None)
    return status, headers


class DecimalEncoder(json.JSONEncoder):
    """JSON энкодер с поддержкой Decimal"""
    def default(self, obj):
        if isinstance(obj, Decimal):
            return str(obj)
        return super(DecimalEncoder, self).default(obj)


class WebSocketClient:
    """WebSocket клиент агента."""

    def __init__(
            self,
            server_url: str,
            agent_token: str,
            on_command_callback: Callable,
            on_status_change_callback: Callable
    ):
        self.server_url = server_url
        self.agent_token = agent_token
        self.on_command_callback = on_command_callback
        self.on_status_change_callback = on_status_change_callback

        self.websocket: Optional[WebSocketClientProtocol] = None
        self.is_running = False
        self.logger = logging.getLogger("WebSocketClient")
        self._backoff_seconds = 2
        self._max_backoff_seconds = 60
        self._heartbeat_task: Optional[asyncio.Task] = None
        # Интервал прикладного heartbeat и порог "молчания" сервера для watchdog.
        # Если сервер не присылает НИЧЕГО (ни pong, ни ack, ни команд) дольше
        # _server_silence_timeout — соединение считаем наполовину мёртвым (half-open TCP)
        # и принудительно переподключаемся. Именно этот кейс выглядит как
        # "агент подключён, но пакеты не улетают".
        self._heartbeat_interval = 15
        self._server_silence_timeout = 45
        self._last_server_msg_at = 0.0

    async def _heartbeat_loop(self) -> None:
        """Фоновая отправка 'ping' сообщений чтобы соединение между агентом и сервером не разрывалось в простое."""
        loop = asyncio.get_event_loop()
        while self.is_running and self.websocket:
            try:
                await self.websocket.send(json.dumps({"type": "ping"}))
                self.logger.info("Heartbeat ping sent")
                await asyncio.sleep(self._heartbeat_interval)

                # Watchdog half-open соединения.
                # При ping_interval=None библиотека не детектит мёртвый сокет:
                # recv() висит вечно, а send() на half-open TCP какое-то время
                # "успешно" пишет в буфер. Сервер на каждый ping отвечает pong,
                # поэтому при живом канале _last_server_msg_at обновляется ~раз в 15с.
                # Если сервер молчит дольше порога — рвём соединение, чтобы recv()
                # в _listen_loop получил ConnectionClosed и сработал reconnect.
                silence = loop.time() - self._last_server_msg_at
                if silence > self._server_silence_timeout:
                    self.logger.warning(
                        f"Watchdog: сервер молчит {silence:.0f}s "
                        f"(порог {self._server_silence_timeout}s) — соединение считаем мёртвым, переподключаемся"
                    )
                    self.on_status_change_callback(False)
                    ws = self.websocket
                    if ws is not None:
                        await ws.close(code=4000, reason="server silence watchdog")
                    break
            except asyncio.CancelledError:
                break
            except websockets.exceptions.ConnectionClosed as e:
                self.logger.warning(
                    "Heartbeat: connection closed "
                    f"(type={type(e).__name__}, code={getattr(e, 'code', None)}, reason={getattr(e, 'reason', None)})"
                )
                break
            except Exception as e:
                self.logger.error(f"Heartbeat error: {type(e).__name__}: {e}", exc_info=True)
                break

    async def _stop_heartbeat(self) -> None:
        """Остановить heartbeat задачу без утечек pending task."""
        if self._heartbeat_task is None:
            return
        if not self._heartbeat_task.done():
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
        self._heartbeat_task = None

    async def connect(self, manifest: List[str]) -> None:
        """
        Подключиться к серверу и отправить манифест.
        Manifest: список логинов, которые обслуживает этот агент.
        """
        self.is_running = True
        ws_url = self._build_ws_url()
        headers = {
            "Authorization": self.agent_token,
            "User-Agent": _CLIENT_USER_AGENT,
            "Origin": "https://tasbots.com",
        }
        ssl_context = ssl.create_default_context(cafile=certifi.where())
        backoff = self._backoff_seconds

        while self.is_running:
            try:
                self.logger.info(f"Попытка подключения к {ws_url}")
                async with websockets.connect(
                        ws_url,
                        extra_headers=headers,
                        # Используем только прикладной heartbeat {"type":"ping"}.
                        # Встроенные control ping/pong у websockets на некоторых
                        # Windows Server + proxy/Nginx окружениях приводят к 1006.
                        ping_interval=None,
                        ping_timeout=None,
                        open_timeout=15,
                        close_timeout=10,
                        ssl=ssl_context
                ) as websocket:
                    self.websocket = websocket
                    # Стартовое значение для watchdog: только что открылись — сервер "не молчит".
                    self._last_server_msg_at = asyncio.get_event_loop().time()
                    self.on_status_change_callback(True)
                    self.logger.info(f"Подключено к {ws_url}")

                    # Отправляем манифест сразу после подключения
                    manifest_msg = {
                        "type": "manifest",
                        "logins": manifest
                    }
                    await websocket.send(json.dumps(manifest_msg))
                    self.logger.info(f"Манифест отправлен: {len(manifest)} логинов: {manifest}")

                    self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
                    backoff = self._backoff_seconds

                    # Слушаем команды
                    await self._listen_loop()

            except _HandshakeRejected as e:
                # Сервер/прокси отклонил WebSocket-handshake до апгрейда (вернул не 101).
                # Самый частый кейс на арендованном сервере: Cloudflare отдаёт 403/429
                # для IP дата-центра, тогда как с домашнего IP всё проходит.
                status, resp_headers = _extract_handshake_status(e)
                cf_ray = None
                try:
                    if resp_headers is not None:
                        cf_ray = resp_headers.get("cf-ray") or resp_headers.get("CF-RAY")
                except Exception:
                    cf_ray = None
                hint = ""
                if status in (403, 429, 503):
                    hint = (
                        " ВЕРОЯТНО Cloudflare режет IP этого сервера "
                        "(allowlist IP / WAF-skip для /ws/ и /ingestion/ на стороне Cloudflare)."
                    )
                self.logger.error(
                    f"WebSocket handshake ОТКЛОНЁН сервером: HTTP {status} (cf-ray={cf_ray}).{hint}"
                )
                self.on_status_change_callback(False)
            except websockets.exceptions.WebSocketException as e:
                self.logger.error(f"WebSocket ошибка: {type(e).__name__}: {e}", exc_info=True)
                self.on_status_change_callback(False)
            except OSError as e:
                # ssl.SSLCertVerificationError — подкласс OSError. Чаще всего на свежем
                # сервере это сбитые часы/таймзона: certifi-сертификат "ещё не валиден".
                extra = ""
                err_text = str(e)
                if "CERTIFICATE_VERIFY_FAILED" in err_text or "not yet valid" in err_text:
                    extra = " Проверь системные часы/таймзону сервера — при сбитом времени TLS-проверка падает."
                self.logger.error(
                    f"Сетевая ошибка сокета: {type(e).__name__}: {e}.{extra}", exc_info=True
                )
                self.on_status_change_callback(False)
            except Exception as e:
                self.logger.error(f"Неожиданная ошибка: {type(e).__name__}: {e}", exc_info=True)
                self.on_status_change_callback(False)
            finally:
                await self._stop_heartbeat()
                self.websocket = None

            if self.is_running:
                self.logger.info(f"Переподключение через {backoff} сек")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, self._max_backoff_seconds)

        self.websocket = None

    async def _listen_loop(self) -> None:
        """Цикл прослушивания команд от сервера."""
        while self.is_running and self.websocket:
            try:
                message = await self.websocket.recv()
                # Любое сообщение от сервера (pong/ack/команда) = канал живой → сбрасываем watchdog.
                self._last_server_msg_at = asyncio.get_event_loop().time()
                command = json.loads(message)
                msg_type = command.get("type")

                if msg_type == "ack":
                    ack_request_id = command.get("request_id")
                    self.logger.info(f"ACK received from server (request_id={ack_request_id})")
                    continue

                if msg_type == "pong":
                    self.logger.info("Heartbeat pong received from server")
                    continue

                # Извлекаем request_id из команды для ответа
                request_id = command.get("request_id")
                cmd_type = command.get("cmd")
                login = command.get("account_login")

                self.logger.info(f"Получена команда: {cmd_type} для {login} (request_id={request_id})")

                # Передаем команду в обработчик
                response = await self.on_command_callback(command)

                # Добавляем request_id в ответ, если его нет
                if "request_id" not in response:
                    response["request_id"] = request_id

                # Отправляем ответ серверу (с поддержкой Decimal через DecimalEncoder)
                self.logger.info(
                    "Sending response to server "
                    f"(cmd_type={cmd_type}, login={login}, request_id={request_id}, response_status={response.get('status')})"
                )
                try:
                    await self.websocket.send(json.dumps(response, cls=DecimalEncoder))
                except websockets.exceptions.ConnectionClosed as e:
                    self.logger.error(
                        "Send failed: connection closed "
                        f"(type={type(e).__name__}, code={getattr(e, 'code', None)}, reason={getattr(e, 'reason', None)}) "
                        f"for request_id={request_id}, cmd_type={cmd_type}, login={login}"
                    )
                    self.on_status_change_callback(False)
                    break
                except Exception as e:
                    self.logger.error(
                        f"Send failed for request_id={request_id}, cmd_type={cmd_type}, login={login}: "
                        f"{type(e).__name__}: {e}",
                        exc_info=True
                    )
                    self.on_status_change_callback(False)
                    break
                self.logger.info(f"Response sent to server (request_id={request_id})")

            except websockets.exceptions.ConnectionClosed as e:
                self.logger.warning(
                    "Connection closed while waiting for server message "
                    f"(type={type(e).__name__}, code={getattr(e, 'code', None)}, reason={getattr(e, 'reason', None)})"
                )
                self.on_status_change_callback(False)
                break
            except Exception as e:
                self.logger.error(f"Ошибка обработки команды: {type(e).__name__}: {e}", exc_info=True)

    async def disconnect(self) -> None:
        """Отключиться от сервера."""
        self.is_running = False
        await self._stop_heartbeat()
        if self.websocket:
            try:
                await self.websocket.close()
            except Exception as e:
                self.logger.error(f"Error during websocket close: {type(e).__name__}: {e}", exc_info=True)
            self.websocket = None
        self.on_status_change_callback(False)

    def _build_ws_url(self) -> str:
        """Собрать полный ws/wss URL для подключения агента."""
        # Формируем правильный WebSocket URL: ws://server_ip/ws/{token}
        # Если server_url уже содержит ws:// или wss://, используем как есть
        # Иначе добавляем ws://
        if not self.server_url.startswith(("ws://", "wss://")):
            # Если server_url содержит http:// или https://, заменяем на ws:// или wss://
            if self.server_url.startswith("https://"):
                ws_url = self.server_url.replace("https://", "wss://", 1)
            elif self.server_url.startswith("http://"):
                ws_url = self.server_url.replace("http://", "ws://", 1)
            else:
                ws_url = f"ws://{self.server_url}"
        else:
            ws_url = self.server_url

        # Добавляем путь /ws/{token}
        if not ws_url.endswith("/"):
            return f"{ws_url}/ws/{self.agent_token}"
        return f"{ws_url}ws/{self.agent_token}"
