"""The transport half of the shared Google calendar: auth, upsert, cleanup.

One calendar holds every hackathon, so the only thing keeping two entries apart
is the deterministic event id - which makes the PUT/POST/PUT dance the part most
worth pinning down. Nothing here goes near the network: aiohttp is swapped out
wholesale and the service account is a throwaway key generated in this process,
so the real JSON in `data/` is never opened and never printed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlencode

import aiohttp
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from hackbot.config import get_settings
from hackbot.domain.services import gcal

ACCOUNT_EMAIL = "hackbot@hackbot-test.iam.gserviceaccount.com"
CALENDAR_ID = "hackbot@group.calendar.google.com"
TOKEN_HOST = "oauth2.googleapis.com"
GID = "hb7e42"
BODY = {"id": GID, "summary": "Защита · ТендерХак", "status": "confirmed"}


# ---------------------------------------------------------------- aiohttp stand-in


@dataclass(slots=True)
class Call:
    method: str
    url: str
    payload: Any = None
    params: dict[str, Any] = field(default_factory=dict)

    @property
    def query(self) -> str:
        """URL plus params, decoded, so a test does not care which one was used."""
        joined = f"{self.url}?{urlencode(self.params)}" if self.params else self.url
        return unquote(joined)


class _Reply:
    """Stands in both for the request context manager and for the response."""

    def __init__(self, status: int, payload: Any) -> None:
        self.status = status
        self.ok = status < 400
        self._payload = payload

    async def json(self, **_: Any) -> Any:
        return self._payload

    async def text(self) -> str:
        return json.dumps(self._payload, ensure_ascii=False)

    # `async with session.request(...)` and `await session.get(...)` are both
    # legal aiohttp; supporting both keeps the fake out of the client's design.
    def __await__(self):
        async def ready() -> _Reply:
            return self

        return ready().__await__()

    async def __aenter__(self) -> _Reply:
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        return False


class _Session:
    def __init__(self, http: FakeHttp) -> None:
        self._http = http

    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        return False

    async def close(self) -> None:
        return None

    def request(self, method: str, url: str, **kwargs: Any) -> _Reply:
        return self._http.handle(method, url, kwargs)

    def get(self, url: str, **kwargs: Any) -> _Reply:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> _Reply:
        return self.request("POST", url, **kwargs)

    def put(self, url: str, **kwargs: Any) -> _Reply:
        return self.request("PUT", url, **kwargs)

    def delete(self, url: str, **kwargs: Any) -> _Reply:
        return self.request("DELETE", url, **kwargs)


class FakeHttp:
    """Answers calendar calls from a script; the token exchange always works.

    Token traffic is served but not recorded: whether the client still holds a
    cached access token is not something a transport test should depend on.
    """

    def __init__(self, *replies: tuple[int, Any] | BaseException) -> None:
        self._replies = list(replies)
        self.calls: list[Call] = []

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(aiohttp, "ClientSession", lambda *a, **kw: _Session(self))

    @property
    def methods(self) -> list[str]:
        return [call.method for call in self.calls]

    def handle(self, method: str, url: str, kwargs: dict[str, Any]) -> _Reply:
        if TOKEN_HOST in url:
            return _Reply(200, {"access_token": "test-token", "expires_in": 3600})
        self.calls.append(
            Call(method.upper(), url, kwargs.get("json"), dict(kwargs.get("params") or {}))
        )
        if not self._replies:
            raise AssertionError(f"незапланированный запрос: {method} {url}")
        reply = self._replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply  # scripted transport failure: no answer ever arrives
        return _Reply(*reply)


class _Unreachable:
    """A session that never reaches Google at all: DNS down, peer gone, 45s of nothing.

    Separate from FakeHttp because the token exchange has to fail too, and
    FakeHttp always serves that one successfully.
    """

    def __init__(self, error: BaseException) -> None:
        self._error = error

    def __call__(self, *_a: Any, **_kw: Any) -> _Unreachable:
        return self

    async def __aenter__(self) -> _Unreachable:
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        return False

    def request(self, *_a: Any, **_kw: Any) -> Any:
        raise self._error

    post = request


def forbid_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_a: Any, **_kw: Any) -> None:
        raise AssertionError("выключенная интеграция полезла в сеть")

    monkeypatch.setattr(aiohttp, "ClientSession", boom)


def forget_credentials() -> None:
    """Drop what the module memoised, so a fixture can point at another key file.

    Only caches gcal owns: `get_settings` is memoised too, and clearing that one
    would rebuild Settings from the environment and undo the fixture.
    """
    for value in vars(gcal).values():
        clear = getattr(value, "cache_clear", None)
        if callable(clear) and getattr(value, "__module__", None) == gcal.__name__:
            clear()
    gcal._account_stamp = None


# ---------------------------------------------------------------- fixtures


@pytest.fixture(scope="session")
def service_account(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A throwaway key: the JWT is really signed, the real account is untouched."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("ascii")
    path = tmp_path_factory.mktemp("gcal") / "service-account.json"
    path.write_text(
        json.dumps(
            {
                "type": "service_account",
                "project_id": "hackbot-test",
                "private_key_id": "test-key-id",
                "private_key": pem,
                "client_email": ACCOUNT_EMAIL,
                "token_uri": f"https://{TOKEN_HOST}/token",
            }
        ),
        encoding="utf-8",
    )
    return path


@pytest.fixture
def calendar(monkeypatch: pytest.MonkeyPatch, service_account: Path):
    settings = get_settings()
    monkeypatch.setattr(settings, "google_credentials_file", service_account)
    monkeypatch.setattr(settings, "google_calendar_id", CALENDAR_ID)
    forget_credentials()
    yield settings
    forget_credentials()


# ---------------------------------------------------------------- upsert


async def test_an_existing_event_is_updated_by_a_single_put(calendar, monkeypatch) -> None:
    """The steady state: the event is already there, so one request is enough."""
    http = FakeHttp((200, {"id": GID}))
    http.install(monkeypatch)

    await gcal.upsert_event(GID, BODY)

    assert http.methods == ["PUT"]
    assert GID in http.calls[0].url


async def test_a_missing_event_is_created_with_its_own_id(calendar, monkeypatch) -> None:
    http = FakeHttp((404, {"error": {"message": "Not Found"}}), (200, {"id": GID}))
    http.install(monkeypatch)

    await gcal.upsert_event(GID, {"summary": "Защита · ТендерХак"})

    assert http.methods == ["PUT", "POST"]
    created = http.calls[1].payload
    assert created is not None
    assert created.get("id") == GID, "id задаём мы сами, иначе синк перестаёт быть идемпотентным"


async def test_a_tombstoned_event_is_revived_by_a_second_put(calendar, monkeypatch) -> None:
    """A deleted event lingers in Google as `status: cancelled`.

    The PUT then 404s, the POST collides with that tombstone (409), and only the
    second PUT brings the row back. Drop the last step and the event is gone for
    good the moment somebody deletes it by hand.
    """
    http = FakeHttp(
        (404, {"error": {"message": "Not Found"}}),
        (409, {"error": {"message": "The requested identifier already exists."}}),
        (200, {"id": GID}),
    )
    http.install(monkeypatch)

    await gcal.upsert_event(GID, BODY)

    assert http.methods == ["PUT", "POST", "PUT"]
    assert GID in http.calls[-1].url


# ---------------------------------------------------------------- delete


@pytest.mark.parametrize("status", [204, 404, 410])
async def test_deleting_an_already_gone_event_is_not_a_failure(
    calendar, monkeypatch, status: int
) -> None:
    payload: dict[str, Any] = {} if status == 204 else {"error": {"message": "Not Found"}}
    http = FakeHttp((status, payload))
    http.install(monkeypatch)

    assert await gcal.delete_event(GID) is None
    assert http.methods == ["DELETE"]


async def test_a_real_delete_failure_is_reported(calendar, monkeypatch) -> None:
    http = FakeHttp((500, {"error": {"message": "Backend Error"}}))
    http.install(monkeypatch)

    with pytest.raises(gcal.GCalError) as excinfo:
        await gcal.delete_event(GID)

    assert excinfo.value.status == 500


# ---------------------------------------------------------------- listing


async def test_pages_are_stitched_into_one_set(calendar, monkeypatch) -> None:
    http = FakeHttp(
        (200, {"items": [{"id": "hb7e1"}, {"id": "hb7e2"}], "nextPageToken": "page-2"}),
        (200, {"items": [{"id": "hb7e3"}]}),
    )
    http.install(monkeypatch)

    assert await gcal.list_event_ids(7) == {"hb7e1", "hb7e2", "hb7e3"}

    first, second = (call.query for call in http.calls)
    assert "hackbot_hack=7" in first, "в общем календаре чужие хакатоны трогать нельзя"
    assert "pageToken=page-2" in second


# ---------------------------------------------------------------- calendar itself


async def test_check_returns_the_calendar_name(calendar, monkeypatch) -> None:
    http = FakeHttp((200, {"summary": "Хакатоны"}))
    http.install(monkeypatch)

    assert await gcal.check() == "Хакатоны"
    assert http.methods == ["GET"]


async def test_the_calendar_id_is_url_encoded(calendar, monkeypatch) -> None:
    """A raw `@` in a path segment turns the URL into something else entirely."""
    http = FakeHttp((200, {"summary": "Хакатоны"}))
    http.install(monkeypatch)

    await gcal.check()

    assert "%40" in http.calls[0].url


async def test_the_error_carries_googles_own_message(calendar, monkeypatch) -> None:
    """Sharing the calendar is a manual step, so the reason has to survive."""
    http = FakeHttp((403, {"error": {"code": 403, "message": "нет доступа к календарю"}}))
    http.install(monkeypatch)

    with pytest.raises(gcal.GCalError) as excinfo:
        await gcal.check()

    assert excinfo.value.status == 403
    assert excinfo.value.message == "нет доступа к календарю"


# ---------------------------------------------------------------- network trouble


@pytest.mark.parametrize(
    "failure",
    [TimeoutError("45s and nothing"), aiohttp.ClientOSError("connection reset")],
    ids=["timeout", "reset"],
)
async def test_a_dead_network_speaks_the_same_language_as_a_refusal(
    calendar, monkeypatch, failure: BaseException
) -> None:
    """Every caller guards itself with `except GCalError` and nothing else.

    A raw TimeoutError walks straight through that guard: it aborts the sync pass
    it was part of and, from the agent tool, the whole agent run. A dropped
    connection is the likeliest calendar failure there is, so it has to arrive as
    the same kind of error a 403 does.
    """
    http = FakeHttp(failure)
    http.install(monkeypatch)

    with pytest.raises(gcal.GCalError) as excinfo:
        await gcal.check()

    assert excinfo.value.status == gcal.TRANSPORT_STATUS
    assert "ClientOSError" in excinfo.value.message or "TimeoutError" in excinfo.value.message


async def test_a_dead_token_endpoint_is_a_calendar_error_too(calendar, monkeypatch) -> None:
    """Auth is the other half of the transport and fails the same way.

    The cached token is dropped first: with one still in hand the request would
    never go near the token endpoint and the test would prove nothing.
    """
    gcal._invalidate_token()
    monkeypatch.setattr(aiohttp, "ClientSession", _Unreachable(TimeoutError()))

    with pytest.raises(gcal.GCalError) as excinfo:
        await gcal.check()

    assert excinfo.value.status == gcal.TRANSPORT_STATUS


# ---------------------------------------------------------------- switched off


async def test_a_disabled_calendar_stays_off_the_network(monkeypatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "google_calendar_id", "")
    forget_credentials()
    forbid_network(monkeypatch)

    assert gcal.enabled() is False
    assert await gcal.upsert_event(GID, BODY) is None
    assert await gcal.delete_event(GID) is None
    assert await gcal.list_event_ids(7) == set()


# ---------------------------------------------------------------- setup helper


def test_the_account_email_is_offered_for_the_sharing_instructions(calendar) -> None:
    """Nobody can share a calendar with an address the bot refuses to show."""
    assert gcal.account_email() == ACCOUNT_EMAIL


def test_no_key_file_means_no_account_email(monkeypatch, tmp_path: Path) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "google_credentials_file", tmp_path / "absent.json")
    forget_credentials()

    assert gcal.account_email() is None


def test_replacing_the_key_file_in_place_takes_effect(tmp_path, monkeypatch) -> None:
    """Rotation is a file copy, and the docs never ask anyone to restart afterwards.

    A new key lands at exactly the same path, so memoising the parse on the path
    alone kept signing with the revoked one indefinitely - and the failure looks
    like Google refusing the account rather than the bot reading a stale file.
    """
    forget_credentials()
    path = tmp_path / "service-account.json"
    path.write_text(
        json.dumps({"client_email": "old@x.iam.gserviceaccount.com", "private_key": "OLD"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(get_settings(), "google_credentials_file", path)
    assert gcal.account_email() == "old@x.iam.gserviceaccount.com"

    # The cached token was minted with the key that is about to disappear.
    monkeypatch.setattr(gcal, "_token", "minted-with-the-old-key")
    monkeypatch.setattr(gcal, "_token_expires", float("inf"))
    path.write_text(
        json.dumps({"client_email": "brand-new@x.iam.gserviceaccount.com", "private_key": "NEW"}),
        encoding="utf-8",
    )

    assert gcal.account_email() == "brand-new@x.iam.gserviceaccount.com"
    assert gcal._token is None, "токен от отозванного ключа обязан уйти вместе с ним"
    forget_credentials()
