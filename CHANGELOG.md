# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] - 2026-11-04

First public release.

### Added
- Desktop app that turns an audio file into an audio-reactive waveform video with a transparent
  background: ProRes 4444 with alpha, in a MOV.
- 10 layouts (Scroll, Center-out, Pulse, Flow, Ripple, Neon, Reflection, Peak hold, Orbit, Voice
  line) and 4 styles (Bars, Line, Area, Dots).
- 18 color themes, solid or gradient fill, custom colors, and Calm, Balanced and Energetic presets.
- Controls for Sensitivity, Contrast, Smoothing, Time window, Density, Height and Opacity, plus
  Reactivity, Drift and Thickness for Voice line. Moving or Static motion, and Peak or Loudness measure.
- Audio analysis every 5 ms, with a 48-band frequency spectrum for the spectrum-driven layouts.
- Live preview with synchronized audio playback, scrubbing, drag and drop, keyboard shortcuts and
  remembered settings.
- Cancelable export that never damages an existing file.
- Export sizes 1080×180, 1920×240, 3840×360 and 1080×1080 at 30 fps.
- `rubato-waveform` command line tool with custom width, height and frame rate.
- Input from WAV, MP3, M4A, AAC, FLAC, AIFF, OGG, OPUS, WMA, MKA and CAF files, and from the audio
  track of MP4, MOV, M4V, MKV and WebM files.
- macOS (Apple Silicon) and Windows (x64) builds from CI. FFmpeg is installed separately.
- A local-only server for the interface: loopback address, random port, per-run token, Host and
  Origin checks, strict Content Security Policy, and an FFmpeg input allow-list.
