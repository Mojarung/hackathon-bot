"""GitHub integration over the plain REST API.

aiohttp already ships with aiogram, so no HTTP client is added just for this.
Two entry points matter: creating a fresh `<название>_<год>` repo in the org, and
attaching one that already exists so the topic can hand out its link.
"""

from __future__ import annotations

import base64
import logging
import re
from dataclasses import dataclass
from typing import Any

import aiohttp

from hackbot.config import get_settings
from hackbot.domain.textutils import repo_name, safe_filename

log = logging.getLogger(__name__)

API = "https://api.github.com"
_TIMEOUT = aiohttp.ClientTimeout(total=45)

_REPO_RE = re.compile(
    r"(?:https?://(?:www\.)?github\.com/)?(?P<owner>[\w.-]+)/(?P<name>[\w.-]+?)(?:\.git)?/?$"
)


class GitHubError(RuntimeError):
    """Any non-success response, carrying the message GitHub actually returned."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"GitHub {status}: {message}")
        self.status = status
        self.message = message


@dataclass(frozen=True, slots=True)
class RepoInfo:
    full_name: str
    html_url: str
    private: bool
    default_branch: str
    description: str | None = None

    @property
    def owner(self) -> str:
        return self.full_name.split("/", 1)[0]

    @property
    def name(self) -> str:
        return self.full_name.split("/", 1)[1]


def parse_repo_ref(value: str) -> tuple[str, str] | None:
    """Accepts a full URL, `owner/name`, or a bare name for the default org."""
    value = (value or "").strip()
    if not value:
        return None
    match = _REPO_RE.match(value)
    if match:
        return match.group("owner"), match.group("name")
    if re.fullmatch(r"[\w.-]+", value):
        return get_settings().github_org, value
    return None


def _headers() -> dict[str, str]:
    settings = get_settings()
    if not settings.github_token:
        raise GitHubError(401, "GITHUB_TOKEN не настроен")
    return {
        "Authorization": f"Bearer {settings.github_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "hackbot",
    }


async def _request(
    method: str, path: str, *, json: Any = None, ok: tuple[int, ...] = (200, 201)
) -> Any:
    url = path if path.startswith("http") else f"{API}{path}"
    async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
        async with session.request(method, url, headers=_headers(), json=json) as resp:
            body = await resp.text()
            if resp.status in ok:
                return await resp.json() if body else {}
            try:
                message = (await resp.json()).get("message", body[:200])
            except Exception:
                message = body[:200]
            raise GitHubError(resp.status, message)


def _to_info(payload: dict[str, Any]) -> RepoInfo:
    return RepoInfo(
        full_name=payload["full_name"],
        html_url=payload["html_url"],
        private=bool(payload.get("private")),
        default_branch=payload.get("default_branch") or "main",
        description=payload.get("description"),
    )


async def get_repo(owner: str, name: str) -> RepoInfo | None:
    try:
        return _to_info(await _request("GET", f"/repos/{owner}/{name}"))
    except GitHubError as exc:
        if exc.status == 404:
            return None
        raise


async def create_repo(
    title: str, year: int, *, description: str | None = None, private: bool | None = None
) -> RepoInfo:
    """Create `<slug>_<year>` in the configured organisation.

    If the name is taken the existing repo is returned rather than erroring: the
    intent is always "give this topic a repo", not "insist on a fresh one".
    """
    settings = get_settings()
    name = repo_name(title, year)
    org = settings.github_org

    existing = await get_repo(org, name)
    if existing is not None:
        log.info("repo %s/%s already exists, reusing", org, name)
        return existing

    payload = {
        "name": name,
        "description": (description or f"{title} {year}")[:350],
        "private": settings.github_private if private is None else private,
        "auto_init": True,  # gives us a default branch to commit against
        "has_issues": True,
        "has_wiki": False,
        "has_projects": False,
    }
    return _to_info(await _request("POST", f"/orgs/{org}/repos", json=payload))


async def attach_repo(reference: str) -> RepoInfo:
    """Resolve a user-supplied repo reference, verifying it is reachable."""
    parsed = parse_repo_ref(reference)
    if parsed is None:
        raise GitHubError(400, "не похоже на ссылку или owner/name")
    owner, name = parsed
    repo = await get_repo(owner, name)
    if repo is None:
        raise GitHubError(404, f"репозиторий {owner}/{name} не найден или недоступен токену")
    return repo


async def set_topics(repo: RepoInfo, topics: list[str]) -> None:
    clean = [
        re.sub(r"[^a-z0-9-]", "", t.lower().replace("_", "-"))[:35]
        for t in topics
    ]
    clean = [t for t in clean if t][:20]
    if not clean:
        return
    await _request("PUT", f"/repos/{repo.full_name}/topics", json={"names": clean})


async def _existing_sha(repo: RepoInfo, path: str) -> str | None:
    try:
        payload = await _request("GET", f"/repos/{repo.full_name}/contents/{path}")
    except GitHubError as exc:
        if exc.status == 404:
            return None
        raise
    if isinstance(payload, dict):
        return payload.get("sha")
    return None


async def put_file(
    repo: RepoInfo, path: str, content: bytes, *, message: str | None = None
) -> str:
    """Create or update one file. Returns its html_url."""
    path = "/".join(safe_filename(part) for part in path.split("/") if part)
    body: dict[str, Any] = {
        "message": message or f"add {path}",
        "content": base64.b64encode(content).decode("ascii"),
        "branch": repo.default_branch,
    }
    sha = await _existing_sha(repo, path)
    if sha:
        body["sha"] = sha
    payload = await _request("PUT", f"/repos/{repo.full_name}/contents/{path}", json=body)
    return payload.get("content", {}).get("html_url", repo.html_url)


async def check_token() -> str:
    """Returns the authenticated login, for a startup sanity check."""
    payload = await _request("GET", "/user")
    return payload.get("login", "?")
