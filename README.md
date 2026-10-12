# bitewise

English | [简体中文](README.zh-CN.md)

A personal food log: snap a photo or send a line of text from your phone → an LLM (Claude, GPT or Gemini, your pick) estimates calories and macros → a web dashboard shows totals, trends and advice. One server can host several people; each gets their own access token and their data stays separate.

The web UI, the AI prompts and the AI's replies are in Chinese.

![bitewise screenshots: today, calendar, trends](docs/screenshot.png)

## How it works

```
iPhone Shortcuts ──HTTPS + Bearer token──▶ reverse proxy (TLS) ──▶ FastAPI server (127.0.0.1)
Browser ──────────HTTPS + login cookie───┘                          ├─ token picks the user, reads/writes data/<user>/
                                                                    ├─ saves right away, a background queue calls the model API
                                                                    ├─ text input: AI decides between log meal / fix a record / log weight
                                                                    ├─ nightly daily summary and advice, per user schedule
                                                                    └─ web dashboard (today / calendar / trends / settings)
```

- Each meal records a description, item breakdown, calories, protein/carbs/fat, and a one-line AI comment
- Meals are sorted into breakfast / lunch / dinner / snack by upload time; the boundaries are adjustable in settings
- Every time and date (upload time, meal slots, "today", the nightly advice) follows each user's timezone setting (default `Europe/London`), not the server's clock
- A free-text "my goal" is injected into every AI call and shapes the comments and advice
- Model provider: Anthropic (default), OpenAI or Google. All users share the API key in `.env`; each user can pick a different model in settings

## Running

Requirements: Python ≥ 3.12, [uv](https://docs.astral.sh/uv/), and an API key for your provider (the model must accept image input).

```bash
uv sync
cp .env.example .env                        # pick a provider and fill in its API key
.venv/bin/python -m bitewise add-user alice # creates a token and prints it once, save it
.venv/bin/python -m bitewise                # listens on 127.0.0.1:8600 by default
```

- `.env`: model provider and API key (see [Models](#models)), plus optional `BITEWISE_HOST` / `BITEWISE_PORT`. It only listens on localhost by default and public access goes through a reverse proxy; set `BITEWISE_HOST=0.0.0.0` only for direct access on a trusted LAN.
- `users.toml`: user name → token, maintained by `add-user` (mode 600). The server refuses to start without users. Restart the server after adding a user.
- `data/<user>/`: each user's SQLite database and original photos. Change the root with `BITEWISE_DATA`.

`.env`, `users.toml` and `data/` are all gitignored.

### Models

`BITEWISE_PROVIDER` in `.env` picks the provider for the whole server; restart after changing it.

| `BITEWISE_PROVIDER` | API key variable | Default model |
|---|---|---|
| `anthropic` (default) | `ANTHROPIC_API_KEY` | `claude-opus-5-5` |
| `openai` | `OPENAI_API_KEY` | `gpt-5.5` |
| `google` | `GEMINI_API_KEY` | `gemini-3.8-flash` (via Gemini's OpenAI-compatible endpoint) |

Each user can enter another model name under Settings → Model; leave it empty to use the default. The model needs image input (meal photos, "can I eat this?"), structured outputs and reasoning effort (`reasoning_effort`). With Anthropic, server-side fallback on refusals is also enabled; other providers don't have it.

### Authentication

Every page and endpoint except the login page and static assets requires authentication:

- Shortcuts: request header `Authorization: Bearer <token>`
- Browser: the site redirects to a login page; the token you enter is stored in an HttpOnly cookie (`Secure` over HTTPS, valid for one year)

Unauthenticated API requests get 401; page requests redirect to `/login`.

### Running under systemd

The repo path in `systemd/bitewise.service` is a placeholder. `systemd/install.sh` fills in the repo's actual location and installs the unit into `~/.config/systemd/user/`, so the repo can live anywhere; rerun it after moving the repo.

```bash
./systemd/install.sh                        # first time, and after moving the repo
loginctl enable-linger "$USER"              # start at boot without logging in
systemctl --user enable --now bitewise
```

### HTTPS reverse proxy (nginx example)

```nginx
server {
    server_name bitewise.example.com;

    # Meal photos from the phone; nginx's 1m default would reject them with 413.
    client_max_body_size 20m;

    location / {
        proxy_pass http://127.0.0.1:8600;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_http_version 1.1;
    }

    listen 80;
}
```

Then run `certbot --nginx -d bitewise.example.com --redirect` to get a certificate and force HTTPS. Keep `X-Forwarded-Proto`: the server uses it to decide whether the cookie gets `Secure`.

## On the phone

See [docs/shortcut.md](docs/shortcut.md) (in Chinese): three iPhone Shortcuts — log a meal from a photo, log text / fix a record / log weight (both hit `/api/upload`), and ask "can I eat this?" before eating (`/api/checks`).

## API summary

Everything except the endpoints marked "public" requires authentication and works on the current user's data.

| Method | Path | Description |
|---|---|---|
| POST | `/api/session` | Public. JSON `{"token": ...}`; sets the login cookie if valid |
| DELETE | `/api/session` | Public. Logs out and clears the cookie |
| GET | `/api/me` | Current user, provider and default model |
| POST | `/api/upload` | multipart: `image` (optional) + `text` (optional) |
| GET | `/api/day/{date}` | A day's meals, totals, advice and weight |
| PATCH / DELETE | `/api/meals/{id}` | Edit / delete a meal |
| POST | `/api/meals/{id}/retry` | Retry a meal whose recognition failed |
| GET | `/api/trends?days=30` | Daily totals + weight series |
| GET / POST / DELETE | `/api/weights` | Weight entries |
| POST | `/api/advice/{date}/generate` | (Re)generate a day's advice |
| GET / PUT | `/api/settings` | Targets, goal prompt, meal boundaries, etc. |
| POST | `/api/checks` | "Can I eat this?": multipart `image`, analyzed asynchronously |
| GET | `/api/checks/{id}` | Check result (including the `summary` the shortcut shows directly) |
| POST | `/api/checks/{id}/eat` | Turn a check into a logged meal |

## Privacy

- Meal photos, text descriptions, and the recent meals and weights used for advice are sent to your chosen provider's API (Anthropic / OpenAI / Google) and fall under that provider's data policy.
- Everything else (SQLite, original photos, tokens) stays on your own server; the project itself collects and reports nothing.

## Development

```bash
uv run pytest
```

A test checks that every route outside the public allowlist has the authentication dependency, so a new endpoint without it fails the suite.

## License

MIT, see [LICENSE](LICENSE).
