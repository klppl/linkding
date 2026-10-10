#!/usr/bin/env python3
"""
Sends a notification when new bookmarks are added to linkding, e.g. to ntfy or
Discord. Meant to be run periodically from cron, e.g. every 10 minutes:

    LD_URL=https://linkding.example.com LD_TOKEN=<api token> \\
    LD_NOTIFY="ntfy+https://ntfy.example.com/linkding" \\
    python3 new_bookmarks_notifier.py --state-file ~/.linkding-notifier.json

The state file remembers which bookmarks were already announced. On the first
run, nothing is sent and only the current state is saved, so you don't get a
notification for every existing bookmark.

"New" means added after the last run. Bookmarks imported with an older date
(e.g. from a browser export) are not announced.

See notify.py for the supported notification targets. Only uses the Python
standard library.
"""

import argparse
import importlib.util
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
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

# Above this many new bookmarks, only the first ones are listed
MAX_LISTED = 10


class LinkdingApi:
    def __init__(self, base_url: str, token: str, timeout: float = 30):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def _get(self, url: str):
        request = urllib.request.Request(url)
        request.add_header("Authorization", f"Token {self.token}")
        request.add_header("Accept", "application/json")
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            return json.loads(response.read())

    def get_bookmarks(self, archived: bool = False, added_since: str | None = None):
        path = "/api/bookmarks/archived/" if archived else "/api/bookmarks/"
        params = {"limit": 100}
        if added_since:
            params["added_since"] = added_since
        url = f"{self.base_url}{path}?{urllib.parse.urlencode(params)}"
        while url:
            page = self._get(url)
            yield from page["results"]
            url = page.get("next")

    def get_latest_bookmark(self, archived: bool = False):
        # Bookmarks are sorted by date added, newest first
        path = "/api/bookmarks/archived/" if archived else "/api/bookmarks/"
        page = self._get(f"{self.base_url}{path}?limit=1")
        return page["results"][0] if page["results"] else None


def parse_date(value: str) -> datetime:
    # fromisoformat only supports the "Z" suffix since Python 3.11
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def load_state(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return None


def save_state(path: Path, state: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(state, indent=2))
    tmp_path.replace(path)


def build_notification(bookmarks: list[dict], linkding_url: str):
    def title_of(bookmark):
        return bookmark.get("title") or bookmark["url"]

    if len(bookmarks) == 1:
        bookmark = bookmarks[0]
        lines = [bookmark["url"]] if bookmark.get("title") else []
        if bookmark.get("description"):
            lines.append(notify._truncate(bookmark["description"], 300))
        if bookmark.get("tag_names"):
            lines.append(" ".join(f"#{tag}" for tag in bookmark["tag_names"]))
        return notify.Notification(
            title=f"New bookmark: {title_of(bookmark)}",
            message="\n".join(lines) or bookmark["url"],
            url=bookmark["url"],
        )

    lines = [f"• {title_of(b)}" for b in bookmarks[:MAX_LISTED]]
    if len(bookmarks) > MAX_LISTED:
        lines.append(f"…and {len(bookmarks) - MAX_LISTED} more")
    return notify.Notification(
        title=f"{len(bookmarks)} new bookmarks",
        message="\n".join(lines),
        url=f"{linkding_url.rstrip('/')}/bookmarks",
    )


def run(args, api: LinkdingApi, out=sys.stdout) -> int:
    def log(message):
        if not args.quiet:
            print(message, file=out)

    state_path = Path(args.state_file).expanduser()
    state = load_state(state_path)

    if state is None:
        # First run: remember the newest bookmark, don't notify
        latest = [api.get_latest_bookmark(archived) for archived in (False, True)]
        latest = [b for b in latest if b]
        newest = max(latest, key=lambda b: parse_date(b["date_added"]), default=None)
        save_state(
            state_path,
            {"last_date_added": newest["date_added"] if newest else None},
        )
        log(
            f"Initialized state in {state_path}, will notify about bookmarks added from now on"
        )
        return 0

    last_date_added = state.get("last_date_added")
    new_bookmarks = list(api.get_bookmarks(added_since=last_date_added))
    if not args.skip_archived:
        new_bookmarks += list(
            api.get_bookmarks(archived=True, added_since=last_date_added)
        )
    # Also filter here, the API ignores the filter if the date is invalid
    if last_date_added:
        last = parse_date(last_date_added)
        new_bookmarks = [b for b in new_bookmarks if parse_date(b["date_added"]) > last]
    new_bookmarks.sort(key=lambda b: parse_date(b["date_added"]))

    if not new_bookmarks:
        log("No new bookmarks")
        return 0

    notification = build_notification(new_bookmarks, args.url)
    if args.dry_run:
        log(f"Would send:\n{notification.title}\n{notification.message}")
        return 0

    errors = notify.send_all(args.notify, notification)
    for error in errors:
        print(error, file=sys.stderr)
    if errors and len(errors) == len(args.notify):
        # Nothing was delivered, keep the state to retry on the next run
        return 1

    save_state(state_path, {"last_date_added": new_bookmarks[-1]["date_added"]})
    log(f"Notified about {len(new_bookmarks)} new bookmarks")
    return 1 if errors else 0


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Send a notification when new bookmarks are added to linkding.",
        epilog="See notify.py for supported notification targets.",
    )
    parser.add_argument(
        "--url", default=os.getenv("LD_URL"), help="linkding base URL (env: LD_URL)"
    )
    parser.add_argument(
        "--token", default=os.getenv("LD_TOKEN"), help="API token (env: LD_TOKEN)"
    )
    parser.add_argument(
        "--notify",
        action="append",
        default=None,
        help="notification target URL, can be repeated (env: LD_NOTIFY, space separated)",
    )
    parser.add_argument(
        "--state-file",
        default=os.getenv("LD_NOTIFIER_STATE", "~/.linkding-notifier.json"),
        help="where to remember the last notified bookmark (env: LD_NOTIFIER_STATE, default: ~/.linkding-notifier.json)",
    )
    parser.add_argument(
        "--skip-archived",
        action="store_true",
        help="ignore bookmarks added as archived",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the notification instead of sending it",
    )
    parser.add_argument("--quiet", action="store_true", help="no output except errors")
    args = parser.parse_args(argv)
    if not args.url or not args.token:
        parser.error("--url and --token (or LD_URL and LD_TOKEN) are required")
    if args.notify is None:
        args.notify = notify.parse_targets(os.getenv("LD_NOTIFY"))
    if not args.notify and not args.dry_run:
        parser.error("--notify (or LD_NOTIFY) is required")
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
