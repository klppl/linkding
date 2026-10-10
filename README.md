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

## Running this fork

The official Docker images (`sissbruecker/linkding`) do **not** include these changes. Build your own image:

```
docker build -f docker/default.Dockerfile --target linkding -t linkding-modified .
```

Then follow the upstream [installation guide](https://linkding.link/installation), using `linkding-modified`
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
