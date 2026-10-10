import importlib.util
import io
import socket
import urllib.error
from pathlib import Path
from unittest import mock

from django.test import LiveServerTestCase, SimpleTestCase

from bookmarks.models import Bookmark
from bookmarks.tests.helpers import BookmarkFactoryMixin

# The script lives outside the Django app, load it from its file
_script_path = Path(__file__).resolve().parents[2] / "scripts" / "link_checker.py"
_spec = importlib.util.spec_from_file_location("link_checker", _script_path)
link_checker = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(link_checker)


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

    def test_find_duplicates(self):
        bookmarks = [
            {"id": 1, "url": "https://example.com/a"},
            {"id": 2, "url": "http://www.example.com/a/"},
            {"id": 3, "url": "https://example.com/b"},
        ]
        groups = link_checker.find_duplicates(bookmarks)
        self.assertEqual([[b["id"] for b in g] for g in groups], [[1, 2]])


class CheckLinkTestCase(SimpleTestCase):
    def test_skips_non_http_urls(self):
        result = link_checker.check_link("ftp://example.com/file", 1)
        self.assertEqual(result.status, link_checker.ALIVE)

    def test_domain_not_found_is_dead(self):
        error = urllib.error.URLError(
            socket.gaierror(socket.EAI_NONAME, "Name or service not known")
        )
        with mock.patch.object(
            link_checker.urllib.request, "urlopen", side_effect=error
        ):
            result = link_checker.check_link("https://gone.example/", 1)
        self.assertEqual(result.status, link_checker.DEAD)

    def test_domain_not_found_is_error_if_dns_is_not_trusted(self):
        error = urllib.error.URLError(
            socket.gaierror(socket.EAI_NONAME, "Name or service not known")
        )
        with mock.patch.object(
            link_checker.urllib.request, "urlopen", side_effect=error
        ):
            result = link_checker.check_link(
                "https://gone.example/", 1, trust_dns=False
            )
        self.assertEqual(result.status, link_checker.ERROR)

    def test_dns_works(self):
        with mock.patch.object(link_checker.socket, "getaddrinfo"):
            self.assertTrue(link_checker.dns_works())
        with mock.patch.object(
            link_checker.socket,
            "getaddrinfo",
            side_effect=socket.gaierror(socket.EAI_NONAME, "fail"),
        ):
            self.assertFalse(link_checker.dns_works())

    def test_temporary_dns_failure_is_error(self):
        error = urllib.error.URLError(
            socket.gaierror(socket.EAI_AGAIN, "Temporary failure")
        )
        with mock.patch.object(
            link_checker.urllib.request, "urlopen", side_effect=error
        ):
            result = link_checker.check_link("https://example.com/", 1)
        self.assertEqual(result.status, link_checker.ERROR)

    def test_timeout_is_error(self):
        with mock.patch.object(
            link_checker.urllib.request, "urlopen", side_effect=TimeoutError()
        ):
            result = link_checker.check_link("https://example.com/", 1)
        self.assertEqual(result.status, link_checker.ERROR)

    def test_falls_back_to_get_if_head_fails(self):
        def urlopen(request, timeout):
            if request.get_method() == "HEAD":
                raise urllib.error.HTTPError(request.full_url, 404, "", {}, None)
            response = mock.MagicMock(status=200)
            response.__enter__.return_value = response
            return response

        with mock.patch.object(link_checker.urllib.request, "urlopen", urlopen):
            result = link_checker.check_link("https://example.com/", 1)
        self.assertEqual(result.status, link_checker.ALIVE)


class LinkCheckerIntegrationTestCase(LiveServerTestCase, BookmarkFactoryMixin):
    """Runs the script against a live linkding server, using pages of the same
    server as bookmark targets so that no external network access is needed."""

    def setUp(self):
        dns_patcher = mock.patch.object(link_checker, "dns_works", return_value=True)
        dns_patcher.start()
        self.addCleanup(dns_patcher.stop)

        self.user = self.get_or_create_test_user()
        self.token = self.setup_api_token(self.user)
        self.alive_url = f"{self.live_server_url}/health"
        self.dead_url = f"{self.live_server_url}/does-not-exist"

    def run_script(self, *extra_args):
        args = link_checker.parse_args(
            ["--url", self.live_server_url, "--token", self.token.key, *extra_args]
        )
        api = link_checker.LinkdingApi(args.url, args.token)
        out = io.StringIO()
        exit_code = link_checker.run(args, api, out)
        self.assertEqual(exit_code, 0)
        return out.getvalue()

    def tag_names(self, bookmark: Bookmark):
        bookmark.refresh_from_db()
        return sorted(bookmark.tag_names)

    def test_tags_dead_links_and_duplicates(self):
        alive = self.setup_bookmark(
            url=self.alive_url, tags=[self.setup_tag(name="keep")]
        )
        dead = self.setup_bookmark(url=self.dead_url)
        archived_dead = self.setup_bookmark(
            url=self.dead_url + "-archived", is_archived=True
        )
        duplicate = self.setup_bookmark(url=self.alive_url + "#section")

        output = self.run_script()

        self.assertEqual(self.tag_names(alive), ["duplicate", "keep"])
        self.assertEqual(self.tag_names(dead), ["dead-link"])
        self.assertEqual(self.tag_names(archived_dead), ["dead-link"])
        self.assertEqual(self.tag_names(duplicate), ["duplicate"])
        self.assertIn("[HTTP 404] " + self.dead_url, output)
        self.assertIn(
            "Summary: 2 dead, 0 could not be checked, 1 duplicate groups, 4 bookmarks updated",
            output,
        )

    def test_removes_tags_when_fixed(self):
        fixed = self.setup_bookmark(
            url=self.alive_url,
            tags=[self.setup_tag(name="dead-link"), self.setup_tag(name="duplicate")],
        )

        self.run_script()

        self.assertEqual(self.tag_names(fixed), [])

    def test_keeps_dead_link_tag_when_check_is_inconclusive(self):
        bookmark = self.setup_bookmark(
            url="https://example.com/", tags=[self.setup_tag(name="dead-link")]
        )

        with mock.patch.object(
            link_checker,
            "check_link",
            return_value=link_checker.CheckResult(link_checker.ERROR, "timeout"),
        ):
            output = self.run_script()

        self.assertEqual(self.tag_names(bookmark), ["dead-link"])
        self.assertIn("1 could not be checked", output)

    def test_dry_run_does_not_change_tags(self):
        dead = self.setup_bookmark(url=self.dead_url)

        output = self.run_script("--dry-run")

        self.assertEqual(self.tag_names(dead), [])
        self.assertIn("1 bookmarks would be updated (dry run)", output)

    def test_custom_tags_and_skip_options(self):
        dead = self.setup_bookmark(url=self.dead_url)
        self.setup_bookmark(url=self.dead_url + "/")
        archived_dead = self.setup_bookmark(
            url=self.dead_url + "-archived", is_archived=True
        )

        self.run_script("--dead-tag", "broken", "--no-duplicates", "--skip-archived")

        self.assertEqual(self.tag_names(dead), ["broken"])
        self.assertEqual(self.tag_names(archived_dead), [])

    def test_does_not_touch_other_users_bookmarks(self):
        other_user = self.setup_user()
        other_bookmark = self.setup_bookmark(user=other_user, url=self.dead_url)

        self.run_script()

        self.assertEqual(self.tag_names(other_bookmark), [])

    def test_invalid_token(self):
        exit_code = link_checker.main(
            ["--url", self.live_server_url, "--token", "invalid"]
        )
        self.assertEqual(exit_code, 1)
