# Roadmap

Planned and exploratory work, ordered roughly by priority within each
horizon. Anything shipped moves to the changelog.

## Near term

- **GUI smoke test in CI** — install GTK 4 + gtk4-layer-shell GIRs on
  the Ubuntu runner and import `linago.ui` to catch binding breakage;
  unit tests currently cover only the GTK-free modules.
- **AUR package** — PKGBUILD consuming sdist/wheel tags; `run.sh`
  remains for checkout use.
- **UI string i18n** — popup labels are hardcoded Chinese; extract
  with gettext so English locales are possible.
- **Per-provider request options** — timeout, temperature, and max
  tokens configurable per provider entry.

## Mid term

- **Resident daemon mode** — a single-instance process holding the
  GTK loop warm, triggered over a Unix socket; removes per-invocation
  startup latency from hotkey binds.
- **Translation history** — local record of source/target pairs with
  search; pinned card that survives popup close.
- **Text-to-speech** — pronounce source or translation via an
  OpenAI-compatible TTS endpoint or a local engine.

## Exploratory

- **IPC broadcast** — publish translations on a Unix socket for
  consumers like OBS overlays or log pipelines.
- **Per-application language memory** — bias auto detection using
  `hyprctl activewindow` class (e.g. terminal → English, IM → Chinese).
- **Multi-region capture** — stitch several selected regions into one
  OCR pass.
