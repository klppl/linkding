"""
Suggests tags for bookmarks using an LLM provider (Anthropic or OpenAI).

Users bring their own API key, which is stored in their profile. Requests only
go to the fixed API endpoints of the supported providers, never to user
supplied URLs.
"""

import json
import logging
import re
from dataclasses import dataclass

import requests  # noqa: TID251 - only calls the fixed provider API URLs below

from bookmarks.models import Bookmark, Tag, UserProfile, sanitize_tag_name

logger = logging.getLogger(__name__)

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
OPENAI_API_URL = "https://api.openai.com/v1/chat/completions"

DEFAULT_MODELS = {
    UserProfile.AI_PROVIDER_ANTHROPIC: "claude-haiku-5-5",
    UserProfile.AI_PROVIDER_OPENAI: "gpt-5.4-nano",
}

# Models offered in the settings dropdown, cheapest first. Any other model can
# be entered as a custom model.
MODEL_PRESETS = {
    UserProfile.AI_PROVIDER_ANTHROPIC: [
        "claude-haiku-5-5",
        "claude-haiku-4-5",
        "claude-sonnet-5-5",
    ],
    UserProfile.AI_PROVIDER_OPENAI: [
        "gpt-5.4-nano",
        "gpt-5-nano",
        "gpt-5-mini",
    ],
}

REQUEST_TIMEOUT = 60
# Limit the number of existing tags sent to the model to keep prompts small
MAX_EXISTING_TAGS = 500
MAX_DESCRIPTION_LENGTH = 2000


class AiTaggingError(Exception):
    """Raised for errors that should not be retried, e.g. an invalid API key."""


@dataclass
class AiTaggingSettings:
    provider: str
    api_key: str
    model: str
    tag_mode: str
    max_tags: int
    instructions: str

    @staticmethod
    def from_profile(profile: UserProfile) -> "AiTaggingSettings":
        return AiTaggingSettings(
            provider=profile.ai_provider,
            api_key=profile.ai_api_key,
            model=profile.ai_model.strip() or DEFAULT_MODELS[profile.ai_provider],
            tag_mode=profile.ai_tag_mode,
            max_tags=profile.ai_max_tags,
            instructions=profile.ai_instructions,
        )


def is_configured(profile: UserProfile) -> bool:
    return bool(profile.ai_api_key) and profile.ai_provider in DEFAULT_MODELS


def suggest_tags(bookmark: Bookmark, settings: AiTaggingSettings) -> list[str]:
    existing_tags = list(
        Tag.objects.filter(owner=bookmark.owner)
        .order_by("name")
        .values_list("name", flat=True)[:MAX_EXISTING_TAGS]
    )
    system_prompt = _build_system_prompt(settings, existing_tags)
    user_prompt = _build_user_prompt(bookmark)

    if settings.provider == UserProfile.AI_PROVIDER_ANTHROPIC:
        text = _call_anthropic(settings, system_prompt, user_prompt)
    elif settings.provider == UserProfile.AI_PROVIDER_OPENAI:
        text = _call_openai(settings, system_prompt, user_prompt)
    else:
        raise AiTaggingError(f"Unknown AI provider: {settings.provider}")

    tags = _parse_tags(text)

    if settings.tag_mode == UserProfile.AI_TAG_MODE_EXISTING_ONLY:
        # Match case-insensitively, but use the name of the existing tag
        existing_lookup = {name.lower(): name for name in existing_tags}
        tags = [
            existing_lookup[t.lower()] for t in tags if t.lower() in existing_lookup
        ]

    # Remove duplicates while keeping order
    unique_tags = []
    for tag in tags:
        if tag.lower() not in [t.lower() for t in unique_tags]:
            unique_tags.append(tag)

    return unique_tags[: settings.max_tags]


def _build_system_prompt(settings: AiTaggingSettings, existing_tags: list[str]):
    lines = [
        "You categorize bookmarks by assigning tags to them.",
        f"Assign between 1 and {settings.max_tags} tags that describe the topic of the bookmark.",
        "Tags must be single words or use hyphens instead of spaces, and should be lowercase unless the user's existing tags use a different style.",
    ]
    if settings.tag_mode == UserProfile.AI_TAG_MODE_EXISTING_ONLY:
        lines.append(
            "Only use tags from the list of existing tags. If none fit, return an empty list."
        )
    else:
        lines.append(
            "Prefer tags from the list of existing tags. Only create a new tag if none of the existing tags fit."
        )
    if existing_tags:
        lines.append("Existing tags: " + ", ".join(existing_tags))
    else:
        lines.append("There are no existing tags yet.")
    if settings.instructions.strip():
        lines.append("Additional instructions from the user:")
        lines.append(settings.instructions.strip())
    lines.append(
        'Respond with only a JSON object in the form {"tags": ["tag1", "tag2"]} and nothing else.'
    )
    return "\n".join(lines)


def _build_user_prompt(bookmark: Bookmark):
    lines = [f"URL: {bookmark.url}"]
    if bookmark.title:
        lines.append(f"Title: {bookmark.title}")
    if bookmark.description:
        lines.append(f"Description: {bookmark.description[:MAX_DESCRIPTION_LENGTH]}")
    return "\n".join(lines)


def _call_anthropic(settings: AiTaggingSettings, system_prompt: str, user_prompt: str):
    response = requests.post(
        ANTHROPIC_API_URL,
        headers={
            "x-api-key": settings.api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": settings.model,
            "max_tokens": 1024,
            "output_config": {"effort": "low"},
            "system": system_prompt,
            "messages": [{"role": "user", "content": user_prompt}],
        },
        timeout=REQUEST_TIMEOUT,
    )
    _raise_for_status(response, "Anthropic")
    data = response.json()
    return "".join(
        block.get("text", "")
        for block in data.get("content", [])
        if block.get("type") == "text"
    )


def _call_openai(settings: AiTaggingSettings, system_prompt: str, user_prompt: str):
    response = requests.post(
        OPENAI_API_URL,
        headers={
            "Authorization": f"Bearer {settings.api_key}",
            "content-type": "application/json",
        },
        json={
            "model": settings.model,
            "max_completion_tokens": 2048,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        },
        timeout=REQUEST_TIMEOUT,
    )
    _raise_for_status(response, "OpenAI")
    data = response.json()
    choices = data.get("choices") or []
    if not choices:
        return ""
    return choices[0].get("message", {}).get("content") or ""


def _raise_for_status(response, provider: str):
    if response.ok:
        return
    message = f"{provider} API request failed with status {response.status_code}"
    try:
        error = response.json().get("error", {})
        if isinstance(error, dict) and error.get("message"):
            message += f": {error['message']}"
    except ValueError:
        pass
    # Client errors (invalid key, unknown model, ...) won't succeed on retry,
    # except for rate limiting
    if 400 <= response.status_code < 500 and response.status_code != 429:
        raise AiTaggingError(message)
    raise requests.HTTPError(message, response=response)


def _parse_tags(text: str) -> list[str]:
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        logger.warning(f"AI tagging returned unexpected response: {text[:200]}")
        return []
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        logger.warning(f"AI tagging returned invalid JSON: {text[:200]}")
        return []
    raw_tags = data.get("tags") if isinstance(data, dict) else None
    if not isinstance(raw_tags, list):
        return []

    tags = []
    for raw_tag in raw_tags:
        if not isinstance(raw_tag, str):
            continue
        # Commas are used as tag delimiter, so they can't be part of a tag name
        tag = sanitize_tag_name(raw_tag.replace(",", ""))
        if tag:
            tags.append(tag[:64])
    return tags
