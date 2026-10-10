import base64
import importlib.util
import io
import json
import sys
import tempfile
import threading
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

from django.test import LiveServerTestCase, SimpleTestCase
from django.utils import timezone

from bookmarks.tests.helpers import BookmarkFactoryMixin

_scripts_dir = Path(__file__).resolve().parents[2] / "scripts"


def _load_script(name: str):
    spec = importlib.util.spec_from_file_location(name, _scripts_dir / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


notify = _load_script("notify")
new_bookmarks_notifier = _load_script("new_bookmarks_notifier")
link_checker = _load_script("link_checker")


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


class NotifyTestCase(SimpleTestCase):
    def setUp(self):
        self.server = CaptureServer()
        self.addCleanup(self.server.stop)
        self.notification = notify.Notification(
            "Title ✓", "Message", "https://example.com"
        )

    def last_request(self):
        self.assertEqual(len(self.server.requests), 1)
        return self.server.requests[0]

    def test_ntfy(self):
        notify.send(f"ntfy+{self.server.url}/my-topic", self.notification)
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
        notify.send(target, self.notification)
        self.assertEqual(
            self.last_request()["headers"]["Authorization"], "Bearer tk_secret"
        )

    def test_ntfy_basic_auth(self):
        target = f"ntfy+{self.server.url}/topic".replace("://", "://user:pa%40ss@")
        notify.send(target, self.notification)
        expected = base64.b64encode(b"user:pa@ss").decode()
        self.assertEqual(
            self.last_request()["headers"]["Authorization"], f"Basic {expected}"
        )

    def test_ntfy_requires_topic(self):
        with self.assertRaises(notify.NotifyError):
            notify.send(f"ntfy+{self.server.url}/", self.notification)

    def test_discord(self):
        notify.send(f"discord+{self.server.url}/api/webhooks/1/abc", self.notification)
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
        notify.send(f"slack+{self.server.url}/hooks/x", self.notification)
        self.assertEqual(
            self.last_request()["json"],
            {"text": "*<https://example.com|Title ✓>*\nMessage"},
        )

    def test_gotify(self):
        notify.send(f"gotify+{self.server.url}/message?token=abc", self.notification)
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
        notify.send(f"json+{self.server.url}/hook", self.notification)
        self.assertEqual(
            self.last_request()["json"],
            {"title": "Title ✓", "message": "Message", "url": "https://example.com"},
        )

    def test_telegram(self):
        with mock.patch.object(notify, "_post_json") as post_json:
            notify.send("telegram://123:ABC/-100200", self.notification)
        url, payload = post_json.call_args[0]
        self.assertEqual(url, "https://api.telegram.org/bot123:ABC/sendMessage")
        self.assertEqual(payload["chat_id"], "-100200")
        self.assertEqual(payload["text"], "Title ✓\n\nMessage\nhttps://example.com")

    def test_detects_services_from_url(self):
        self.assertEqual(
            notify._detect_service("https://discord.com/api/webhooks/1/x")[0],
            "discord",
        )
        self.assertEqual(
            notify._detect_service("https://hooks.slack.com/services/x")[0], "slack"
        )
        self.assertEqual(notify._detect_service("https://ntfy.sh/topic")[0], "ntfy")
        with self.assertRaises(notify.NotifyError):
            notify._detect_service("https://example.com/hook")
        with self.assertRaises(notify.NotifyError):
            notify._detect_service("unknown+https://example.com/hook")

    def test_http_error(self):
        self.server.status = 500
        with self.assertRaises(notify.NotifyError):
            notify.send(f"json+{self.server.url}/hook", self.notification)

    def test_send_all_returns_errors_without_secrets(self):
        errors = notify.send_all(
            [
                f"json+{self.server.url}/hook",
                "discord+https://user:secret@127.0.0.1:1/api/webhooks/1/token",
            ],
            self.notification,
        )
        self.assertEqual(len(self.server.requests), 1)
        self.assertEqual(len(errors), 1)
        self.assertNotIn("secret", errors[0])
        self.assertNotIn("token", errors[0])

    def test_parse_targets(self):
        self.assertEqual(notify.parse_targets(None), [])
        self.assertEqual(
            notify.parse_targets(" https://ntfy.sh/a\n json+https://b "),
            ["https://ntfy.sh/a", "json+https://b"],
        )


class NewBookmarksNotifierTestCase(LiveServerTestCase, BookmarkFactoryMixin):
    def setUp(self):
        self.server = CaptureServer()
        self.addCleanup(self.server.stop)
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        self.state_file = Path(tmp_dir.name) / "state.json"

        self.user = self.get_or_create_test_user()
        self.token = self.setup_api_token(self.user)

    def run_script(self, *extra_args):
        args = new_bookmarks_notifier.parse_args(
            [
                "--url",
                self.live_server_url,
                "--token",
                self.token.key,
                "--notify",
                f"json+{self.server.url}/hook",
                "--state-file",
                str(self.state_file),
                *extra_args,
            ]
        )
        api = new_bookmarks_notifier.LinkdingApi(args.url, args.token)
        out = io.StringIO()
        exit_code = new_bookmarks_notifier.run(args, api, out)
        return exit_code, out.getvalue()

    def notifications(self):
        return [request["json"] for request in self.server.requests]

    def test_first_run_initializes_without_notifying(self):
        self.setup_bookmark()

        exit_code, output = self.run_script()

        self.assertEqual(exit_code, 0)
        self.assertEqual(self.notifications(), [])
        self.assertTrue(self.state_file.exists())
        self.assertIn("Initialized state", output)

    def test_notifies_about_new_bookmark(self):
        self.setup_bookmark()
        self.run_script()

        self.setup_bookmark(
            url="https://example.com/new",
            title="New one",
            description="Description",
            tags=[self.setup_tag(name="news")],
        )
        exit_code, _ = self.run_script()

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            self.notifications(),
            [
                {
                    "title": "New bookmark: New one",
                    "message": "https://example.com/new\nDescription\n#news",
                    "url": "https://example.com/new",
                }
            ],
        )

        # Nothing new on the next run
        exit_code, output = self.run_script()
        self.assertEqual(len(self.notifications()), 1)
        self.assertIn("No new bookmarks", output)

    def test_summarizes_multiple_bookmarks(self):
        self.run_script()
        for i in range(12):
            self.setup_bookmark(title=f"Bookmark {i}")
        self.setup_bookmark(title="Archived", is_archived=True)

        self.run_script()

        notification = self.notifications()[0]
        self.assertEqual(notification["title"], "13 new bookmarks")
        self.assertIn("• Bookmark 0", notification["message"])
        self.assertIn("…and 3 more", notification["message"])
        self.assertEqual(notification["url"], f"{self.live_server_url}/bookmarks")

    def test_skip_archived(self):
        self.run_script()
        self.setup_bookmark(is_archived=True)

        self.run_script("--skip-archived")

        self.assertEqual(self.notifications(), [])

    def test_ignores_bookmarks_imported_with_older_date(self):
        self.setup_bookmark()
        self.run_script()
        self.setup_bookmark(added=timezone.now() - timedelta(days=365))

        self.run_script()

        self.assertEqual(self.notifications(), [])

    def test_keeps_state_if_notification_fails(self):
        self.run_script()
        self.setup_bookmark(title="Retry me")
        self.server.status = 500

        exit_code, _ = self.run_script()
        self.assertEqual(exit_code, 1)

        self.server.status = 200
        self.server.requests.clear()
        exit_code, _ = self.run_script()
        self.assertEqual(exit_code, 0)
        self.assertEqual(self.notifications()[0]["title"], "New bookmark: Retry me")

    def test_dry_run_does_not_send(self):
        self.run_script()
        self.setup_bookmark(title="Dry")

        _, output = self.run_script("--dry-run")

        self.assertEqual(self.notifications(), [])
        self.assertIn("Would send", output)

    def test_requires_notify_target(self):
        with (
            mock.patch.dict("os.environ", {"LD_NOTIFY": ""}),
            self.assertRaises(SystemExit),
        ):
            new_bookmarks_notifier.parse_args(["--url", "http://x", "--token", "y"])

    def test_reads_targets_from_env(self):
        with mock.patch.dict(
            "os.environ", {"LD_NOTIFY": "https://ntfy.sh/a json+https://b"}
        ):
            args = new_bookmarks_notifier.parse_args(
                ["--url", "http://x", "--token", "y"]
            )
        self.assertEqual(args.notify, ["https://ntfy.sh/a", "json+https://b"])


class LinkCheckerNotificationTestCase(LiveServerTestCase, BookmarkFactoryMixin):
    def setUp(self):
        dns_patcher = mock.patch.object(link_checker, "dns_works", return_value=True)
        dns_patcher.start()
        self.addCleanup(dns_patcher.stop)

        self.server = CaptureServer()
        self.addCleanup(self.server.stop)
        self.user = self.get_or_create_test_user()
        self.token = self.setup_api_token(self.user)
        self.dead_url = f"{self.live_server_url}/does-not-exist"

    def run_script(self, *extra_args):
        args = link_checker.parse_args(
            [
                "--url",
                self.live_server_url,
                "--token",
                self.token.key,
                "--notify",
                f"json+{self.server.url}/hook",
                *extra_args,
            ]
        )
        api = link_checker.LinkdingApi(args.url, args.token)
        return link_checker.run(args, api, io.StringIO())

    def notifications(self):
        return [request["json"] for request in self.server.requests]

    def test_notifies_about_new_problems_once(self):
        self.setup_bookmark(url=self.dead_url, title="Gone")
        self.setup_bookmark(url=f"{self.live_server_url}/health")
        self.setup_bookmark(url=f"{self.live_server_url}/health#a")

        self.assertEqual(self.run_script(), 0)

        notification = self.notifications()[0]
        self.assertEqual(
            notification["title"], "linkding: 1 new dead links, 2 new duplicates"
        )
        self.assertIn("• Gone", notification["message"])
        self.assertIn(
            "In total: 1 dead links, 1 groups of duplicates", notification["message"]
        )
        self.assertEqual(
            notification["url"], f"{self.live_server_url}/bookmarks?q=%23dead-link"
        )

        # Already tagged problems are not notified again
        self.run_script()
        self.assertEqual(len(self.notifications()), 1)

    def test_notify_always(self):
        self.setup_bookmark(url=f"{self.live_server_url}/health")

        self.run_script("--notify-always")

        self.assertEqual(
            self.notifications()[0]["title"], "linkding: link check finished"
        )

    def test_no_notification_without_problems(self):
        self.setup_bookmark(url=f"{self.live_server_url}/health")
        self.run_script()
        self.assertEqual(self.notifications(), [])

    def test_failed_notification_returns_error(self):
        self.server.status = 500
        self.setup_bookmark(url=self.dead_url)
        self.assertEqual(self.run_script(), 1)
