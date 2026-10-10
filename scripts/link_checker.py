#!/usr/bin/env python3
"""
Checks a linkding collection for dead links and duplicates using the REST API.

Meant to be run periodically, e.g. from cron. Bookmarks are never deleted.
Problems are marked with tags instead, so they can be reviewed and cleaned up
in the linkding UI by searching for the tags:

- `dead-link`: the URL returned 404 / 410, or its domain no longer exists.
  The tag is removed again if the link works on a later run.
- `duplicate`: the URL points to the same page as another bookmark, ignoring
  differences like http/https, "www.", trailing slashes, URL fragments and
  tracking parameters (utm_*, fbclid, ...). The tag is removed again once the
  duplicates are gone.

Other errors (timeouts, 5xx, 403, TLS errors, ...) are often temporary or
caused by bot protection, so they are only reported, not tagged.

Only uses the Python standard library. Usage:

    LD_URL=https://linkding.example.com LD_TOKEN=<api token> python3 link_checker.py

Run with --help for all options.
"""

import argparse
import concurrent.futures
import importlib.util
import json
import os
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path


def _load_notify():
    # Load notify.py from this folder, independent of the current directory
    path = Path(__file__).resolve().with_name("notify.py")
    spec = importlib.util.spec_from_file_location("linkding_notify", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


notify = _load_notify()

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)
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

# Link check results
ALIVE = "alive"
DEAD = "dead"
ERROR = "error"


@dataclass
class CheckResult:
    status: str
    detail: str


# --- linkding API -------------------------------------------------------------


class LinkdingApi:
    def __init__(self, base_url: str, token: str, timeout: float = 30):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def _request(self, method: str, url: str, body=None):
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("Authorization", f"Token {self.token}")
        request.add_header("Accept", "application/json")
        if data is not None:
            request.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            return json.loads(response.read() or b"null")

    def get_bookmarks(self, archived: bool = False):
        path = "/api/bookmarks/archived/" if archived else "/api/bookmarks/"
        url = f"{self.base_url}{path}?limit=100"
        while url:
            page = self._request("GET", url)
            yield from page["results"]
            url = page.get("next")

    def set_tags(self, bookmark_id: int, tag_names: list[str]):
        url = f"{self.base_url}/api/bookmarks/{bookmark_id}/"
        self._request("PATCH", url, {"tag_names": tag_names})


# --- Dead link check ----------------------------------------------------------


def dns_works() -> bool:
    """Checks that the DNS resolver works, by resolving a domain that always
    exists. Without this, a DNS outage would mark every link as dead."""
    try:
        socket.getaddrinfo("example.com", 443)
        return True
    except OSError:
        return False


def check_link(url: str, timeout: float, trust_dns: bool = True) -> CheckResult:
    if urllib.parse.urlsplit(url).scheme not in ("http", "https"):
        return CheckResult(ALIVE, "not an http(s) URL, skipped")

    # Try a cheap HEAD request first. Some servers don't support HEAD or
    # answer it incorrectly, so retry with GET if HEAD does not succeed.
    result = _request_status(url, "HEAD", timeout, trust_dns)
    if result.status != ALIVE:
        get_result = _request_status(url, "GET", timeout, trust_dns)
        # Keep a definitive answer from either method
        if get_result.status == ALIVE or result.status != DEAD:
            result = get_result
    return result


def _request_status(
    url: str, method: str, timeout: float, trust_dns: bool
) -> CheckResult:
    request = urllib.request.Request(url, method=method)
    request.add_header("User-Agent", USER_AGENT)
    request.add_header("Accept", "text/html,application/xhtml+xml,*/*;q=0.8")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return CheckResult(ALIVE, str(response.status))
    except urllib.error.HTTPError as e:
        if e.code in DEAD_STATUS_CODES:
            return CheckResult(DEAD, f"HTTP {e.code}")
        return CheckResult(ERROR, f"HTTP {e.code}")
    except urllib.error.URLError as e:
        reason = e.reason
        # The domain does not exist (anymore)
        if isinstance(reason, socket.gaierror) and reason.errno in (
            socket.EAI_NONAME,
            getattr(socket, "EAI_NODATA", socket.EAI_NONAME),
        ):
            return CheckResult(DEAD if trust_dns else ERROR, "domain not found")
        return CheckResult(ERROR, str(reason))
    except TimeoutError:
        return CheckResult(ERROR, "timeout")
    except Exception as e:
        # E.g. TLS errors, connection resets, invalid URLs
        return CheckResult(ERROR, str(e) or e.__class__.__name__)


# --- Duplicate detection ------------------------------------------------------


def normalize_url(url: str) -> str:
    """Returns a key that is equal for URLs that most likely point to the same page."""
    parts = urllib.parse.urlsplit(url.strip())
    scheme = parts.scheme.lower()
    if scheme in ("http", "https"):
        scheme = "http"
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    port = parts.port
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


def find_duplicates(bookmarks: list[dict]) -> list[list[dict]]:
    groups = defaultdict(list)
    for bookmark in bookmarks:
        groups[normalize_url(bookmark["url"])].append(bookmark)
    return [group for group in groups.values() if len(group) > 1]


# --- Main ---------------------------------------------------------------------


def run(args, api: LinkdingApi, out=sys.stdout) -> int:
    def log(message=""):
        if not args.quiet:
            print(message, file=out)

    bookmarks = list(api.get_bookmarks())
    if not args.skip_archived:
        bookmarks += list(api.get_bookmarks(archived=True))
    log(f"Loaded {len(bookmarks)} bookmarks")

    # Tags each bookmark should have added / removed
    add_tags = defaultdict(set)
    remove_tags = defaultdict(set)
    dead = []
    errors = []
    duplicate_groups = []

    if not args.no_dead_links:
        trust_dns = dns_works()
        if not trust_dns:
            log(
                "Warning: DNS lookups don't work, unknown domains are not tagged as dead"
            )
        log(f"Checking links with {args.workers} workers...")
        with concurrent.futures.ThreadPoolExecutor(args.workers) as executor:
            futures = {
                executor.submit(
                    check_link, bookmark["url"], args.timeout, trust_dns
                ): bookmark
                for bookmark in bookmarks
            }
            for future in concurrent.futures.as_completed(futures):
                bookmark = futures[future]
                result = future.result()
                if result.status == DEAD:
                    dead.append((bookmark, result))
                    add_tags[bookmark["id"]].add(args.dead_tag)
                elif result.status == ALIVE:
                    remove_tags[bookmark["id"]].add(args.dead_tag)
                else:
                    # Inconclusive, keep the current tag state
                    errors.append((bookmark, result))

    if not args.no_duplicates:
        duplicate_groups = find_duplicates(bookmarks)
        duplicate_ids = {b["id"] for group in duplicate_groups for b in group}
        for bookmark in bookmarks:
            if bookmark["id"] in duplicate_ids:
                add_tags[bookmark["id"]].add(args.duplicate_tag)
            else:
                remove_tags[bookmark["id"]].add(args.duplicate_tag)

    # Apply tag changes, only for bookmarks that actually change
    updated = 0
    # Problems that were not tagged before, used for notifications
    new_dead = []
    new_duplicates = []
    for bookmark in bookmarks:
        current = bookmark["tag_names"]
        current_lower = {tag.lower() for tag in current}
        to_add = [t for t in add_tags[bookmark["id"]] if t.lower() not in current_lower]
        to_remove = {t.lower() for t in remove_tags[bookmark["id"]]}
        new_tags = [t for t in current if t.lower() not in to_remove] + sorted(to_add)
        if new_tags == current:
            continue
        if args.dead_tag in to_add:
            new_dead.append(bookmark)
        if args.duplicate_tag in to_add:
            new_duplicates.append(bookmark)
        updated += 1
        if not args.dry_run:
            api.set_tags(bookmark["id"], new_tags)

    if dead:
        log(f"\nDead links ({len(dead)}):")
        for bookmark, result in sorted(dead, key=lambda d: d[0]["url"]):
            log(f"  [{result.detail}] {bookmark['url']}")
    if errors and args.verbose:
        log(f"\nCould not check, not tagged ({len(errors)}):")
        for bookmark, result in sorted(errors, key=lambda e: e[0]["url"]):
            log(f"  [{result.detail}] {bookmark['url']}")
    if duplicate_groups:
        log(f"\nDuplicates ({len(duplicate_groups)} groups):")
        for group in duplicate_groups:
            log("  " + "\n  = ".join(b["url"] for b in group))

    action = "would be updated (dry run)" if args.dry_run else "updated"
    log(
        f"\nSummary: {len(dead)} dead, {len(errors)} could not be checked, "
        f"{len(duplicate_groups)} duplicate groups, {updated} bookmarks {action}"
    )

    if args.notify and (new_dead or new_duplicates or args.notify_always):
        notification = build_notification(
            args, new_dead, new_duplicates, len(dead), len(duplicate_groups)
        )
        if args.dry_run:
            log(
                f"\nWould send notification:\n{notification.title}\n{notification.message}"
            )
        else:
            errors = notify.send_all(args.notify, notification)
            for error in errors:
                print(error, file=sys.stderr)
            if errors:
                return 1
    return 0


def build_notification(args, new_dead, new_duplicates, dead_total, duplicate_total):
    max_listed = 10
    lines = []
    if new_dead:
        lines.append(f"New dead links ({len(new_dead)}):")
        lines += [f"• {b.get('title') or b['url']}" for b in new_dead[:max_listed]]
        if len(new_dead) > max_listed:
            lines.append(f"…and {len(new_dead) - max_listed} more")
    if new_duplicates:
        if lines:
            lines.append("")
        lines.append(f"New duplicates ({len(new_duplicates)}):")
        lines += [f"• {b['url']}" for b in new_duplicates[:max_listed]]
        if len(new_duplicates) > max_listed:
            lines.append(f"…and {len(new_duplicates) - max_listed} more")
    if not lines:
        lines.append("No new problems found.")
    lines.append("")
    lines.append(
        f"In total: {dead_total} dead links, {duplicate_total} groups of duplicates"
    )

    if new_dead or new_duplicates:
        parts = []
        if new_dead:
            parts.append(f"{len(new_dead)} new dead links")
        if new_duplicates:
            parts.append(f"{len(new_duplicates)} new duplicates")
        title = "linkding: " + ", ".join(parts)
    else:
        title = "linkding: link check finished"

    # Link to a search for the tagged bookmarks
    tag = args.dead_tag if new_dead or not new_duplicates else args.duplicate_tag
    query = urllib.parse.urlencode({"q": f"#{tag}"})
    url = f"{args.url.rstrip('/')}/bookmarks?{query}"
    return notify.Notification(title, "\n".join(lines), url)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Tag dead links and duplicates in linkding.",
        epilog="The linkding URL and API token can also be set with the LD_URL and LD_TOKEN environment variables.",
    )
    parser.add_argument("--url", default=os.getenv("LD_URL"), help="linkding base URL")
    parser.add_argument("--token", default=os.getenv("LD_TOKEN"), help="API token")
    parser.add_argument("--dead-tag", default="dead-link", help="default: dead-link")
    parser.add_argument(
        "--duplicate-tag", default="duplicate", help="default: duplicate"
    )
    parser.add_argument(
        "--no-dead-links", action="store_true", help="skip the dead link check"
    )
    parser.add_argument(
        "--no-duplicates", action="store_true", help="skip the duplicate check"
    )
    parser.add_argument(
        "--skip-archived", action="store_true", help="ignore archived bookmarks"
    )
    parser.add_argument(
        "--workers", type=int, default=8, help="parallel link checks (default: 8)"
    )
    parser.add_argument(
        "--timeout", type=float, default=15, help="seconds per link (default: 15)"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="report only, don't change tags"
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="also list links that could not be checked",
    )
    parser.add_argument("--quiet", action="store_true", help="no output")
    parser.add_argument(
        "--notify",
        action="append",
        default=None,
        help="send a notification when new problems are found, see notify.py for "
        "supported targets. Can be repeated (env: LD_NOTIFY, space separated)",
    )
    parser.add_argument(
        "--notify-always",
        action="store_true",
        help="send a notification after every run, even without new problems",
    )
    args = parser.parse_args(argv)
    if not args.url or not args.token:
        parser.error("--url and --token (or LD_URL and LD_TOKEN) are required")
    if args.notify is None:
        args.notify = notify.parse_targets(os.getenv("LD_NOTIFY"))
    return args


def main(argv=None):
    args = parse_args(argv)
    api = LinkdingApi(args.url, args.token)
    try:
        return run(args, api)
    except urllib.error.HTTPError as e:
        print(f"linkding API request failed: HTTP {e.code} {e.reason}", file=sys.stderr)
        return 1
    except urllib.error.URLError as e:
        print(f"Could not connect to linkding: {e.reason}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
