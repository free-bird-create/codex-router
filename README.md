# codex-router

A tiny, zero-dependency local router that lets the Linux ChatGPT desktop app (Codex) switch between multiple OpenAI Responses API providers from a single model dropdown.

## Features

- **Single-file Python stdlib router** (`router.py`): routes `/v1/responses` by `model` name, swaps `Authorization` headers, and streams SSE chunks through untouched.
- **Reasoning effort clamping**: maps higher effort levels (e.g. `xhigh`, `max`, `ultra`) down to the highest level supported by the target model.
- **On-demand startup**: runs via `systemd --user` socket activation on `127.0.0.1:4000` and exits automatically after idle timeout (default 15 minutes).
- **Single source of truth**: `models.toml` drives both routing (`router.py`) and Codex catalog generation (`build_catalog.py`).
- **Secret isolation**: API keys live in `~/.config/codex-router/secrets.env` (`chmod 600`), read only by the router process.

## Layout

```text
router.py                 Core router (config, pure functions, streaming proxy, lifecycle)
build_catalog.py          Generates ~/.codex/codex-models.json from base catalog + models.toml
test_router.py            Unit & local end-to-end tests (python3 -m unittest)
models.example.toml       Example provider/model config (no secrets)
systemd/                  User socket & service units for on-demand activation
```

## Setup

Requires Python 3.11+ (uses `tomllib`) and `systemd --user`.

1. Create the runtime config directory and files:

```bash
mkdir -p ~/.config/codex-router && chmod 700 ~/.config/codex-router
cp models.example.toml ~/.config/codex-router/models.toml
cp /path/to/your/base-codex-models.json ~/.config/codex-router/base-catalog.json
```

2. Create `~/.config/codex-router/secrets.env` (`chmod 600`) with the environment variables referenced by `key_env` in `models.toml`:

```ini
EGPT_API_KEY=...
DEEPSEEK_API_KEY=...
GLM_API_KEY=...
```

3. Generate the merged Codex model catalog:

```bash
python3 build_catalog.py
```

4. Point `~/.codex/config.toml` at the local router:

```toml
model = "gpt-6.2-astra"
model_provider = "router"
model_catalog_json = "/home/<you>/.codex/codex-models.json"

[features]
responses_websockets_v2 = false
api_key_model_discovery = false

[model_providers.router]
name = "codex-router"
base_url = "http://127.0.0.1:4000/v1"
wire_api = "responses"
requires_openai_auth = false
```

5. Enable on-demand startup (link the unit files and enable only the `.socket`):

```bash
systemctl --user link "$PWD/systemd/codex-router.socket" "$PWD/systemd/codex-router.service"
systemctl --user daemon-reload
systemctl --user enable --now codex-router.socket
```

Verify with `curl http://127.0.0.1:4000/healthz`, then restart the ChatGPT desktop app.

## Daily Operations

- **Add a provider/model**: edit `~/.config/codex-router/models.toml` and `secrets.env`, run `python3 build_catalog.py`, stop the running router (`systemctl --user stop codex-router.service`), and restart ChatGPT.
- **View logs**: `journalctl --user -u codex-router.service -f` (one line per request; headers and bodies are never logged).
- **Run tests**: `python3 -m unittest -v`
