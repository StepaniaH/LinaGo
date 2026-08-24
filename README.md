<p align="center">
  <img src="assets/LinaGo.png" alt="LinaGo" width="160" />
</p>

<h1 align="center">LinaGo</h1>

<p align="center">
  A lightweight OCR + AI translation overlay for Hyprland / Wayland.
</p>

<p align="center">
  <img alt="version" src="https://img.shields.io/badge/version-v0.2.2-blue" />
  <img alt="platform" src="https://img.shields.io/badge/platform-Hyprland%20%7C%20Wayland-lightgrey" />
  <img alt="license" src="https://img.shields.io/badge/license-MIT-green" />
</p>

---

LinaGo captures a screen region, recognizes text with Tesseract, and translates it through a configurable AI backend — local **Ollama** or **bring-your-own-key** OpenAI-compatible APIs. Results appear in a GTK4 layer-shell popup near your cursor, with Bob-style language controls, editable source text, and one-click copy.

## Features

- **Region OCR** — `slurp` + `grim` + Tesseract (`chi_sim` + `eng`)
- **Streaming translation** — tokens appear as they arrive
- **Bob-style language pair** — `auto` / English / 中文, with swap; auto flips en↔zh by character mix
- **BYOK providers** — switch between local Ollama and OpenAI-compatible endpoints (OpenAI, DeepSeek, Groq, OpenRouter, …)
- **Editable source** — edit the recognized text; translation refreshes after you pause typing (or press `Ctrl+Enter`)
- **Copy buttons** — one click per pane; uses `wl-copy` so the clipboard survives after the popup closes
- **Screen-aware placement** — anchors to the roomier side of the cursor and clamps height so the card stays on screen

## Requirements

| Component | Notes |
|-----------|--------|
| Hyprland / Wayland | Uses `hyprctl` for cursor & monitor geometry |
| GTK 4 + Gtk4LayerShell | Popup overlay |
| Python 3.11+ | Project tested on 3.14 |
| `grim`, `slurp`, `tesseract` | OCR pipeline (+ `chi_sim` / `eng` traineddata) |
| `wl-copy` (optional) | Persistent clipboard after close |
| Ollama **or** an API key | Translation backend |

On Arch Linux:

```bash
sudo pacman -S grim slurp tesseract tesseract-data-chi_sim tesseract-data-eng \
  gtk4 gtk4-layer-shell python-gobject python-requests wl-clipboard
```

## Quick start

```bash
git clone git@github.com:StepaniaH/LinaGo.git
cd LinaGo

python -m venv --system-site-packages .venv
source .venv/bin/activate   # PyGObject usually comes from system packages

# Point the Ollama provider at your local host/model (see config/settings.toml)
./run.sh --ocr --translate
```

`run.sh` sets `LD_PRELOAD` for gtk4-layer-shell on Arch and launches the popup.

## Usage

```bash
./run.sh --ocr                          # screenshot → OCR → popup
./run.sh --ocr --translate              # screenshot → OCR → translate → popup
./run.sh --translate --text "hello"     # translate given text
./run.sh --translate --provider openai --text "…"
./run.sh --translate --from auto --to zh --text "…"
./run.sh --text "hello"                 # show text only
./run.sh                                # demo / help card
```

**In the popup**

| Action | Behavior |
|--------|----------|
| Esc / ✕ | Close |
| Language dropdowns / ⇄ | Change pair and retranslate |
| Provider dropdown (footer) | Switch Ollama ↔ BYOK backends |
| Edit source text | Debounced retranslate (~700 ms) |
| `Ctrl+Enter` in source | Retranslate immediately |
| ⧉ on a pane | Copy that pane’s text |

### Hyprland bind example

```conf
bind = SUPER, T, exec, /path/to/LinaGo/run.sh --ocr --translate
bind = SUPER, S, exec, /path/to/LinaGo/run.sh --selection --translate
```

`--selection` translates the current primary selection (select text,
then press the key); it requires `wl-clipboard`.

## Configuration

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

Add any OpenAI-compatible endpoint with `type = "openai"`. Switch the active provider at runtime via:

- `provider = "…"` in settings
- `TRANSLATE_PROVIDER` / `TRANSLATE_MODEL`
- CLI `--provider`
- the footer dropdown

### API keys (BYOK) — `config/secrets.toml`

```bash
cp config/secrets.toml.example config/secrets.toml
```

```toml
[keys]
openai = "sk-…"
deepseek = "sk-…"
```

Keys are matched by provider name. Alternatively set the env var named in `api_key_env` (e.g. `OPENAI_API_KEY`). **`config/secrets.toml` is gitignored — never commit real keys.**

### Languages

```bash
./run.sh --translate --from auto --to zh --text "…"
# or TRANSLATE_FROM / TRANSLATE_TO
```

`auto` detects English, Chinese, Japanese, Korean, and Russian from
Unicode scripts; languages sharing the Latin script (French, German,
Spanish) are selectable manually. `auto` on the target side picks the
peer language (English pairs with Chinese, everything else defaults to
English).

## Project layout

```
LinaGo/
├── assets/LinaGo.png            # project icon
├── config/
│   ├── settings.toml            # providers, OCR engine & defaults
│   ├── secrets.toml.example     # API key template
│   └── style.css                # popup styling
├── linago/
│   ├── lang.py                  # language table, detection, prompts
│   ├── placement.py             # hyprctl geometry + popup placement math
│   ├── ocr.py                   # slurp/grim capture + tesseract
│   ├── config.py                # provider/settings loading
│   ├── backends.py              # streaming translation backends
│   ├── ui.py                    # GTK4 layer-shell popup
│   └── cli.py                   # argument parsing & launch
├── run.sh                       # launcher
└── README.md
```

## Privacy

- API keys belong only in `config/secrets.toml` (ignored) or environment variables.
- Screenshots go to the system cache directory (`~/.cache/linago`) and are deleted after OCR.
- No telemetry; translation traffic goes only to the backend you configure.

## License

MIT
