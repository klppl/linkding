# linkding (modified)

> **This is a modified fork of [linkding](https://github.com/sissbruecker/linkding) by Sascha Ißbrücker.**
> It is not the official project and is not supported by its maintainer.
> Report issues with the additions below here, not upstream.

linkding is a minimal, fast, self-hosted bookmark manager. This fork keeps all of upstream's features
(tags, bulk editing, Markdown notes, sharing, HTML snapshots, browser extensions, REST API, OIDC and more,
see the [official docs](https://linkding.link/)) and adds the features listed below.

## Added in this fork

### AI tagging (bring your own key)

Lets an AI model suggest tags for your bookmarks, using your own Anthropic or OpenAI API key.
Configure it under **Settings → General → AI Tagging**:

- **Provider and API key:** Anthropic (Claude) or OpenAI. Once saved, the key is never shown again.
- **Model:** pick from a list (the default is the provider's cheapest model, `claude-haiku-5-5` or `gpt-5.4-nano`) or enter any model ID.
- **Tag selection:** prefer your existing tags and create new ones only if needed, or only use existing tags.
- **Maximum number of tags** per bookmark (1 to 10), and **custom instructions** added to the prompt.
- **Automatically tag new bookmarks**, optionally skipping bookmarks that already have tags.
- **Tag existing bookmarks** with the "Tag with AI" bulk action, or tag all untagged bookmarks from the settings page.

Tagging runs in the background and only adds tags, it never removes them. The URL, title and description of
each bookmark, and the names of your existing tags, are sent to the selected provider. The API key is stored
in plain text in the linkding database.

### Dead link and duplicate cleanup

linkding already prevents saving the exact same URL twice. This fork adds a link check that runs in the background,
set up under **Settings → General → Link check** (daily, weekly or monthly, or "Run link check now").
Nothing is ever deleted. Instead, bookmarks are tagged so you can review them by searching for the tag:

- **`#dead-link`:** the page returns 404 or 410, or its domain no longer exists.
- **`#duplicate`:** the URL points to the same page as another bookmark, ignoring http/https, `www.`,
  trailing slashes, `#fragments` and tracking parameters like `utm_*` and `fbclid`.

Tags are removed again once a link works or the duplicate is gone. Timeouts, server errors, 403s and similar are
often temporary or caused by bot protection, so they never change the tags. If DNS isn't working during a check,
unknown domains aren't tagged either.

### Notifications (ntfy, Discord, Slack, Gotify, Telegram, webhooks)

Under **Settings → General → Notifications**, add one or more targets (one URL per line) and choose what to be
notified about:

- **New bookmarks:** sent about a minute after a bookmark is added, so its title and tags (including AI tags) are
  included. Clicking the notification opens the page. Imported bookmarks don't send notifications.
- **Link check problems:** sent when the link check finds new dead links or duplicates. Problems that are already
  tagged don't notify again.

| Service | Target URL |
| --- | --- |
| ntfy | `https://ntfy.sh/<topic>` or `ntfy+https://ntfy.example.com/<topic>`, with optional `<access token>@` or `<user>:<password>@` before the host |
| Discord | `https://discord.com/api/webhooks/<id>/<token>` |
| Slack | `https://hooks.slack.com/services/...` |
| Mattermost and other Slack-compatible hooks | `slack+https://mattermost.example.com/hooks/...` |
| Gotify | `gotify+https://gotify.example.com/message?token=<app token>` |
| Telegram | `telegram://<bot token>/<chat id>` |
| Any webhook | `json+https://example.com/hook` (POSTs `{"title", "message", "url"}`) |

Use **Send test notification** to check your targets before saving.

linkding refuses to send requests to servers on your local network by default. If your ntfy or Gotify server runs
on your LAN, allow it with the [`LD_ALLOWED_INTERNAL_HOSTS`](https://linkding.link/options/#ld_allowed_internal_hosts)
option, e.g. `LD_ALLOWED_INTERNAL_HOSTS=ntfy.home.arpa` or `192.168.1.20`.

Both features run in linkding's background task worker, so they don't work if `LD_DISABLE_BACKGROUND_TASKS` is set.

## Running this fork

The official Docker images (`sissbruecker/linkding`) do **not** include these changes.

**Build with GitHub Actions:** open **Actions → Build image → Run workflow** and choose your server's
architecture (`amd64` for Hetzner CX/CPX/CCX, `arm64` for CAX). It builds on a native runner, without
emulation, and reuses cached layers from earlier runs. The image is pushed to the GitHub Container Registry:

```
ghcr.io/<your GitHub user>/linkding:latest        # also tagged with the version and commit, e.g. :sha-1a2b3c4
ghcr.io/<your GitHub user>/linkding:latest-plus   # "plus" variant, includes Chromium for HTML snapshots
```

New packages on GHCR are private, so either make the package public in its settings on GitHub, or log in on the
server with `docker login ghcr.io` using a personal access token with the `read:packages` scope.

**Or build on the server:**

```
docker build -f docker/default.Dockerfile --target linkding -t linkding-modified .
```

Then follow the upstream [installation guide](https://linkding.link/installation), using your image
in place of `sissbruecker/linkding:latest` (for example in `docker-compose.yml`).
Database migrations run automatically on startup.

## Development

Requires Python 3.13, [uv](https://docs.astral.sh/uv/getting-started/installation/) and Node.js.

```
make init        # create the virtual environment, install npm packages, run migrations
uv run manage.py createsuperuser --username=joe --email=joe@example.com
make frontend    # build frontend components
make serve       # start the dev server at http://localhost:8000
```

`make test` runs the tests, `make lint` lints, and `make format` formats the code.

## License

linkding is licensed under the MIT license, see [LICENSE.txt](LICENSE.txt). All credit for linkding itself goes
to its [original author and contributors](https://github.com/sissbruecker/linkding/graphs/contributors).
