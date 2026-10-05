class SevenDaysHoldException(Exception):
    pass


class TooManyRequests(Exception):
    pass


class ApiException(Exception):
    pass


class LoginRequired(Exception):
    pass


class InvalidCredentials(Exception):
    pass


class CaptchaRequired(Exception):
    pass


class ConfirmationExpected(Exception):
    pass


class SessionNeedsAuth(ConfirmationExpected):
    """Steam-сессия не авторизована для эндпоинта мобильных подтверждений:
    mobileconf/getlist вернул {"success":false,"needauth":true}.

    Наследуется от ConfirmationExpected ради обратной совместимости с существующими
    обработчиками, НО подтверждение не восстановится ретраями — нужен новый вход
    в аккаунт.
    """
    pass
