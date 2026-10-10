#!/usr/bin/env python3
"""
Sends notifications to ntfy, Discord, Slack, Gotify, Telegram or any JSON
webhook. Used by the other scripts in this folder, and can be run on its own to
test a notification target:

    python3 notify.py "ntfy+https://ntfy.example.com/linkding" "Hello from linkding"

Notification targets are URLs. Most services are detected from the URL, others
need a prefix that names the service:

    https://ntfy.sh/<topic>                         ntfy.sh
    ntfy+https://<user>:<password>@ntfy.example.com/<topic>
    ntfy+https://<access token>@ntfy.example.com/<topic>
    https://discord.com/api/webhooks/<id>/<token>   Discord
    https://hooks.slack.com/services/...            Slack
    slack+https://mattermost.example.com/hooks/...  Slack-compatible (Mattermost, ...)
    gotify+https://gotify.example.com/message?token=<app token>
    telegram://<bot token>/<chat id>
    json+https://example.com/hook                   POSTs {"title", "message", "url"}

Only uses the Python standard library.
"""

import argparse
import base64
import json
import sys
import urllib.parse
import urllib.request
from dataclasses import dataclass

TIMEOUT = 15


@dataclass
class Notification:
    title: str
    message: str
    # Optional link that should be opened when clicking the notification
    url: str | None = None


class NotifyError(Exception):
    pass


def parse_targets(value: str | None) -> list[str]:
    """Parses a whitespace separated list of target URLs, e.g. from an env variable."""
    return value.split() if value else []


def send(target: str, notification: Notification):
    """Sends a notification to a single target URL. Raises NotifyError on failure."""
    service, url = _detect_service(target)
    try:
        if service == "ntfy":
            _send_ntfy(url, notification)
        elif service == "discord":
            _send_discord(url, notification)
        elif service == "slack":
            _send_slack(url, notification)
        elif service == "gotify":
            _send_gotify(url, notification)
        elif service == "telegram":
            _send_telegram(url, notification)
        else:
            _send_json(url, notification)
    except NotifyError:
        raise
    except Exception as e:
        raise NotifyError(f"{service}: {e}") from e


def send_all(targets: list[str], notification: Notification) -> list[str]:
    """Sends a notification to all targets. Returns error messages for failed targets."""
    errors = []
    for target in targets:
        try:
            send(target, notification)
        except NotifyError as e:
            errors.append(f"Failed to send notification to {redact(target)}: {e}")
    return errors


def redact(target: str) -> str:
    """Hides credentials in a target URL, for logging."""
    if target.startswith("telegram://"):
        return "telegram://***"
    parts = urllib.parse.urlsplit(target)
    netloc = parts.hostname or ""
    if parts.port:
        netloc += f":{parts.port}"
    # Paths and queries can contain secrets as well, e.g. Discord webhook tokens
    return urllib.parse.urlunsplit((parts.scheme, netloc, "/***", "", ""))


def _detect_service(target: str) -> tuple[str, str]:
    if target.startswith("telegram://"):
        return "telegram", target
    scheme, _, rest = target.partition("://")
    if "+" in scheme:
        service, _, real_scheme = scheme.partition("+")
        if service not in ("ntfy", "discord", "slack", "gotify", "json"):
            raise NotifyError(f"Unknown notification service: {service}")
        return service, f"{real_scheme}://{rest}"

    host = (urllib.parse.urlsplit(target).hostname or "").lower()
    if host in ("discord.com", "discordapp.com") and "/api/webhooks/" in target:
        return "discord", target
    if host == "hooks.slack.com":
        return "slack", target
    if host == "ntfy.sh":
        return "ntfy", target
    raise NotifyError(
        f"Can't detect the notification service for {redact(target)}, "
        "add a prefix like ntfy+https://, gotify+https:// or json+https://"
    )


def _post_json(url: str, payload: dict, headers: dict | None = None):
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode(), method="POST"
    )
    request.add_header("Content-Type", "application/json")
    request.add_header("User-Agent", "linkding-notify")
    for name, value in (headers or {}).items():
        request.add_header(name, value)
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        return response.read()


def _truncate(text: str, max_length: int) -> str:
    return text if len(text) <= max_length else text[: max_length - 1] + "…"


def _split_credentials(url: str) -> tuple[str, dict]:
    """Removes user info from a URL, and returns it as an Authorization header."""
    parts = urllib.parse.urlsplit(url)
    headers = {}
    if parts.username and parts.password:
        credentials = f"{urllib.parse.unquote(parts.username)}:{urllib.parse.unquote(parts.password)}"
        headers["Authorization"] = (
            "Basic " + base64.b64encode(credentials.encode()).decode()
        )
    elif parts.username:
        headers["Authorization"] = f"Bearer {urllib.parse.unquote(parts.username)}"
    netloc = parts.hostname or ""
    if parts.port:
        netloc += f":{parts.port}"
    clean_url = urllib.parse.urlunsplit(
        (parts.scheme, netloc, parts.path, parts.query, "")
    )
    return clean_url, headers


def _send_ntfy(url: str, notification: Notification):
    url, headers = _split_credentials(url)
    parts = urllib.parse.urlsplit(url)
    topic = parts.path.strip("/")
    if not topic or "/" in topic:
        raise NotifyError("ntfy URL must have the form https://<server>/<topic>")
    # Publish as JSON to the server root, which supports non-ASCII titles
    server_url = urllib.parse.urlunsplit((parts.scheme, parts.netloc, "/", "", ""))
    payload = {
        "topic": topic,
        "title": notification.title,
        "message": notification.message,
        "tags": ["bookmark"],
    }
    if notification.url:
        payload["click"] = notification.url
    _post_json(server_url, payload, headers)


def _send_discord(url: str, notification: Notification):
    embed = {
        "title": _truncate(notification.title, 256),
        "description": _truncate(notification.message, 4096),
    }
    if notification.url:
        embed["url"] = notification.url
    _post_json(url, {"username": "linkding", "embeds": [embed]})


def _send_slack(url: str, notification: Notification):
    title = notification.title
    if notification.url:
        title = f"<{notification.url}|{title}>"
    _post_json(url, {"text": f"*{title}*\n{notification.message}"})


def _send_gotify(url: str, notification: Notification):
    url, headers = _split_credentials(url)
    message = notification.message
    if notification.url:
        message += f"\n{notification.url}"
    _post_json(
        url,
        {"title": notification.title, "message": message, "priority": 5},
        headers,
    )


def _send_telegram(url: str, notification: Notification):
    bot_token, _, chat_id = url[len("telegram://") :].partition("/")
    if not bot_token or not chat_id:
        raise NotifyError(
            "Telegram URL must have the form telegram://<bot token>/<chat id>"
        )
    text = f"{notification.title}\n\n{notification.message}"
    if notification.url:
        text += f"\n{notification.url}"
    _post_json(
        f"https://api.telegram.org/bot{bot_token}/sendMessage",
        {
            "chat_id": chat_id.strip("/"),
            "text": _truncate(text, 4096),
            "disable_web_page_preview": True,
        },
    )


def _send_json(url: str, notification: Notification):
    _post_json(
        url,
        {
            "title": notification.title,
            "message": notification.message,
            "url": notification.url,
        },
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description="Send a test notification.")
    parser.add_argument("target", help="notification target URL")
    parser.add_argument("message", nargs="?", default="Test notification from linkding")
    parser.add_argument("--title", default="linkding")
    parser.add_argument("--url", help="link to open when clicking the notification")
    args = parser.parse_args(argv)
    try:
        send(args.target, Notification(args.title, args.message, args.url))
    except NotifyError as e:
        print(e, file=sys.stderr)
        return 1
    print(f"Sent notification to {redact(args.target)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
