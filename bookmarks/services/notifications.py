"""
Sends notifications to ntfy, Discord, Slack, Gotify, Telegram or any JSON
webhook. Targets are configured as URLs in the user profile, one per line.
Most services are detected from the URL, others need a prefix:

    https://ntfy.sh/<topic>
    ntfy+https://<token or user:password>@ntfy.example.com/<topic>
    https://discord.com/api/webhooks/<id>/<token>
    https://hooks.slack.com/services/...
    slack+https://mattermost.example.com/hooks/...
    gotify+https://gotify.example.com/message?token=<app token>
    telegram://<bot token>/<chat id>
    json+https://example.com/hook

Requests go through the SSRF protected HTTP client, so targets on internal
networks must be allowed with the LD_ALLOWED_INTERNAL_HOSTS option.
"""

import base64
import logging
import urllib.parse
from dataclasses import dataclass

from django.conf import settings

from bookmarks.models import Bookmark, UserProfile
from bookmarks.services import http_client

logger = logging.getLogger(__name__)

TIMEOUT = 15
SERVICES = ("ntfy", "discord", "slack", "gotify", "telegram", "json")


@dataclass
class Notification:
    title: str
    message: str
    # Optional link that should be opened when clicking the notification
    url: str | None = None


class NotifyError(Exception):
    pass


def parse_targets(value: str | None) -> list[str]:
    """Parses targets from a text with one URL per line, ignoring # comments."""
    targets = []
    for line in (value or "").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            targets.append(line)
    return targets


def validate_target(target: str):
    """Raises NotifyError if the target URL is not supported."""
    service, url = _detect_service(target)
    if service == "telegram":
        _parse_telegram(url)
        return
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise NotifyError(f"Invalid URL: {redact(target)}")
    if service == "ntfy":
        _ntfy_topic(parts)


def is_active(profile: UserProfile) -> bool:
    background_tasks_enabled = not settings.LD_DISABLE_BACKGROUND_TASKS
    return background_tasks_enabled and bool(
        parse_targets(profile.notification_targets)
    )


def send(target: str, notification: Notification):
    """Sends a notification to a single target. Raises NotifyError on failure."""
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
    except http_client.RequestException as e:
        raise NotifyError(f"{service}: {_describe_error(e)}") from e


def _describe_error(error: Exception) -> str:
    # Don't use the exception message, it can contain the request URL, which
    # can contain secrets like webhook or bot tokens
    if isinstance(error, http_client.BlockedAddressError):
        return str(error)
    if isinstance(error, http_client.RequestTimeout):
        return "request timed out"
    if isinstance(error, http_client.RequestConnectionError):
        return "connection failed"
    return error.__class__.__name__


def send_to_targets(targets: list[str], notification: Notification) -> list[str]:
    """Sends a notification to all targets. Returns error messages for failed targets."""
    errors = []
    for target in targets:
        try:
            send(target, notification)
        except NotifyError as e:
            message = f"Failed to send notification to {redact(target)}: {e}"
            logger.warning(message)
            errors.append(message)
    return errors


def send_to_user(profile: UserProfile, notification: Notification) -> list[str]:
    return send_to_targets(parse_targets(profile.notification_targets), notification)


def new_bookmark_notification(bookmark: Bookmark) -> Notification:
    lines = [bookmark.url] if bookmark.title else []
    if bookmark.description:
        lines.append(_truncate(bookmark.description, 300))
    tag_names = bookmark.tag_names
    if tag_names:
        lines.append(" ".join(f"#{tag}" for tag in tag_names))
    return Notification(
        title=f"New bookmark: {bookmark.resolved_title}",
        message="\n".join(lines) or bookmark.url,
        url=bookmark.url,
    )


def redact(target: str) -> str:
    """Hides credentials in a target URL, for logging and error messages."""
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
        if service not in SERVICES or service == "telegram":
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
    response = http_client.request(
        "POST",
        url,
        json=payload,
        headers={"User-Agent": "linkding", **(headers or {})},
        timeout=TIMEOUT,
        allow_redirects=False,
    )
    if not response.ok:
        raise NotifyError(f"HTTP {response.status_code}")


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


def _ntfy_topic(parts: urllib.parse.SplitResult) -> str:
    topic = parts.path.strip("/")
    if not topic or "/" in topic:
        raise NotifyError("ntfy URL must have the form https://<server>/<topic>")
    return topic


def _send_ntfy(url: str, notification: Notification):
    url, headers = _split_credentials(url)
    parts = urllib.parse.urlsplit(url)
    topic = _ntfy_topic(parts)
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


def _parse_telegram(url: str) -> tuple[str, str]:
    bot_token, _, chat_id = url[len("telegram://") :].partition("/")
    chat_id = chat_id.strip("/")
    if not bot_token or not chat_id:
        raise NotifyError(
            "Telegram URL must have the form telegram://<bot token>/<chat id>"
        )
    return bot_token, chat_id


def _send_telegram(url: str, notification: Notification):
    bot_token, chat_id = _parse_telegram(url)
    text = f"{notification.title}\n\n{notification.message}"
    if notification.url:
        text += f"\n{notification.url}"
    _post_json(
        f"https://api.telegram.org/bot{bot_token}/sendMessage",
        {
            "chat_id": chat_id,
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
