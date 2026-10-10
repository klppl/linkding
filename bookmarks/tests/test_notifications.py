import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock

from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from huey.contrib.djhuey import HUEY as huey

from bookmarks.models import Bookmark
from bookmarks.services import http_client, notifications, tasks
from bookmarks.services.bookmarks import create_bookmark
from bookmarks.tests.helpers import BookmarkFactoryMixin


class CaptureServer:
    """Local HTTP server that records received requests."""

    def __init__(self, status=200):
        self.requests = []
        self.status = status
        captured = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                captured.requests.append(
                    {
                        "path": self.path,
                        "headers": dict(self.headers),
                        "json": json.loads(body) if body else None,
                    }
                )
                self.send_response(captured.status)
                self.end_headers()

            def log_message(self, *args):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


def allow_internal_hosts(test_case, value="127.0.0.1"):
    """Allows requests to the local capture server through the SSRF protection."""
    override = override_settings(LD_ALLOWED_INTERNAL_HOSTS=value)
    override.enable()
    test_case.addCleanup(override.disable)
    http_client.get_allowlist.cache_clear()
    test_case.addCleanup(http_client.get_allowlist.cache_clear)


class NotificationServiceTestCase(SimpleTestCase):
    def setUp(self):
        allow_internal_hosts(self)
        self.server = CaptureServer()
        self.addCleanup(self.server.stop)
        self.notification = notifications.Notification(
            "Title ✓", "Message", "https://example.com"
        )

    def last_request(self):
        self.assertEqual(len(self.server.requests), 1)
        return self.server.requests[0]

    def test_ntfy(self):
        notifications.send(f"ntfy+{self.server.url}/my-topic", self.notification)
        request = self.last_request()
        self.assertEqual(request["path"], "/")
        self.assertEqual(
            request["json"],
            {
                "topic": "my-topic",
                "title": "Title ✓",
                "message": "Message",
                "tags": ["bookmark"],
                "click": "https://example.com",
            },
        )
        self.assertNotIn("Authorization", request["headers"])

    def test_ntfy_access_token(self):
        target = f"ntfy+{self.server.url}/topic".replace("://", "://tk_secret@")
        notifications.send(target, self.notification)
        self.assertEqual(
            self.last_request()["headers"]["Authorization"], "Bearer tk_secret"
        )

    def test_ntfy_basic_auth(self):
        target = f"ntfy+{self.server.url}/topic".replace("://", "://user:pa%40ss@")
        notifications.send(target, self.notification)
        expected = base64.b64encode(b"user:pa@ss").decode()
        self.assertEqual(
            self.last_request()["headers"]["Authorization"], f"Basic {expected}"
        )

    def test_discord(self):
        notifications.send(
            f"discord+{self.server.url}/api/webhooks/1/abc", self.notification
        )
        request = self.last_request()
        self.assertEqual(request["path"], "/api/webhooks/1/abc")
        self.assertEqual(
            request["json"],
            {
                "username": "linkding",
                "embeds": [
                    {
                        "title": "Title ✓",
                        "description": "Message",
                        "url": "https://example.com",
                    }
                ],
            },
        )

    def test_slack(self):
        notifications.send(f"slack+{self.server.url}/hooks/x", self.notification)
        self.assertEqual(
            self.last_request()["json"],
            {"text": "*<https://example.com|Title ✓>*\nMessage"},
        )

    def test_gotify(self):
        notifications.send(
            f"gotify+{self.server.url}/message?token=abc", self.notification
        )
        request = self.last_request()
        self.assertEqual(request["path"], "/message?token=abc")
        self.assertEqual(
            request["json"],
            {
                "title": "Title ✓",
                "message": "Message\nhttps://example.com",
                "priority": 5,
            },
        )

    def test_json_webhook(self):
        notifications.send(f"json+{self.server.url}/hook", self.notification)
        self.assertEqual(
            self.last_request()["json"],
            {"title": "Title ✓", "message": "Message", "url": "https://example.com"},
        )

    def test_telegram(self):
        with mock.patch.object(notifications, "_post_json") as post_json:
            notifications.send("telegram://123:ABC/-100200", self.notification)
        url, payload = post_json.call_args[0]
        self.assertEqual(url, "https://api.telegram.org/bot123:ABC/sendMessage")
        self.assertEqual(payload["chat_id"], "-100200")
        self.assertEqual(payload["text"], "Title ✓\n\nMessage\nhttps://example.com")

    def test_http_error(self):
        self.server.status = 500
        with self.assertRaises(notifications.NotifyError):
            notifications.send(f"json+{self.server.url}/hook", self.notification)

    def test_errors_do_not_contain_secrets(self):
        errors = notifications.send_to_targets(
            [
                f"json+{self.server.url}/hook",
                "discord+http://user:secret@127.0.0.1:1/api/webhooks/1/token",
            ],
            self.notification,
        )
        self.assertEqual(len(self.server.requests), 1)
        self.assertEqual(len(errors), 1)
        self.assertNotIn("secret", errors[0])
        self.assertNotIn("token", errors[0])

    def test_blocks_internal_hosts_by_default(self):
        allow_internal_hosts(self, "")
        with self.assertRaises(notifications.NotifyError) as context:
            notifications.send(f"json+{self.server.url}/hook", self.notification)
        self.assertIn("LD_ALLOWED_INTERNAL_HOSTS", str(context.exception))
        self.assertEqual(self.server.requests, [])

    def test_validate_target(self):
        valid = [
            "https://ntfy.sh/topic",
            "ntfy+https://token@ntfy.example.com/topic",
            "https://discord.com/api/webhooks/1/x",
            "https://hooks.slack.com/services/x",
            "slack+https://mattermost.example.com/hooks/x",
            "gotify+https://gotify.example.com/message?token=x",
            "telegram://123:ABC/456",
            "json+https://example.com/hook",
        ]
        for target in valid:
            notifications.validate_target(target)

        invalid = [
            "https://example.com/hook",
            "unknown+https://example.com",
            "ntfy+https://ntfy.example.com/",
            "telegram://only-token",
            "json+ftp://example.com",
        ]
        for target in invalid:
            with self.assertRaises(notifications.NotifyError, msg=target):
                notifications.validate_target(target)

    def test_parse_targets(self):
        self.assertEqual(notifications.parse_targets(None), [])
        self.assertEqual(
            notifications.parse_targets(
                " https://ntfy.sh/a\n\n# comment\njson+https://b "
            ),
            ["https://ntfy.sh/a", "json+https://b"],
        )


class NewBookmarkNotificationTestCase(TestCase, BookmarkFactoryMixin):
    def setUp(self):
        huey.immediate = True
        self.addCleanup(setattr, huey, "immediate", False)
        delay_patcher = mock.patch.object(tasks, "NEW_BOOKMARK_NOTIFICATION_DELAY", 0)
        delay_patcher.start()
        self.addCleanup(delay_patcher.stop)
        send_patcher = mock.patch.object(notifications, "send")
        self.mock_send = send_patcher.start()
        self.addCleanup(send_patcher.stop)

        self.user = self.get_or_create_test_user()
        self.user.profile.notification_targets = "https://ntfy.sh/topic"
        self.user.profile.notify_new_bookmarks = True
        self.user.profile.save()

    def create(self, url="https://example.com/new", title="New", tags="news"):
        return create_bookmark(
            Bookmark(url=url, title=title, description="Description"), tags, self.user
        )

    def test_notifies_about_new_bookmark(self):
        self.create()

        self.mock_send.assert_called_once()
        target, notification = self.mock_send.call_args[0]
        self.assertEqual(target, "https://ntfy.sh/topic")
        self.assertEqual(notification.title, "New bookmark: New")
        self.assertEqual(
            notification.message, "https://example.com/new\nDescription\n#news"
        )
        self.assertEqual(notification.url, "https://example.com/new")

    def test_uses_url_if_bookmark_has_no_title(self):
        self.create(title="", tags="")
        notification = self.mock_send.call_args[0][1]
        self.assertEqual(notification.title, "New bookmark: https://example.com/new")
        self.assertEqual(notification.message, "Description")

    def test_does_not_notify_when_disabled(self):
        self.user.profile.notify_new_bookmarks = False
        self.user.profile.save()
        self.create()
        self.mock_send.assert_not_called()

    def test_does_not_notify_without_targets(self):
        self.user.profile.notification_targets = ""
        self.user.profile.save()
        self.create()
        self.mock_send.assert_not_called()

    @override_settings(LD_DISABLE_BACKGROUND_TASKS=True)
    def test_does_not_notify_when_background_tasks_disabled(self):
        self.create()
        self.mock_send.assert_not_called()

    def test_does_not_notify_when_updating_existing_bookmark(self):
        self.create()
        self.mock_send.reset_mock()
        self.create()
        self.mock_send.assert_not_called()

    def test_schedules_notification_with_delay(self):
        with (
            mock.patch.object(tasks, "NEW_BOOKMARK_NOTIFICATION_DELAY", 60),
            mock.patch.object(tasks._notify_new_bookmark_task, "schedule") as schedule,
        ):
            bookmark = self.create()
        schedule.assert_called_once_with(args=(bookmark.id,), delay=60)


class NotificationSettingsTestCase(TestCase, BookmarkFactoryMixin):
    def setUp(self):
        self.user = self.get_or_create_test_user()
        self.client.force_login(self.user)

    def test_send_test_notification_uses_unsaved_targets(self):
        with mock.patch.object(notifications, "send") as send:
            response = self.client.post(
                reverse("linkding:settings.update"),
                {
                    "send_test_notification": "",
                    "notification_targets": "https://ntfy.sh/a\njson+https://example.com/b",
                },
                follow=True,
            )
        self.assertEqual(
            [call[0][0] for call in send.call_args_list],
            ["https://ntfy.sh/a", "json+https://example.com/b"],
        )
        self.assertContains(response, "Sent test notification to 2 target(s).")
        self.user.profile.refresh_from_db()
        self.assertEqual(self.user.profile.notification_targets, "")

    def test_send_test_notification_shows_errors(self):
        with mock.patch.object(
            notifications, "send", side_effect=notifications.NotifyError("HTTP 401")
        ):
            response = self.client.post(
                reverse("linkding:settings.update"),
                {
                    "send_test_notification": "",
                    "notification_targets": "https://ntfy.sh/a",
                },
                follow=True,
            )
        self.assertContains(response, "HTTP 401")

    def test_send_test_notification_without_targets(self):
        response = self.client.post(
            reverse("linkding:settings.update"),
            {"send_test_notification": "", "notification_targets": ""},
            follow=True,
        )
        self.assertContains(response, "Add at least one notification target")

    def test_invalid_targets_are_rejected(self):
        from bookmarks.tests.test_settings_general_view import (
            SettingsGeneralViewTestCase,
        )

        form_data = SettingsGeneralViewTestCase.create_profile_form_data(
            self, {"notification_targets": "https://example.com/hook"}
        )
        response = self.client.post(reverse("linkding:settings.update"), form_data)
        self.assertEqual(response.status_code, 422)
        self.assertContains(
            response, "Can&#x27;t detect the notification service", status_code=422
        )

    def test_notification_targets_not_in_admin(self):
        from bookmarks.admin import AdminUserProfileInline

        self.assertIn("notification_targets", AdminUserProfileInline.exclude)
