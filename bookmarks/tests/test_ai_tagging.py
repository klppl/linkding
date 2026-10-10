from unittest import mock

from django.test import TestCase, override_settings
from huey.contrib.djhuey import HUEY as huey

from bookmarks.models import Bookmark, UserProfile
from bookmarks.services import ai_tagging, tasks
from bookmarks.services.bookmarks import ai_tag_bookmarks, create_bookmark
from bookmarks.services.website_loader import WebsiteMetadata
from bookmarks.tests.helpers import BookmarkFactoryMixin


def mock_response(status_code=200, json_data=None):
    response = mock.Mock()
    response.status_code = status_code
    response.ok = 200 <= status_code < 300
    response.json.return_value = json_data or {}
    return response


def anthropic_response(text: str):
    return mock_response(
        json_data={
            "content": [
                {"type": "thinking", "thinking": ""},
                {"type": "text", "text": text},
            ]
        }
    )


def openai_response(text: str):
    return mock_response(json_data={"choices": [{"message": {"content": text}}]})


class AiTaggingServiceTestCase(TestCase, BookmarkFactoryMixin):
    def setUp(self):
        self.user = self.get_or_create_test_user()
        self.user.profile.ai_api_key = "test-key"
        self.user.profile.save()

        self.post_patcher = mock.patch.object(ai_tagging.requests, "post")
        self.mock_post = self.post_patcher.start()

    def tearDown(self):
        self.post_patcher.stop()

    def get_settings(self):
        return ai_tagging.AiTaggingSettings.from_profile(self.user.profile)

    def test_is_configured(self):
        profile = UserProfile(ai_api_key="")
        self.assertFalse(ai_tagging.is_configured(profile))
        profile.ai_api_key = "key"
        self.assertTrue(ai_tagging.is_configured(profile))

    def test_uses_default_model(self):
        self.assertEqual(
            self.get_settings().model,
            ai_tagging.DEFAULT_MODELS[UserProfile.AI_PROVIDER_ANTHROPIC],
        )
        self.user.profile.ai_provider = UserProfile.AI_PROVIDER_OPENAI
        self.assertEqual(
            self.get_settings().model,
            ai_tagging.DEFAULT_MODELS[UserProfile.AI_PROVIDER_OPENAI],
        )
        self.user.profile.ai_model = "custom-model"
        self.assertEqual(self.get_settings().model, "custom-model")

    def test_anthropic_request(self):
        self.mock_post.return_value = anthropic_response('{"tags": ["python"]}')
        self.setup_tag(name="programming")
        self.user.profile.ai_instructions = "Use lowercase"
        bookmark = self.setup_bookmark(
            url="https://python.org", title="Python", description="A language"
        )

        tags = ai_tagging.suggest_tags(bookmark, self.get_settings())

        self.assertEqual(tags, ["python"])
        args, kwargs = self.mock_post.call_args
        self.assertEqual(args[0], ai_tagging.ANTHROPIC_API_URL)
        self.assertEqual(kwargs["headers"]["x-api-key"], "test-key")
        body = kwargs["json"]
        self.assertEqual(body["model"], "claude-haiku-5-5")
        self.assertIn("programming", body["system"])
        self.assertIn("Use lowercase", body["system"])
        user_prompt = body["messages"][0]["content"]
        self.assertIn("https://python.org", user_prompt)
        self.assertIn("Python", user_prompt)
        self.assertIn("A language", user_prompt)

    def test_openai_request(self):
        self.user.profile.ai_provider = UserProfile.AI_PROVIDER_OPENAI
        self.mock_post.return_value = openai_response('{"tags": ["news"]}')
        bookmark = self.setup_bookmark()

        tags = ai_tagging.suggest_tags(bookmark, self.get_settings())

        self.assertEqual(tags, ["news"])
        args, kwargs = self.mock_post.call_args
        self.assertEqual(args[0], ai_tagging.OPENAI_API_URL)
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer test-key")
        self.assertEqual(kwargs["json"]["model"], "gpt-5.4-nano")

    def test_parses_and_sanitizes_tags(self):
        self.mock_post.return_value = anthropic_response(
            'Sure: {"tags": ["web dev", "a,b", "", 42, "Web-Dev", "python"]}'
        )
        bookmark = self.setup_bookmark()
        self.user.profile.ai_max_tags = 10

        tags = ai_tagging.suggest_tags(bookmark, self.get_settings())

        self.assertEqual(tags, ["web-dev", "ab", "python"])

    def test_limits_number_of_tags(self):
        self.mock_post.return_value = anthropic_response(
            '{"tags": ["a", "b", "c", "d"]}'
        )
        self.user.profile.ai_max_tags = 2
        tags = ai_tagging.suggest_tags(self.setup_bookmark(), self.get_settings())
        self.assertEqual(tags, ["a", "b"])

    def test_existing_only_mode(self):
        self.setup_tag(name="Python")
        self.mock_post.return_value = anthropic_response(
            '{"tags": ["python", "new-tag"]}'
        )
        self.user.profile.ai_tag_mode = UserProfile.AI_TAG_MODE_EXISTING_ONLY

        tags = ai_tagging.suggest_tags(self.setup_bookmark(), self.get_settings())

        self.assertEqual(tags, ["Python"])

    def test_invalid_response(self):
        self.mock_post.return_value = anthropic_response("not json")
        tags = ai_tagging.suggest_tags(self.setup_bookmark(), self.get_settings())
        self.assertEqual(tags, [])

    def test_client_error_raises_ai_tagging_error(self):
        self.mock_post.return_value = mock_response(
            401, {"error": {"message": "invalid x-api-key"}}
        )
        with self.assertRaises(ai_tagging.AiTaggingError) as context:
            ai_tagging.suggest_tags(self.setup_bookmark(), self.get_settings())
        self.assertIn("invalid x-api-key", str(context.exception))

    def test_server_error_raises_retryable_error(self):
        self.mock_post.return_value = mock_response(529)
        with self.assertRaises(ai_tagging.requests.HTTPError):
            ai_tagging.suggest_tags(self.setup_bookmark(), self.get_settings())


class AiTaggingTaskTestCase(TestCase, BookmarkFactoryMixin):
    def setUp(self):
        huey.immediate = True
        huey.results = True
        huey.store_none = True

        self.user = self.get_or_create_test_user()
        self.user.profile.ai_api_key = "test-key"
        self.user.profile.save()

        self.suggest_patcher = mock.patch.object(ai_tagging, "suggest_tags")
        self.mock_suggest_tags = self.suggest_patcher.start()
        self.mock_suggest_tags.return_value = ["ai-tag"]

        self.metadata_patcher = mock.patch.object(tasks, "load_website_metadata")
        self.mock_load_metadata = self.metadata_patcher.start()
        self.mock_load_metadata.return_value = WebsiteMetadata(
            url="https://example.com",
            title="Website title",
            description="Website description",
            preview_image=None,
        )

        # Avoid running unrelated background tasks
        self.other_tasks_patchers = [
            mock.patch.object(tasks, name)
            for name in [
                "create_web_archive_snapshot",
                "load_favicon",
                "load_preview_image",
                "create_html_snapshot",
            ]
        ]
        for patcher in self.other_tasks_patchers:
            patcher.start()

    def tearDown(self):
        self.suggest_patcher.stop()
        self.metadata_patcher.stop()
        for patcher in self.other_tasks_patchers:
            patcher.stop()
        huey.storage.flush_results()
        huey.immediate = False

    def tag_names(self, bookmark: Bookmark):
        return sorted(tag.name for tag in bookmark.tags.all())

    def test_adds_tags_to_bookmark(self):
        bookmark = self.setup_bookmark(tags=[self.setup_tag(name="existing")])

        tasks.ai_tag_bookmark(self.user, bookmark)

        self.assertEqual(self.tag_names(bookmark), ["ai-tag", "existing"])

    @override_settings(LD_DISABLE_BACKGROUND_TASKS=True)
    def test_does_nothing_when_background_tasks_disabled(self):
        bookmark = self.setup_bookmark()
        tasks.ai_tag_bookmark(self.user, bookmark)
        self.mock_suggest_tags.assert_not_called()

    def test_does_nothing_without_api_key(self):
        self.user.profile.ai_api_key = ""
        self.user.profile.save()
        bookmark = self.setup_bookmark()

        tasks.ai_tag_bookmark(self.user, bookmark)

        self.mock_suggest_tags.assert_not_called()

    def test_loads_website_metadata_if_bookmark_has_no_title_or_description(self):
        bookmark = self.setup_bookmark(title="", description="")

        tasks.ai_tag_bookmark(self.user, bookmark)

        self.mock_load_metadata.assert_called_once_with(bookmark.url)
        passed_bookmark = self.mock_suggest_tags.call_args[0][0]
        self.assertEqual(passed_bookmark.title, "Website title")
        # Metadata is only used for the prompt, not stored
        bookmark.refresh_from_db()
        self.assertEqual(bookmark.title, "")

    def test_does_not_load_website_metadata_if_bookmark_has_title(self):
        bookmark = self.setup_bookmark(title="Title")
        tasks.ai_tag_bookmark(self.user, bookmark)
        self.mock_load_metadata.assert_not_called()

    def test_handles_configuration_errors(self):
        self.mock_suggest_tags.side_effect = ai_tagging.AiTaggingError("invalid key")
        bookmark = self.setup_bookmark()

        tasks.ai_tag_bookmark(self.user, bookmark)

        self.assertEqual(self.mock_suggest_tags.call_count, 1)
        self.assertEqual(self.tag_names(bookmark), [])

    def test_create_bookmark_triggers_ai_tagging(self):
        self.user.profile.enable_ai_tagging = True
        self.user.profile.save()

        bookmark = create_bookmark(
            Bookmark(url="https://example.com", title="Example"), "", self.user
        )

        self.assertEqual(self.tag_names(bookmark), ["ai-tag"])

    def test_create_bookmark_does_not_trigger_ai_tagging_when_disabled(self):
        self.user.profile.enable_ai_tagging = False
        self.user.profile.save()

        create_bookmark(Bookmark(url="https://example.com"), "", self.user)

        self.mock_suggest_tags.assert_not_called()

    def test_create_bookmark_skips_tagged_bookmarks_if_only_untagged(self):
        self.user.profile.enable_ai_tagging = True
        self.user.profile.ai_only_untagged = True
        self.user.profile.save()

        bookmark = create_bookmark(
            Bookmark(url="https://example.com"), "manual", self.user
        )

        self.mock_suggest_tags.assert_not_called()
        self.assertEqual(self.tag_names(bookmark), ["manual"])

    def test_create_bookmark_tags_tagged_bookmarks_if_not_only_untagged(self):
        self.user.profile.enable_ai_tagging = True
        self.user.profile.ai_only_untagged = False
        self.user.profile.save()

        bookmark = create_bookmark(
            Bookmark(url="https://example.com"), "manual", self.user
        )

        self.assertEqual(self.tag_names(bookmark), ["ai-tag", "manual"])

    def test_ai_tag_bookmarks_only_tags_owned_bookmarks(self):
        own_bookmark = self.setup_bookmark()
        other_user = self.setup_user()
        other_user.profile.ai_api_key = "other-key"
        other_user.profile.save()
        other_bookmark = self.setup_bookmark(user=other_user)

        ai_tag_bookmarks([own_bookmark.id, other_bookmark.id], self.user)

        self.assertEqual(self.tag_names(own_bookmark), ["ai-tag"])
        self.assertEqual(self.tag_names(other_bookmark), [])

    def test_schedule_ai_tag_untagged_bookmarks(self):
        untagged = self.setup_bookmark()
        tagged = self.setup_bookmark(tags=[self.setup_tag(name="existing")])

        count = tasks.schedule_ai_tag_untagged_bookmarks(self.user)

        self.assertEqual(count, 1)
        self.assertEqual(self.tag_names(untagged), ["ai-tag"])
        self.assertEqual(self.tag_names(tagged), ["existing"])
