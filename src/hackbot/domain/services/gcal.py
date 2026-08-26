"""Google Calendar transport: service-account auth plus the raw REST calls.

`google-api-python-client` is synchronous and would block the bot's event loop,
so the only thing borrowed from Google's libraries is the JWT signing; the rest
is plain aiohttp against Calendar API v3, mirroring `github.py`.

Auth is the two-legged service-account flow: a self-signed assertion is traded
for a real OAuth2 access token. `google.auth.jwt.Credentials` looks like a
shortcut but does not work here - Calendar rejects self-signed JWTs and insists
on a token minted by the token endpoint.

The module stays inert while the integration is unconfigured: without a
credentials file or a calendar id the write helpers quietly do nothing, so a bot
without a calendar behaves exactly as it did before.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import quote

import aiohttp

from hackbot.config import get_settings

try:  # google-auth is only needed once the calendar is actually configured
    from google.auth import crypt as _crypt
    from google.auth import jwt as _jwt
except ImportError:  # pragma: no cover - reported lazily, see _assertion()
    _crypt = None  # type: ignore[assignment]
    _jwt = None  # type: ignore[assignment]

log = logging.getLogger(__name__)

API = "https://www.googleapis.com/calendar/v3"
TOKEN_URI = "https://oauth2.googleapis.com/token"
SCOPE = "https://www.googleapis.com/auth/calendar"
GRANT_TYPE = "urn:ietf:params:oauth:grant-type:jwt-bearer"

# Not a status Google ever sends: what it marks is a request that never got an
# answer. Callers guard themselves with `except GCalError` alone, so network
# trouble has to arrive wearing the same clothes as a refusal - see
# _transport_error() for what it costs when it does not.
TRANSPORT_STATUS = 504
# Ours, never Google's: something on this machine is wrong - a key file that is
# missing, truncated or not JSON. Telling the operator to retry later would be a
# lie, so it has to be tellable apart from a status Google actually returned.
CONFIG_STATUS = 599

_TIMEOUT = aiohttp.ClientTimeout(total=45)
_ASSERTION_TTL = 3600  # the longest lifetime Google accepts
_TOKEN_SKEW = 60  # refresh this early so a request never races the expiry
_PAGE_SIZE = 250
_MAX_PAGES = 40  # a stuck nextPageToken must not turn listing into a loop

_account_stamp: tuple[int, int] | None = None
_token: str | None = None
_token_expires: float = 0.0
_token_lock = asyncio.Lock()


class GCalError(RuntimeError):
    """Any non-success response, carrying the message Google actually returned."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"Google Calendar {status}: {message}")
        self.status = status
        self.message = message


def enabled() -> bool:
    return get_settings().google_calendar_enabled


def _transport_error(exc: BaseException) -> GCalError:
    """Turn a request that never got an answer into an ordinary calendar error.

    A dropped connection is the likeliest failure this module has, and every
    caller catches GCalError and nothing else. Left as itself, a TimeoutError
    walks through the per-event guard in calsync and costs the whole sync pass
    instead of the one stage it belongs to; from the agent tool it escapes even
    further and kills the run. The type name is kept because it is the only clue
    to which kind of trouble it was, and it says nothing sensitive.
    """
    return GCalError(TRANSPORT_STATUS, f"сеть до Google недоступна ({type(exc).__name__})")


@lru_cache(maxsize=1)
def _load_account(path: Path, stamp: tuple[int, int]) -> dict[str, Any]:
    """Parse the key file once per version of it.

    The dict holds the private key, so it must never reach a log line or an
    exception message - only the reason a read failed does. `from None` matters
    here: JSONDecodeError carries the whole document it choked on, and a chained
    traceback would print the private key.

    `stamp` is the file's mtime and size. It is in the signature purely to be part
    of the cache key: a rotated key lands at the same path, so caching on the path
    alone would keep signing with the revoked one until someone restarted.
    """
    try:
        info = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError) as exc:
        raise GCalError(
            CONFIG_STATUS, f"файл сервис-аккаунта не читается ({type(exc).__name__})"
        ) from None
    if not isinstance(info, dict) or not info.get("client_email") or not info.get("private_key"):
        raise GCalError(CONFIG_STATUS, "в файле сервис-аккаунта нет client_email или private_key")
    return info


def _service_account() -> dict[str, Any] | None:
    """Parsed service-account JSON, or None when the file is simply absent.

    The stat sits outside the cache on purpose, and it does double duty. It lets a
    key copied onto a running server start working without a restart - memoising
    "no such file" would hand that server a permanent 401 instead - and it makes a
    key *replaced* in place take effect too, which is exactly what an operator
    following the rotation steps in docs/google-calendar.md does.
    """
    global _account_stamp
    path = get_settings().abs_google_credentials_file
    try:
        stat = path.stat()
    except OSError:
        return None
    stamp = (stat.st_mtime_ns, stat.st_size)
    if _account_stamp is not None and _account_stamp != stamp:
        # The cached token was minted with the key that just went away, so it is
        # worthless now and would keep failing for the rest of its hour.
        _invalidate_token()
    _account_stamp = stamp
    return _load_account(path, stamp)


def account_email() -> str | None:
    """The address the user has to share their calendar with."""
    try:
        info = _service_account()
    except GCalError as exc:
        log.warning("service account file unusable: %s", exc.message)
        return None
    return info.get("client_email") if info else None


def _assertion(info: dict[str, Any]) -> str:
    if _jwt is None or _crypt is None:
        raise GCalError(500, "google-auth не установлен")
    now = int(time.time())
    audience = info.get("token_uri") or TOKEN_URI
    payload = {
        "iss": info["client_email"],
        "scope": SCOPE,
        "aud": audience,
        "iat": now,
        "exp": now + _ASSERTION_TTL,
    }
    signer = _crypt.RSASigner.from_service_account_info(info)
    return _jwt.encode(signer, payload).decode("ascii")


async def _fetch_token() -> tuple[str, int]:
    info = _service_account()
    if info is None:
        raise GCalError(401, "файл сервис-аккаунта не найден")
    form = {"grant_type": GRANT_TYPE, "assertion": _assertion(info)}
    try:
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
            async with session.post(info.get("token_uri") or TOKEN_URI, data=form) as resp:
                body = await resp.text()
                status = resp.status
    except (TimeoutError, aiohttp.ClientError) as exc:
        # `from None`: the chained traceback would carry the request that was
        # being signed, and the assertion in it is minted from the private key.
        raise _transport_error(exc) from None
    if status != 200:
        raise GCalError(status, _error_message(body))
    payload = _decode(body)
    token = payload.get("access_token")
    if not token:
        raise GCalError(500, "Google не вернул access_token")
    return str(token), int(payload.get("expires_in") or _ASSERTION_TTL)


async def _access_token() -> str:
    """Cached bearer token; concurrent callers share a single refresh."""
    global _token, _token_expires
    if _token and time.monotonic() < _token_expires:
        return _token
    async with _token_lock:
        # Someone else may have refreshed it while we waited for the lock.
        if _token and time.monotonic() < _token_expires:
            return _token
        token, ttl = await _fetch_token()
        _token = token
        _token_expires = time.monotonic() + max(ttl - _TOKEN_SKEW, 0)
        log.info("google calendar token refreshed, valid for %ss", ttl)
        return token


def _invalidate_token() -> None:
    global _token, _token_expires
    _token = None
    _token_expires = 0.0


def _decode(body: str) -> dict[str, Any]:
    if not body:
        return {}
    try:
        payload = json.loads(body)
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _error_message(body: str) -> str:
    """Google speaks two dialects here: `error.message` from the API, and
    `error_description` from the token endpoint. Raw body is the last resort."""
    payload = _decode(body)
    error = payload.get("error")
    if isinstance(error, dict) and error.get("message"):
        return str(error["message"])
    if payload.get("error_description"):
        return str(payload["error_description"])
    if isinstance(error, str) and error:
        return error
    return body[:200] or "пустой ответ"


def _calendar() -> str:
    """Calendar ids are e-mail-shaped, so the `@` has to survive as a path segment."""
    return quote(get_settings().google_calendar_id, safe="")


def _events_path() -> str:
    return f"/calendars/{_calendar()}/events"


async def _request(
    method: str,
    path: str,
    *,
    json_body: Any = None,
    params: dict[str, str] | None = None,
    ok: tuple[int, ...] = (200, 201, 204),
    retry_auth: bool = True,
) -> dict[str, Any]:
    token = await _access_token()
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    url = path if path.startswith("http") else f"{API}{path}"
    try:
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
            async with session.request(
                method, url, headers=headers, json=json_body, params=params
            ) as resp:
                body = await resp.text()
                status = resp.status
    except (TimeoutError, aiohttp.ClientError) as exc:
        raise _transport_error(exc) from None
    if status in ok:
        return _decode(body)
    if status == 401 and retry_auth:
        # Clock skew or a revoked token: drop the cache and give it exactly one more go.
        _invalidate_token()
        return await _request(
            method, path, json_body=json_body, params=params, ok=ok, retry_auth=False
        )
    raise GCalError(status, _error_message(body))


async def check() -> str:
    """Read the calendar back and return its title.

    Unlike the write helpers this one is a diagnostic an admin explicitly asked
    for, so an unconfigured integration is an error worth reporting rather than
    a silent empty answer.
    """
    if not enabled():
        raise GCalError(400, "календарь не настроен")
    payload = await _request("GET", f"/calendars/{_calendar()}")
    return str(payload.get("summary") or get_settings().google_calendar_id)


async def upsert_event(gid: str, body: dict) -> None:
    """Write one event under our own deterministic id.

    PUT goes first because in the steady state the event already exists, which
    makes the common path a single request. 404 means it was never created; 409
    means the id is still held by the tombstone of a deleted event, and only a
    PUT (with status=confirmed in the body) brings that one back to life.
    """
    if not enabled():
        return
    path = f"{_events_path()}/{quote(gid, safe='')}"
    try:
        await _request("PUT", path, json_body=body)
        return
    except GCalError as exc:
        if exc.status != 404:
            raise
    try:
        await _request("POST", _events_path(), json_body={**body, "id": gid})
        return
    except GCalError as exc:
        if exc.status != 409:
            raise
    await _request("PUT", path, json_body=body)


async def delete_event(gid: str) -> None:
    """Remove an event. Already gone (404) or already purged (410) counts as done."""
    if not enabled():
        return
    try:
        await _request("DELETE", f"{_events_path()}/{quote(gid, safe='')}")
    except GCalError as exc:
        if exc.status not in (404, 410):
            raise


async def list_event_ids(hack_id: int) -> set[str]:
    """Ids of everything this hackathon owns in the shared calendar.

    Paging is not optional: a long timeline overflows one page, and a partial
    answer would make the orphan cleanup drop events that are still current.
    """
    if not enabled():
        return set()
    params = {
        "privateExtendedProperty": f"hackbot_hack={hack_id}",
        "showDeleted": "false",
        "maxResults": str(_PAGE_SIZE),
        "fields": "items(id),nextPageToken",
    }
    found: set[str] = set()
    page_token: str | None = None
    for _ in range(_MAX_PAGES):
        query = dict(params) if page_token is None else {**params, "pageToken": page_token}
        payload = await _request("GET", _events_path(), params=query)
        for item in payload.get("items") or []:
            item_id = item.get("id") if isinstance(item, dict) else None
            if item_id:
                found.add(str(item_id))
        next_token = payload.get("nextPageToken")
        if not next_token or next_token == page_token:
            return found
        page_token = str(next_token)
    log.warning("calendar listing for hack %s stopped after %s pages", hack_id, _MAX_PAGES)
    return found
