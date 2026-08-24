# Changelog

All notable changes to LinaGo are documented in this file. The format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
versioning follows [SemVer](https://semver.org/).

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
