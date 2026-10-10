"""
Checks the bookmarks of a user for dead links and duplicates. Bookmarks are
never deleted, problems are marked with tags instead:

- `dead-link`: the URL returned 404 / 410, or its domain no longer exists.
  Other errors (timeouts, 5xx, 403, TLS errors, blocked internal hosts, ...)
  are often temporary or caused by bot protection, so they don't change the
  tag. If DNS lookups don't work at all, unknown domains are not tagged
  either, so a DNS outage can't mark every link as dead.
- `duplicate`: the URL points to the same page as another bookmark, ignoring
  http/https, "www.", trailing slashes, URL fragments and tracking parameters.

Tags are removed again once the problem is gone.
"""

import concurrent.futures
import logging
import socket
import urllib.parse
from collections import defaultdict
from dataclasses import dataclass, field

from django.contrib.auth.models import User

from bookmarks.models import Bookmark, Tag
from bookmarks.services import http_client
from bookmarks.services.notifications import Notification
from bookmarks.services.tags import get_or_create_tag
from bookmarks.services.website_loader import fake_request_headers

logger = logging.getLogger(__name__)

DEAD_LINK_TAG = "dead-link"
DUPLICATE_TAG = "duplicate"

WORKERS = 8
TIMEOUT = 15
DEAD_STATUS_CODES = {404, 410}
TRACKING_PARAMS = {
    "fbclid",
    "gclid",
    "dclid",
    "msclkid",
    "mc_cid",
    "mc_eid",
    "igshid",
    "yclid",
    "_hsenc",
    "_hsmi",
    "ref_src",
}

ALIVE = "alive"
DEAD = "dead"
ERROR = "error"


@dataclass
class CheckResult:
    status: str
    detail: str


@dataclass
class LinkCheckResult:
    checked: int = 0
    dead: list[Bookmark] = field(default_factory=list)
    new_dead: list[Bookmark] = field(default_factory=list)
    errors: int = 0
    duplicate_groups: list[list[Bookmark]] = field(default_factory=list)
    new_duplicates: list[Bookmark] = field(default_factory=list)

    @property
    def has_new_problems(self):
        return bool(self.new_dead or self.new_duplicates)


# --- Dead links ----------------------------------------------------------------


def dns_works() -> bool:
    """Checks that the DNS resolver works, by resolving a domain that always
    exists. Without this, a DNS outage would mark every link as dead."""
    try:
        socket.getaddrinfo("example.com", 443)
        return True
    except OSError:
        return False


def check_link(url: str, trust_dns: bool = True) -> CheckResult:
    if urllib.parse.urlsplit(url).scheme not in ("http", "https"):
        return CheckResult(ALIVE, "not an http(s) URL, skipped")

    # Try a cheap HEAD request first. Some servers don't support HEAD or
    # answer it incorrectly, so retry with GET if HEAD does not succeed.
    result = _request_status(url, "HEAD", trust_dns)
    if result.status != ALIVE:
        get_result = _request_status(url, "GET", trust_dns)
        # Keep a definitive answer from either method
        if get_result.status == ALIVE or result.status != DEAD:
            result = get_result
    return result


def _request_status(url: str, method: str, trust_dns: bool) -> CheckResult:
    try:
        # Stream to avoid downloading the body of GET requests
        response = http_client.request(
            method,
            url,
            headers=fake_request_headers(),
            timeout=TIMEOUT,
            allow_redirects=True,
            stream=True,
        )
        response.close()
    except http_client.BlockedAddressError:
        return CheckResult(ERROR, "internal host, not allowed")
    except http_client.RequestTimeout:
        return CheckResult(ERROR, "timeout")
    except Exception as e:
        if _is_unknown_domain(e):
            return CheckResult(DEAD if trust_dns else ERROR, "domain not found")
        return CheckResult(ERROR, e.__class__.__name__)

    if response.status_code in DEAD_STATUS_CODES:
        return CheckResult(DEAD, f"HTTP {response.status_code}")
    if response.status_code >= 400:
        return CheckResult(ERROR, f"HTTP {response.status_code}")
    return CheckResult(ALIVE, f"HTTP {response.status_code}")


def _is_unknown_domain(error: BaseException) -> bool:
    # requests wraps the original socket error in several layers
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        if isinstance(error, socket.gaierror):
            return error.errno in (
                socket.EAI_NONAME,
                getattr(socket, "EAI_NODATA", socket.EAI_NONAME),
            )
        error = error.__cause__ or error.__context__
    return False


# --- Duplicates ----------------------------------------------------------------


def normalize_url(url: str) -> str:
    """Returns a key that is equal for URLs that most likely point to the same page."""
    try:
        parts = urllib.parse.urlsplit(url.strip())
        port = parts.port
    except ValueError:
        return url
    scheme = parts.scheme.lower()
    if scheme in ("http", "https"):
        scheme = "http"
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if port in (80, 443):
        port = None
    netloc = f"{host}:{port}" if port else host
    path = parts.path.rstrip("/")
    for index_file in ("/index.html", "/index.htm", "/index.php"):
        if path.endswith(index_file):
            path = path[: -len(index_file)]
    query = sorted(
        (key, value)
        for key, value in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in TRACKING_PARAMS
    )
    return urllib.parse.urlunsplit(
        (scheme, netloc, path, urllib.parse.urlencode(query), "")
    )


def find_duplicates(bookmarks: list[Bookmark]) -> list[list[Bookmark]]:
    groups = defaultdict(list)
    for bookmark in bookmarks:
        groups[normalize_url(bookmark.url)].append(bookmark)
    return [group for group in groups.values() if len(group) > 1]


# --- Check ---------------------------------------------------------------------


def run_link_check(user: User) -> LinkCheckResult:
    bookmarks = list(
        Bookmark.objects.filter(owner=user).prefetch_related("tags").order_by("id")
    )
    result = LinkCheckResult(checked=len(bookmarks))

    trust_dns = dns_works()
    if not trust_dns:
        logger.warning(
            "DNS lookups don't work, unknown domains are not tagged as dead links"
        )

    # Only network requests run in threads, database access stays here
    with concurrent.futures.ThreadPoolExecutor(WORKERS) as executor:
        check_results = list(
            executor.map(lambda b: check_link(b.url, trust_dns), bookmarks)
        )

    result.duplicate_groups = find_duplicates(bookmarks)
    duplicate_ids = {b.id for group in result.duplicate_groups for b in group}

    tags = _TagCache(user)
    for bookmark, check_result in zip(bookmarks, check_results, strict=True):
        tag_names = {tag.name.lower() for tag in bookmark.tags.all()}

        has_dead_tag = DEAD_LINK_TAG in tag_names
        if check_result.status == DEAD:
            result.dead.append(bookmark)
            if not has_dead_tag:
                bookmark.tags.add(tags.get_or_create(DEAD_LINK_TAG))
                result.new_dead.append(bookmark)
        elif check_result.status == ALIVE:
            if has_dead_tag:
                bookmark.tags.remove(*tags.find(DEAD_LINK_TAG))
        else:
            result.errors += 1

        has_duplicate_tag = DUPLICATE_TAG in tag_names
        if bookmark.id in duplicate_ids:
            if not has_duplicate_tag:
                bookmark.tags.add(tags.get_or_create(DUPLICATE_TAG))
                result.new_duplicates.append(bookmark)
        elif has_duplicate_tag:
            bookmark.tags.remove(*tags.find(DUPLICATE_TAG))

    logger.info(
        f"Link check finished. user={user.username} checked={result.checked} "
        f"dead={len(result.dead)} new_dead={len(result.new_dead)} "
        f"errors={result.errors} duplicate_groups={len(result.duplicate_groups)}"
    )
    return result


class _TagCache:
    def __init__(self, user: User):
        self.user = user
        self.created = {}

    def get_or_create(self, name: str) -> Tag:
        if name not in self.created:
            self.created[name] = get_or_create_tag(name, self.user)
        return self.created[name]

    def find(self, name: str) -> list[Tag]:
        return list(Tag.objects.filter(owner=self.user, name__iexact=name))


def build_notification(result: LinkCheckResult) -> Notification:
    max_listed = 10
    lines = []
    if result.new_dead:
        lines.append(f"New dead links ({len(result.new_dead)}):")
        lines += [f"• {b.resolved_title}" for b in result.new_dead[:max_listed]]
        if len(result.new_dead) > max_listed:
            lines.append(f"…and {len(result.new_dead) - max_listed} more")
    if result.new_duplicates:
        if lines:
            lines.append("")
        lines.append(f"New duplicates ({len(result.new_duplicates)}):")
        lines += [f"• {b.url}" for b in result.new_duplicates[:max_listed]]
        if len(result.new_duplicates) > max_listed:
            lines.append(f"…and {len(result.new_duplicates) - max_listed} more")
    if not lines:
        lines.append("No new problems found.")
    lines.append("")
    lines.append(
        f"In total: {len(result.dead)} dead links, "
        f"{len(result.duplicate_groups)} groups of duplicates. "
        f"Search for #{DEAD_LINK_TAG} or #{DUPLICATE_TAG} in linkding to review them."
    )

    parts = []
    if result.new_dead:
        parts.append(f"{len(result.new_dead)} new dead links")
    if result.new_duplicates:
        parts.append(f"{len(result.new_duplicates)} new duplicates")
    title = (
        "linkding: " + ", ".join(parts) if parts else "linkding: link check finished"
    )
    return Notification(title, "\n".join(lines))
