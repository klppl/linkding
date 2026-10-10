import socket
from datetime import timedelta
from unittest import mock

from django.test import LiveServerTestCase, SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from huey.contrib.djhuey import HUEY as huey

from bookmarks.models import Bookmark, UserProfile
from bookmarks.services import http_client, link_checker, notifications, tasks
from bookmarks.tests.helpers import BookmarkFactoryMixin

ALIVE = link_checker.CheckResult(link_checker.ALIVE, "HTTP 200")
DEAD = link_checker.CheckResult(link_checker.DEAD, "HTTP 404")
ERROR = link_checker.CheckResult(link_checker.ERROR, "timeout")


def mock_check_link(results: dict):
    """Returns a check_link replacement that looks up results by URL."""
    return lambda url, trust_dns=True: results.get(url, ALIVE)


class NormalizeUrlTestCase(SimpleTestCase):
    def assertSameKey(self, url1, url2):
        self.assertEqual(
            link_checker.normalize_url(url1), link_checker.normalize_url(url2)
        )

    def assertDifferentKey(self, url1, url2):
        self.assertNotEqual(
            link_checker.normalize_url(url1), link_checker.normalize_url(url2)
        )

    def test_equivalent_urls(self):
        base = "https://example.com/path"
        self.assertSameKey(base, "http://example.com/path")
        self.assertSameKey(base, "https://www.example.com/path")
        self.assertSameKey(base, "https://EXAMPLE.com/path/")
        self.assertSameKey(base, "https://example.com:443/path")
        self.assertSameKey(base, "https://example.com/path#section")
        self.assertSameKey(base, "https://example.com/path/index.html")
        self.assertSameKey(base, "https://example.com/path?utm_source=x&fbclid=y")
        self.assertSameKey(
            "https://example.com/?a=1&b=2", "https://example.com/?b=2&a=1"
        )

    def test_different_urls(self):
        base = "https://example.com/path"
        self.assertDifferentKey(base, "https://example.com/other")
        self.assertDifferentKey(base, "https://example.org/path")
        self.assertDifferentKey(base, "https://sub.example.com/path")
        self.assertDifferentKey(base, "https://example.com/path?id=1")
        self.assertDifferentKey(base, "https://example.com/PATH")
        self.assertDifferentKey(base, "https://example.com:8080/path")

    def test_invalid_url(self):
        self.assertEqual(
            link_checker.normalize_url("https://example.com:99999/"),
            "https://example.com:99999/",
        )


class CheckLinkTestCase(SimpleTestCase):
    def mock_request(self, side_effect):
        patcher = mock.patch.object(http_client, "request", side_effect=side_effect)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def response(self, status_code):
        return mock.Mock(status_code=status_code)

    def test_skips_non_http_urls(self):
        request = self.mock_request(None)
        result = link_checker.check_link("ftp://example.com/file")
        self.assertEqual(result.status, link_checker.ALIVE)
        request.assert_not_called()

    def test_status_codes(self):
        for status_code, expected in [
            (200, link_checker.ALIVE),
            (301, link_checker.ALIVE),
            (404, link_checker.DEAD),
            (410, link_checker.DEAD),
            (403, link_checker.ERROR),
            (429, link_checker.ERROR),
            (500, link_checker.ERROR),
        ]:
            self.mock_request(lambda *args, s=status_code, **kwargs: self.response(s))
            result = link_checker.check_link("https://example.com/")
            self.assertEqual(result.status, expected, status_code)

    def test_falls_back_to_get_if_head_fails(self):
        def request(method, url, **kwargs):
            return self.response(405 if method == "HEAD" else 200)

        self.mock_request(request)
        self.assertEqual(
            link_checker.check_link("https://example.com/").status, link_checker.ALIVE
        )

    def test_keeps_dead_result_from_head_if_get_errors(self):
        def request(method, url, **kwargs):
            if method == "HEAD":
                return self.response(404)
            raise http_client.RequestTimeout()

        self.mock_request(request)
        self.assertEqual(
            link_checker.check_link("https://example.com/").status, link_checker.DEAD
        )

    def domain_not_found_error(self):
        # Mimics how requests wraps DNS errors
        try:
            try:
                raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")
            except socket.gaierror as e:
                raise http_client.RequestConnectionError("Max retries exceeded") from e
        except http_client.RequestConnectionError as error:
            return error

    def test_domain_not_found_is_dead(self):
        self.mock_request(self.domain_not_found_error())
        result = link_checker.check_link("https://gone.example/")
        self.assertEqual(result.status, link_checker.DEAD)
        self.assertEqual(result.detail, "domain not found")

    def test_domain_not_found_is_error_if_dns_is_not_trusted(self):
        self.mock_request(self.domain_not_found_error())
        result = link_checker.check_link("https://gone.example/", trust_dns=False)
        self.assertEqual(result.status, link_checker.ERROR)

    def test_errors(self):
        for error in [
            http_client.RequestTimeout(),
            http_client.RequestConnectionError("refused"),
            http_client.BlockedAddressError("internal", None),
            socket.gaierror(socket.EAI_AGAIN, "Temporary failure"),
        ]:
            self.mock_request(error)
            result = link_checker.check_link("https://example.com/")
            self.assertEqual(result.status, link_checker.ERROR, error)

    def test_dns_works(self):
        with mock.patch.object(link_checker.socket, "getaddrinfo"):
            self.assertTrue(link_checker.dns_works())
        with mock.patch.object(
            link_checker.socket, "getaddrinfo", side_effect=socket.gaierror()
        ):
            self.assertFalse(link_checker.dns_works())


class RunLinkCheckTestCase(TestCase, BookmarkFactoryMixin):
    def setUp(self):
        self.user = self.get_or_create_test_user()
        dns_patcher = mock.patch.object(link_checker, "dns_works", return_value=True)
        dns_patcher.start()
        self.addCleanup(dns_patcher.stop)

    def run_check(self, results: dict):
        with mock.patch.object(
            link_checker, "check_link", side_effect=mock_check_link(results)
        ):
            return link_checker.run_link_check(self.user)

    def tag_names(self, bookmark: Bookmark):
        bookmark.refresh_from_db()
        return sorted(bookmark.tag_names)

    def test_tags_dead_links_and_duplicates(self):
        alive = self.setup_bookmark(
            url="https://example.com/a", tags=[self.setup_tag(name="keep")]
        )
        duplicate = self.setup_bookmark(url="http://www.example.com/a/")
        dead = self.setup_bookmark(url="https://example.com/dead")
        archived_dead = self.setup_bookmark(
            url="https://example.com/archived", is_archived=True
        )
        date_modified = dead.date_modified

        result = self.run_check(
            {"https://example.com/dead": DEAD, "https://example.com/archived": DEAD}
        )

        self.assertEqual(self.tag_names(alive), ["duplicate", "keep"])
        self.assertEqual(self.tag_names(duplicate), ["duplicate"])
        self.assertEqual(self.tag_names(dead), ["dead-link"])
        self.assertEqual(self.tag_names(archived_dead), ["dead-link"])
        self.assertEqual(result.checked, 4)
        self.assertCountEqual(result.new_dead, [dead, archived_dead])
        self.assertCountEqual(result.new_duplicates, [alive, duplicate])
        self.assertEqual(len(result.duplicate_groups), 1)
        self.assertTrue(result.has_new_problems)
        # Tagging does not change the modification date
        self.assertEqual(dead.date_modified, date_modified)

    def test_does_not_report_already_tagged_problems_again(self):
        dead = self.setup_bookmark(url="https://example.com/dead")
        self.run_check({"https://example.com/dead": DEAD})

        result = self.run_check({"https://example.com/dead": DEAD})

        self.assertEqual(result.dead, [dead])
        self.assertEqual(result.new_dead, [])
        self.assertFalse(result.has_new_problems)

    def test_removes_tags_when_fixed(self):
        bookmark = self.setup_bookmark(
            url="https://example.com/",
            tags=[self.setup_tag(name="Dead-Link"), self.setup_tag(name="duplicate")],
        )

        self.run_check({})

        self.assertEqual(self.tag_names(bookmark), [])

    def test_keeps_dead_link_tag_when_check_is_inconclusive(self):
        bookmark = self.setup_bookmark(
            url="https://example.com/", tags=[self.setup_tag(name="dead-link")]
        )

        result = self.run_check({"https://example.com/": ERROR})

        self.assertEqual(self.tag_names(bookmark), ["dead-link"])
        self.assertEqual(result.errors, 1)

    def test_only_checks_own_bookmarks(self):
        other_bookmark = self.setup_bookmark(
            user=self.setup_user(), url="https://example.com/dead"
        )
        result = self.run_check({"https://example.com/dead": DEAD})
        self.assertEqual(result.checked, 0)
        self.assertEqual(self.tag_names(other_bookmark), [])

    def test_build_notification(self):
        dead = self.setup_bookmark(url="https://example.com/dead", title="Gone")
        self.setup_bookmark(url="https://example.com/a")
        self.setup_bookmark(url="https://example.com/a/")

        result = self.run_check({"https://example.com/dead": DEAD})
        notification = link_checker.build_notification(result)

        self.assertEqual(
            notification.title, "linkding: 1 new dead links, 2 new duplicates"
        )
        self.assertIn("• Gone", notification.message)
        self.assertIn("• https://example.com/a/", notification.message)
        self.assertIn("1 dead links, 1 groups of duplicates", notification.message)
        self.assertIsNone(notification.url)
        self.assertEqual(result.new_dead, [dead])


class LinkCheckTaskTestCase(TestCase, BookmarkFactoryMixin):
    def setUp(self):
        huey.immediate = True
        self.addCleanup(setattr, huey, "immediate", False)
        self.user = self.get_or_create_test_user()

        self.run_patcher = mock.patch.object(link_checker, "run_link_check")
        self.mock_run = self.run_patcher.start()
        self.addCleanup(self.run_patcher.stop)
        bookmark = self.setup_bookmark()
        self.mock_run.return_value = link_checker.LinkCheckResult(
            checked=1, dead=[bookmark], new_dead=[bookmark]
        )
        self.notify_patcher = mock.patch.object(notifications, "send_to_user")
        self.mock_notify = self.notify_patcher.start()
        self.addCleanup(self.notify_patcher.stop)

    def set_profile(self, **values):
        UserProfile.objects.filter(user=self.user).update(**values)
        self.user.profile.refresh_from_db()

    def test_schedule_link_check_runs_check_and_notifies(self):
        self.set_profile(notification_targets="https://ntfy.sh/a")

        self.assertTrue(tasks.schedule_link_check(self.user))

        self.mock_run.assert_called_once()
        self.mock_notify.assert_called_once()
        self.user.profile.refresh_from_db()
        self.assertIsNotNone(self.user.profile.link_check_last_run)

    def test_does_not_notify_without_new_problems(self):
        self.set_profile(notification_targets="https://ntfy.sh/a")
        self.mock_run.return_value = link_checker.LinkCheckResult(checked=1)
        tasks.schedule_link_check(self.user)
        self.mock_notify.assert_not_called()

    def test_does_not_notify_when_disabled(self):
        self.set_profile(
            notification_targets="https://ntfy.sh/a", notify_link_check=False
        )
        tasks.schedule_link_check(self.user)
        self.mock_run.assert_called_once()
        self.mock_notify.assert_not_called()

    @override_settings(LD_DISABLE_BACKGROUND_TASKS=True)
    def test_does_not_run_when_background_tasks_disabled(self):
        self.assertFalse(tasks.schedule_link_check(self.user))
        self.mock_run.assert_not_called()

    def test_skips_if_check_is_already_running(self):
        with huey.lock_task(f"link-check-{self.user.id}"):
            tasks.schedule_link_check(self.user)
        self.mock_run.assert_not_called()

    def test_periodic_task_schedules_due_checks(self):
        now = timezone.now()
        cases = [
            (UserProfile.LINK_CHECK_DISABLED, None, False),
            (UserProfile.LINK_CHECK_DAILY, None, True),
            (UserProfile.LINK_CHECK_DAILY, now - timedelta(hours=23), False),
            (UserProfile.LINK_CHECK_DAILY, now - timedelta(days=1, minutes=1), True),
            (UserProfile.LINK_CHECK_WEEKLY, now - timedelta(days=6), False),
            (UserProfile.LINK_CHECK_WEEKLY, now - timedelta(days=7), True),
            (UserProfile.LINK_CHECK_MONTHLY, now - timedelta(days=29), False),
            (UserProfile.LINK_CHECK_MONTHLY, now - timedelta(days=30), True),
        ]
        for interval, last_run, expected in cases:
            self.mock_run.reset_mock()
            self.set_profile(link_check_interval=interval, link_check_last_run=last_run)

            tasks._schedule_due_link_checks_task.call_local()

            self.assertEqual(
                self.mock_run.called, expected, f"{interval}, last run {last_run}"
            )

    def test_periodic_task_skips_inactive_users(self):
        self.set_profile(link_check_interval=UserProfile.LINK_CHECK_DAILY)
        self.user.is_active = False
        self.user.save()

        tasks._schedule_due_link_checks_task.call_local()

        self.mock_run.assert_not_called()


class LinkCheckSettingsTestCase(TestCase, BookmarkFactoryMixin):
    def setUp(self):
        self.user = self.get_or_create_test_user()
        self.client.force_login(self.user)

    def test_run_link_check_button(self):
        with mock.patch.object(tasks, "_link_check_task") as link_check_task:
            response = self.client.post(
                reverse("linkding:settings.update"), {"run_link_check": ""}, follow=True
            )
        link_check_task.assert_called_once_with(self.user.id)
        self.assertContains(response, "Link check started.")

    @override_settings(LD_DISABLE_BACKGROUND_TASKS=True)
    def test_run_link_check_button_without_background_tasks(self):
        response = self.client.post(
            reverse("linkding:settings.update"), {"run_link_check": ""}, follow=True
        )
        self.assertContains(response, "requires background tasks")
        self.assertNotContains(response, 'name="run_link_check"')


class LinkCheckLiveTestCase(LiveServerTestCase, BookmarkFactoryMixin):
    """Checks real HTTP requests against pages of the live test server."""

    def setUp(self):
        override = override_settings(LD_ALLOWED_INTERNAL_HOSTS="localhost,127.0.0.1")
        override.enable()
        self.addCleanup(override.disable)
        http_client.get_allowlist.cache_clear()
        self.addCleanup(http_client.get_allowlist.cache_clear)
        dns_patcher = mock.patch.object(link_checker, "dns_works", return_value=True)
        dns_patcher.start()
        self.addCleanup(dns_patcher.stop)

    def test_check_link(self):
        self.assertEqual(
            link_checker.check_link(f"{self.live_server_url}/health").status,
            link_checker.ALIVE,
        )
        self.assertEqual(
            link_checker.check_link(f"{self.live_server_url}/does-not-exist").status,
            link_checker.DEAD,
        )

    def test_run_link_check(self):
        user = self.get_or_create_test_user()
        alive = self.setup_bookmark(url=f"{self.live_server_url}/health")
        dead = self.setup_bookmark(url=f"{self.live_server_url}/does-not-exist")

        link_checker.run_link_check(user)

        alive.refresh_from_db()
        dead.refresh_from_db()
        self.assertEqual(alive.tag_names, [])
        self.assertEqual(dead.tag_names, ["dead-link"])

    def test_internal_hosts_are_not_tagged_by_default(self):
        http_client.get_allowlist.cache_clear()
        with override_settings(LD_ALLOWED_INTERNAL_HOSTS=""):
            http_client.get_allowlist.cache_clear()
            result = link_checker.check_link(f"{self.live_server_url}/does-not-exist")
        self.assertEqual(result.status, link_checker.ERROR)
