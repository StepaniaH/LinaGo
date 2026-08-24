# Roadmap

Planned and exploratory work, ordered roughly by priority within each
horizon. Anything shipped moves to the changelog.

## Near term

- **Runtime QA pass on Hyprland** — daemon window replacement, pin
  toggle, TTS playback, and multi-monitor placement were built against
  unit-tested cores but need a real session before tagging.
- **PyPI publication** — configure trusted publishing so the release
  workflow can upload wheels; AUR PKGBUILD then switches to the PyPI
  source.

## Exploratory

- **History search UI** — full-text filter over recorded translations
  inside the popup instead of the CLI listing only.
- **Streaming vision OCR** — transcribe progressively like text
  translation instead of one blocking call.
- **Waybar integration** — surface daemon state and last translation
  summary in the bar.
- **Multi-language batch actions** — run several configured actions
  over the same source text in parallel panes.
