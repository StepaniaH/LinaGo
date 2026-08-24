# Changelog

All notable changes to LinaGo are documented in this file. The format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
versioning follows [SemVer](https://semver.org/).

## [Unreleased]

### Added

- Vision OCR streams the transcription into the source pane while the
  model is still generating instead of waiting for the full result.
- `--history-search QUERY` filters recorded translations
  case-insensitively across both text columns; `--history-limit`
  caps listed rows; `--history-export PATH` writes the selection as
  JSON or CSV.

### Changed

- CI cancels superseded runs on the same ref.
- The resident daemon converts SIGTERM and SIGINT into a clean
  shutdown so socket and console cleanup still runs.

## [0.5.0] - 2026-08-24

### Added

- Compare mode: `[compare] providers` renders up to four backends as
  labeled stacked panes with independent streaming; history rows and
  daemon events carry the pane provider.
- Web console served by the daemon on loopback (default port 8777,
  `--web-port` / `--no-web`); `--web-only` runs it without the popup
  stack. Token-guarded JSON API plus static single-page interface.
- Provider management through the console: CRUD with write-only API
  keys, per-provider reachability test, comment-preserving settings
  writes, secrets stored with owner-only permissions.
- Appearance presets (`dark`, `midnight`, `paper`) with accent,
  background alpha, and font-scale overrides; `style.css` is now
  generated from `linago/style.css.template`.
- `--doctor` self-check with `--json` output: binaries, tesseract
  traineddata, configuration, active provider key, reachability
  probes, daemon socket, version.
- Hyprland bind snippets in the console with copy support and an
  explicit session-only apply via `hyprctl keyword`.

## [0.4.0] - 2026-08-24

### Added

- Resident daemon (`--daemon`) with transparent request forwarding and
  a Unix socket protocol; subscribers receive one JSON event line per
  completed translation.
- Multi-region capture (`--ocr-multi`) joining recognized blocks into
  one translation.
- Local translation history: `--history [N]`, `--history-clear`,
  `[history] enabled` opt-out.
- Speech synthesis for translations via OpenAI-compatible
  `/audio/speech`; `[tts] provider` gates the popup control.
- Opt-in per-application language memory (`[memory] enabled`)
  biasing auto detection by the focused window's Hyprland class.
- Per-provider request options: `timeout`, `temperature`,
  `max_tokens`.
- gettext catalogs for UI strings; `[app] lang` / `LINAGO_LANG`
  override the system locale.
- GUI smoke-test job importing the GTK stack in CI; release workflow
  attaching sdist/wheel to GitHub Releases; AUR package template.

## [0.3.0] - 2026-08-24

### Added

- Installable `linago` package (`pyproject.toml`, console script) with
  the codebase split into focused modules; GTK imports are isolated so
  `--help`, dependency checks, and unit tests run without a Wayland
  stack.
- `--selection` mode translating the Wayland primary selection.
- Japanese, Korean, Russian, French, German, and Spanish targets;
  script-based detection resolves kana to Japanese, hangul to Korean,
  and Cyrillic to Russian.
- Custom actions: named prompt templates under `[actions]` with
  `{source}` / `{target}` / `{text}` placeholders, exposed through
  `--action`, `TRANSLATE_ACTION`, and a popup dropdown.
- Vision-model OCR engine (`[ocr] engine = "vision"`) sending the
  screenshot to a multimodal provider as an alternative to tesseract,
  with `--ocr-engine` / `TRANSLATE_OCR_ENGINE` overrides.
- GitHub Actions CI running ruff, mypy, and pytest on Python 3.11
  and 3.13.
- Connection-level request retries with exponential backoff for
  translation and vision OCR; HTTP rejections still fail fast.
- `--verbose` debug logging to `~/.cache/linago/linago.log`.
- Warning on startup when `secrets.toml` is group/world readable.

### Changed

- Popup placement scopes to the focused monitor using monitor-local
  logical coordinates instead of assuming a single screen at the
  origin.
- Section height caps are recomputed from measured widget heights
  after first layout rather than fixed estimates only.
- Transient state moved from the repository's `.cache/` directory to
  the XDG cache location.

### Removed

- Legacy `TRANSLATE_OLLAMA_URL` / `TRANSLATE_OLLAMA_MODEL`
  environment variables; providers are configured in `settings.toml`.

### Fixed

- Failed or empty OCR output is no longer forwarded to translation.
- Dependency checks actually verify each required binary on PATH
  before failing; missing `hyprctl` degrades to a warning.

## [0.2.2] - 2026-07-30

### Added

- BYOK providers (OpenAI-compatible endpoints) alongside local
  Ollama, switchable at runtime.
- Editable source text with debounced retranslation.
- Copy buttons backed by `wl-copy`.

## [0.2.0] - 2026-07-30

### Added

- Bob-style language pair controls with auto detection and swap.
- Overflow-safe popup placement anchored to the cursor.

## [0.1.0] - 2026-07-29

### Added

- Initial release: region OCR via slurp/grim/tesseract and streaming
  Ollama translation in a GTK4 layer-shell popup.
