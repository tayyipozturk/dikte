# Dikte

A lightweight macOS menu-bar app that turns your speech (Turkish, English, or
both mixed in one sentence) into text and inserts it wherever your cursor is:
VS Code, the terminal, a chat app, a browser, anything.

Everything runs locally on your Mac by default (whisper.cpp on the GPU). Nothing
is sent anywhere unless you switch to the cloud engine.

## Quick start

```bash
./scripts/install.sh          # dependencies, speech model (~550 MB), Dikte.app
open ~/Applications/Dikte.app
```

When macOS asks, allow **Microphone** and **Accessibility**
(System Settings → Privacy & Security). If the hotkey does nothing, also allow
**Input Monitoring** there, then choose **Restart Dikte** from the menu.

Then click into any text field, **hold Right ⌘, speak, release**. The text
appears about one second later.

## How to trigger it

| Mode (menu → Mode) | Start | Stop |
|---|---|---|
| **Hold to talk** (default) | hold Right ⌘ | release |
| ↳ hands-free | double-tap Right ⌘ | tap again, or pause for 3 s |
| **Tap to start / stop** | tap Right ⌘ (holding still works as push-to-talk) | tap again, or pause |
| **Voice activated** | just speak (above the sound-level threshold) | pause for 1.2 s |

* **Esc** cancels a recording. **Right ⌘ + another key** is treated as a normal
  shortcut, so no recording starts.
* In voice-activated mode a tap on the hotkey pauses or resumes listening.
  Use headphones there, or it may transcribe your speakers. For safety this mode
  never presses Enter, so a video or a colleague can't run commands in your terminal.
* The hotkey can be Right ⌘, Right ⌥, Right ⌃, Right ⇧ or fn. Right ⌘ is the default
  because Right ⌥ types `@ [ ]` on the Turkish-Q layout and fn switches input sources.
* Dikte records from the moment the key goes down and ~150 ms past the release,
  so the first and last syllables are kept.
* If you switch apps while it transcribes, the text goes to the clipboard
  instead of the wrong window, and you get a notification.

A small overlay at the bottom of the screen shows the recording time and level.
The menu-bar icon shows the state: mic (ready), red mic (recording),
waveform (listening), ⋯ (transcribing), ⚠︎ (needs attention).

## Turkish + English

The default language setting, **Türkçe + English**, forces Whisper to Turkish and
gives it a mixed example prompt. In testing this kept English terms intact
("component", "pull request") and transcribed pure English sentences correctly.
It was also faster than auto-detect, which costs an extra second.

Whisper sometimes drops a trailing sentence in the other language. Dikte compares
Whisper's timestamps with where the audio really contains speech, and
re-transcribes any skipped part. This is "Repair skipped speech"; it only costs
time when it triggers.

Improve recognition of your own words with `vocabulary` and fix recurring
mistakes with `replacements` (see Settings).

## Microphone tips

* Dikte uses the system default input. To always prefer a specific mic, pick it
  in menu → Microphone, or set `input_device` to part of its name.
* If the overlay meter barely moves, raise the input level in
  System Settings → Sound → Input (many USB mics have no gain knob).
* A muted mic (hardware mute button) sends pure silence. Dikte notices and tells
  you instead of pasting nothing.
* Menu → Sensitivity shows the live level, room noise and threshold.
  **Calibrate Now** measures the room for 2 s.

## Settings

Most options are in the menu. All of them are in
`~/Library/Application Support/Dikte/config.json` (menu → **Edit Settings File…**,
then **Reload Settings**). Invalid values fall back to defaults with a warning.

| Setting | Default | Meaning |
|---|---|---|
| `mode` | `hold` | `hold`, `toggle` or `voice` |
| `hotkey` | `right_cmd` | `right_cmd`, `right_option`, `right_ctrl`, `right_shift`, `fn` |
| `input_device` | `""` | part of the mic's name, e.g. `"USB"`; `""` = system default |
| `language` | `tr` | `tr` (Turkish + English), `auto`, `en` |
| `vocabulary` | GitHub, TypeScript, … | words Whisper should spell your way |
| `replacements` | `{}` | e.g. `{"jit hab": "GitHub", "yeni satır": "\n"}` |
| `submit_phrases` | `[]` | e.g. `["gönder", "send it"]`: say it at the end to press Enter (not in voice mode) |
| `auto_enter` | `false` | always press Enter after inserting (not in voice mode) |
| `auto_threshold` | `true` | threshold follows room noise (+ `noise_margin_db`) |
| `level_threshold_db` | `-45` | manual threshold, and the minimum in auto mode |
| `silence_stop_s` | `3.0` | hands-free stops after this much silence (0 = never) |
| `voice_silence_stop_s` | `1.2` | voice mode: end of an utterance |
| `no_speech_timeout_s` | `10` | hands-free gives up if you say nothing |
| `insert_method` | `paste` | `paste` (clipboard + ⌘V, clipboard restored) or `type` |
| `restore_delay_ms` | `800` | raise if an app sometimes pastes your *old* clipboard |
| `paste_guard` | `true` | copy instead of paste if you switched apps |
| `keep_mic_open` | `false` | instant start, but the orange mic dot stays on |
| `local_model` | `large-v3-turbo-q5_0` | see menu → Speech Engine (others download on click) |
| `engine` | `local` | `local` or `cloud` |
| `keep_last_recording` | `false` | save the last recording as a WAV for debugging |

### Cloud engine (optional)

OpenAI's `gpt-transcribe` officially supports code-switching and gets the
language hints `tr` + `en` and your vocabulary as keywords (about $0.0045 per minute).
Store the key in the Keychain (it's never written to the config):

```bash
security add-generic-password -s Dikte -a OPENAI_API_KEY -w   # paste the key when asked
```

Then choose menu → Speech Engine → Cloud. Any OpenAI-compatible service works via
`cloud_base_url` / `cloud_model` (e.g. Groq with `whisper-large-v3`).

## Troubleshooting

`uv run dikte doctor` checks the setup. Logs are in `~/Library/Logs/Dikte/`.

* **Hotkey does nothing**: allow Accessibility and Input Monitoring for Dikte,
  then Restart Dikte. The menu lists missing permissions at the top.
* **Nothing is pasted**: Accessibility is missing, or the app ignores ⌘V
  (try Output → Type Characters).
* **Old clipboard gets pasted**: raise `restore_delay_ms`.
* **"Mic is silent"**: the mic is muted (check its mute button), or Microphone
  permission is off.
* **Voice mode triggers on typing/noise**: raise the threshold or run Calibrate.
* **Test without a mic**: `uv run dikte transcribe some-file.wav`.

## Command line

```bash
uv run dikte                 # run the menu-bar app in this terminal (dev mode)
uv run dikte doctor          # permissions, model, devices
uv run dikte devices         # microphones (→ marks the one Dikte would use)
uv run dikte download [MODEL]
uv run dikte transcribe FILE [--language tr|auto|en]
uv run pytest                # unit tests;  add "-m integration" for real-model tests
```

In dev mode the permissions belong to your terminal/VS Code, not to Dikte.app.

## How it works

```
Right ⌘ ─► hotkey.py (listen-only event tap)          sounddevice ─► audio.py (16 kHz frames)
                 │                                                        │
                 └──────────► controller.py (one thread, event queue) ◄───┘
                               gesture.py · levels.py (VAD, silence stop)
                                        │ Job
                                        ▼
                               transcriber.py ─► engines/whisper_server.py ─► whisper-server (127.0.0.1)
                                        │           (or engines/cloud.py)
                                        ▼
                               textproc.py (filters, replacements) ─► inserter.py (⌘V / typing)
```

`Dikte.app` is only a tiny signed C launcher (`launcher/launcher.c`) that starts
the Python app as its child. macOS therefore grants the permissions to Dikte.app
itself, and editing the Python code never resets them. Rebuilding the launcher
does (`scripts/build_app.sh` only rebuilds when its sources change).

Privacy and security:
- Audio stays in memory and is discarded after transcription. The local engine
  never uses a proxy, so the audio doesn't leave the Mac.
- whisper-server listens on 127.0.0.1 behind a random secret URL, so other local
  processes can't use or reconfigure it.
- Dictated text is placed on the clipboard for this Mac only (no Universal
  Clipboard) and marked transient, so clipboard managers skip it. The previous
  clipboard is restored.
- The hotkey listener only sees whether its own key is down; it never records
  what you type. Transcripts are logged only at `DEBUG` level. API keys are
  masked in error messages, and cloud requests refuse redirects.
- Downloaded models are checked against their published SHA-256.
- The launcher uses hardened runtime and accepts only a `launcher.conf` that you
  own and that nobody else can write. It gives Python a minimal environment in
  isolated mode (`-I`), because the child inherits Dikte's permissions.
- Known limits (fine on a single-user Mac, worth knowing): a program already
  running as *you* could still change what runs under Dikte's permissions,
  through any of these routes:
  - the Python code and `.venv` in this folder;
  - `launcher.conf`;
  - `whisper_server_path` in config.json, or `/opt/homebrew/bin/whisper-server`;
  - the uv-managed interpreter in `~/.local/share/uv`, e.g. a `sitecustomize.py`.

  Shipping everything inside the signed app would close these routes. Other
  accounts on the same Mac can also see the server's secret URL in the process list.

## Uninstall

Quit Dikte, then delete `~/Applications/Dikte.app`,
`~/Library/Application Support/Dikte`, `~/Library/Logs/Dikte` and (if you enabled
it) `~/Library/LaunchAgents/local.dikte.Dikte.plist`.
