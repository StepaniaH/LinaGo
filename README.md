<p align="center">
  <img src="assets/LinaGo.png" alt="LinaGo" width="160" />
</p>

<h1 align="center">LinaGo</h1>

<p align="center">
  A lightweight OCR + AI translation overlay for Hyprland / Wayland.
</p>

<p align="center">
  <img alt="version" src="https://img.shields.io/badge/version-v0.5.0-blue" />
  <img alt="platform" src="https://img.shields.io/badge/platform-Hyprland%20%7C%20Wayland-lightgrey" />
  <img alt="license" src="https://img.shields.io/badge/license-MIT-green" />
</p>

---

LinaGo captures a screen region (or the primary selection), recognizes text with Tesseract or a vision-capable model, and translates it through a configurable AI backend — local **Ollama** or **bring-your-own-key** OpenAI-compatible APIs. Results appear in a GTK4 layer-shell popup anchored to your cursor, with streaming output, editable source text, custom prompt actions, and one-click copy.

## Features

- **Region OCR** — `slurp` + `grim` + Tesseract (`chi_sim` + `eng`)
- **Vision OCR** — send the screenshot straight to a multimodal model (e.g. `qwen2.5vl`) when Tesseract struggles with small or stylized text
- **Selection mode** — translate the primary selection without taking a screenshot
- **Streaming translation** — tokens appear as they arrive
- **Language pairs** — auto-detects English, 中文, 日本語, 한국어, Русский; manual targets for Français, Deutsch, Español
- **Custom actions** — reusable prompt templates (explain, polish, …) beside plain translation
- **BYOK providers** — switch between local Ollama and OpenAI-compatible endpoints (OpenAI, DeepSeek, Groq, OpenRouter, …)
- **Editable source** — edit the recognized text; translation refreshes after you pause typing (or press `Ctrl+Enter`)
- **Copy buttons** — one click per pane; uses `wl-copy` so the clipboard survives after the popup closes
- **Compare providers** — run up to four backends on the same text in labeled stacked panes
- **Web console** — configure everything from a loopback web page served by the daemon
- **Doctor** — `--doctor` self-checks binaries, traineddata, config, and provider reachability
- **Theme presets** — dark / midnight / paper with accent and font-scale overrides, rendered to the popup stylesheet

## Requirements

| Component | Notes |
|-----------|--------|
| Hyprland / Wayland | Uses `hyprctl` for cursor & monitor geometry; without it the popup opens in a fixed corner |
| GTK 4 + Gtk4LayerShell | Popup overlay |
| Python 3.11+ | Tested on 3.14 |
| `grim`, `slurp` | Region capture |
| `tesseract` | Only for the default OCR engine (+ `chi_sim` / `eng` traineddata) |
| `wl-copy`, `wl-paste` (optional) | Clipboard survival; `--selection` mode |
| Ollama **or** an API key | Translation backend |

On Arch Linux:

```bash
sudo pacman -S grim slurp tesseract tesseract-data-chi_sim tesseract-data-eng \
  gtk4 gtk4-layer-shell python-gobject python-requests python-tomlkit wl-clipboard
```

## Quick start

```bash
git clone git@github.com:StepaniaH/LinaGo.git
cd LinaGo

./run.sh --ocr --translate
```

`run.sh` creates `.venv` (with `--system-site-packages` so PyGObject comes from system packages) and launches `python -m linago`. To install instead of running from the checkout:

```bash
pip install .          # console script: linago
```

## Usage

```bash
./run.sh --ocr                          # screenshot → OCR → popup
./run.sh --ocr --translate              # screenshot → OCR → translate → popup
./run.sh --ocr-multi --translate        # several regions → one translation
./run.sh --ocr-engine vision --translate  # transcribe with a vision model
./run.sh --selection --translate        # translate the primary selection
./run.sh --translate --text "hello"     # translate given text
./run.sh --translate --action explain --text "…"
./run.sh --translate --provider openai --text "…"
./run.sh --translate --from auto --to zh --text "…"
./run.sh --history 50                   # replay recent translations
./run.sh --daemon                       # resident mode + web console
./run.sh --web-only                     # configuration console, no popup
./run.sh --doctor                       # environment self-check (--json supported)
./run.sh                                # demo / help card
```

**In the popup**

| Action | Behavior |
|--------|----------|
| Esc / ✕ | Close |
| Language dropdowns / ⇄ | Change pair and retranslate |
| Provider dropdown (footer) | Switch Ollama ↔ BYOK backends |
| Action dropdown (footer) | Plain translation ↔ configured actions |
| Edit source text | Debounced retranslate (~700 ms) |
| `Ctrl+Enter` in source | Retranslate immediately |
| ⧉ on a pane | Copy that pane’s text |
| 🔊 on the translation pane | Speak via OpenAI-compatible TTS (`[tts]`) |
| 📌 in the header | Pin the card; Esc is ignored while pinned |

With `[compare] providers` set, the footer provider dropdown is hidden — the pane set comes from settings instead. Failed or empty OCR stays in the source pane with its message and is never sent to the provider.

### Hyprland bind examples

```conf
bind = SUPER, T, exec, /path/to/LinaGo/run.sh --ocr --translate
bind = SUPER, S, exec, /path/to/LinaGo/run.sh --selection --translate
```

### Resident daemon

```bash
./run.sh --daemon
```

While a daemon runs, every other invocation detects its socket and
forwards the request instead of starting a fresh process — hotkey
pops appear instantly. `--no-forward` opts out, `--socket PATH`
relocates the endpoint (`$XDG_RUNTIME_DIR/linago-$UID.sock` default).

Subscribers receive one JSON line per completed translation, e.g. for
OBS overlays or logging:

```bash
echo '{"cmd":"subscribe"}' | nc -U "$XDG_RUNTIME_DIR/linago-$UID.sock"
```

## Configuration

Config resolution order: `$LINAGO_CONFIG_DIR`, `./config/` (checkout), then `$XDG_CONFIG_HOME/linago/`.

### Providers — `config/settings.toml`

```toml
[app]
provider = "ollama"          # default backend name

[providers.ollama]
type = "ollama"
label = "Ollama"
base_url = "http://127.0.0.1:11434"
model = "qwen2.5:3b"

[providers.openai]
type = "openai"
label = "OpenAI"
base_url = "https://api.openai.com/v1"
model = "gpt-4o-mini"
api_key_env = "OPENAI_API_KEY"
```

Add any OpenAI-compatible endpoint with `type = "openai"`. Switch the active provider via `provider = "…"` in settings, `TRANSLATE_PROVIDER` / `TRANSLATE_MODEL`, CLI `--provider`, or the footer dropdown. Providers accept optional `timeout`, `temperature`, and `max_tokens` keys.

### OCR engine

```toml
[ocr]
engine = "tesseract"         # or "vision"
tesseract_langs = "chi_sim+eng"
provider = "ollama"          # provider used when engine = "vision"
```

Vision mode sends the screenshot to the named provider's model (e.g. pull `qwen2.5vl:7b` into Ollama). Override per run with `--ocr-engine tesseract|vision`.

### Actions

```toml
[actions]
explain = "Explain the terminology and context of this {source} text:"
polish = "Polish the following text:\n\n{text}"
```

`{source}` / `{target}` expand to language names; `{text}` receives the text (appended when absent). Select with `--action`, the footer dropdown, or set `[app] action = "explain"` as default.

### API keys (BYOK)

Keys live in `config/secrets.toml` (copy `secrets.toml.example`) or the env var named by each provider's `api_key_env`; `TRANSLATE_KEY_<NAME>` works too. Keys are matched by provider name. **`config/secrets.toml` is gitignored — never commit real keys**, and keep it readable only by you:

```bash
chmod 600 config/secrets.toml
```

### Languages

`auto` detects English, Chinese, Japanese, Korean, and Russian from Unicode scripts; languages sharing the Latin script (French, German, Spanish) are selectable manually. `auto` on the target side picks the peer language (English pairs with Chinese, everything else defaults to English). Override with `--from` / `--to` or `TRANSLATE_FROM` / `TRANSLATE_TO`.

### Compare output

```toml
[compare]
providers = ["openai", "deepseek"]      # up to four; empty = single pane
```

When set, the popup renders one streaming pane per provider instead of a single translation. Panes restart together on language or action changes, and each completion is recorded separately in history and daemon events.

### Appearance

```toml
[appearance]
preset = "dark"             # dark | midnight | paper
accent = ""                 # optional override for section labels
bg_alpha = 0.94             # 0.3 – 1.0
font_scale = 1.0            # 0.7 – 1.6
```

`style.css` is generated from `linago/style.css.template`; edit appearance through settings or the web console rather than by hand.

### Web console

The daemon serves a configuration page on `http://127.0.0.1:8777`
(`--web-port`, `--no-web` to disable). `--web-only` runs the console
without the popup stack for headless setups.

First start writes a token into the cache directory
(`~/.cache/linago/web-token`); paste it once in the browser — data
endpoints reject requests without it. Tabs cover providers/BYOK,
compare selection, appearance, language defaults and action templates,
Hyprland bind snippets, and diagnostics including per-provider
reachability tests. Provider keys are write-only: saving updates
secrets.toml (mode 0600), and values are never sent back to the
browser.

### History, speech, memory

```toml
[history]
enabled = true              # record translations locally (default)

[tts]
provider = "openai"         # enables the speaker button (OpenAI-compatible TTS)

[memory]
enabled = false             # opt-in: remember languages per app class

[app]
lang = "zh_CN"              # UI language; unset follows the system locale
```

With `[memory] enabled = true` and both language dropdowns set to auto, the focused window's Hyprland class biases detection from recorded votes; every auto translation records a vote (last 500 kept, cache dir).

## Privacy

- API keys belong only in `secrets.toml` (gitignored) or environment variables; they are never logged.
- Screenshots go to the system cache directory (`~/.cache/linago`) and are deleted right after OCR.
- `--verbose` writes logs to that same directory; requests are logged by model name only, never by content or credentials.
- No telemetry; translation traffic goes only to the backends you configure.

## Development

```bash
python -m venv --system-site-packages .venv && . .venv/bin/activate
pip install -e .[dev]

pytest            # unit tests (no GTK needed)
ruff check .
mypy
```

CI runs lint, typecheck, and tests on every push. Runtime behavior (popup, OCR, layer-shell placement) needs a real Wayland session. After editing `linago/locale/**/*.po`, regenerate the catalog:

```bash
msgfmt -o linago/locale/zh_CN/LC_MESSAGES/linago.mo \
       linago/locale/zh_CN/LC_MESSAGES/linago.po
```

## Project layout

```
LinaGo/
├── assets/LinaGo.png           # project icon
├── config/
│   ├── settings.toml           # providers, engines, appearance defaults
│   ├── secrets.toml.example    # API key template
│   └── style.css               # generated popup stylesheet
├── docs/ROADMAP.md             # planned work
├── linago/
│   ├── cli.py                  # argument parsing and launch
│   ├── ui.py                   # GTK4 layer-shell popup
│   ├── compare.py              # multi-provider pane helpers
│   ├── backends.py             # streaming, TTS, vision OCR clients
│   ├── ocr.py                  # capture, primary selection, tesseract
│   ├── placement.py            # hyprctl geometry and placement math
│   ├── lang.py                 # language table, detection, prompts
│   ├── i18n.py                 # gettext catalogs (locale/)
│   ├── config.py               # settings and provider loading
│   ├── configstore.py          # comment-preserving TOML writes
│   ├── theme.py                # preset rendering for style.css
│   ├── history.py              # SQLite translation history
│   ├── memory.py               # per-application language votes
│   ├── daemon.py               # Unix socket protocol and event bus
│   ├── webserver.py            # web console API
│   ├── playback.py             # audio player dispatch
│   ├── doctor.py               # self-check report
│   ├── style.css.template      # theme template
│   ├── locale/zh_CN/           # catalog sources and compiled .mo
│   └── webui/                  # console static assets
├── packaging/aur/              # AUR package template
├── tests/
├── run.sh                      # launcher
└── pyproject.toml
```

## License

MIT
