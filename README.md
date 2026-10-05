# Amy AI

**A local-first voice assistant for Windows.** Amy hears you across the room, looks at whatever is on your screen, drives your applications for you, and models real CAD parts — with the language models running locally through [Ollama](https://ollama.com), so nothing is sent to an external API.

🌐 **[edzzztech.github.io/Amy-AI](https://edzzztech.github.io/Amy-AI/)**

> Actively in development — a few known bugs are being worked out. Contributions and PRs welcome; feel free to fork and improve.

---

## What she does

Amy isn't a chat window. She has hands, eyes and ears on the machine she runs on, and one wake word reaches all of it.

| | |
|---|---|
| **Full-duplex voice** | Listens while she speaks. Interrupt mid-sentence and she stops — barge-in is voice-activated, with a clap as a backup wake signal. |
| **Screen awareness** | A vision model reads your actual display on an interval. Ask what's on screen, what an error means, or what a dialog is asking for. |
| **Computer control** | Finds and drives real applications through UI automation — opening, clicking, typing. Risky steps wait for your approval. |
| **Real CAD** | CadQuery builds exact B-rep solids — hinged assemblies, tolerances, STEP export — then a visual check confirms the part matches the description. |
| **Cameras & gestures** | Point a webcam at something and ask what it is. Hand gestures map to actions: open palm pauses media, fist mutes, peace skips the track. |
| **Proactive engine** | Thinks on a timer, watches folders for new files, and raises what's worth raising — rate-limited, and silent during quiet hours. |
| **Persistent memory** | Transcripts, a knowledge base and learned skills live on disk in `amy_data/`, watched for changes and recalled in later conversations. |
| **Model routing** | Separate local models for fast replies, reasoning, code, CAD and vision. Auto-select picks the right one per request. |

Plus email drafting, SMS via Twilio, Spotify, Home Assistant, Outlook calendar, browser automation, styled PDF reports, a security sentinel, and a plugin system.

---

## Requirements

- **Windows** (10 or 11)
- **Python 3.10+**
- **[Ollama](https://ollama.com)** installed, with enough RAM/VRAM to host a local model
- A microphone; a webcam is optional

---

## Setup

### 1. Clone and install

```bash
git clone https://github.com/edzzztech/Amy-AI.git
cd Amy-AI
pip install -r requirements.txt
```

CadQuery (~450 MB) and OpenCV are imported lazily — neither loads until you actually ask for CAD or the camera.

### 2. Pull the local models

```bash
ollama pull llama3.2          # conversation + reasoning
ollama pull llava             # screen and camera vision
ollama pull nomic-embed-text  # memory search
```

Amy can start the Ollama server herself on launch (`ollama.auto_start`).

### 3. Configure

Open `amy_config.json` and replace the placeholders. Everything is optional except the `ollama` block — skip any integration you don't want and that feature stays quiet.

```jsonc
"assistant": {
  "wake_word": "amy",
  "user_title": "Sir",
  "default_city": "London",
  "neural_voice": "en-GB-SoniaNeural"
}
```

### 4. Run

```bash
python amy.py
```

She greets you on launch, then waits for the wake word. Say **"Amy, what can you do?"** for the current skill list.

---

## Configuration

`amy_config.json` holds every key and tunable — nothing is hard-coded. The settings most worth changing first:

| Setting | Default | What it does |
|---|---|---|
| `assistant.wake_word` | `amy` | What she answers to. Pick something that won't collide with normal speech. |
| `assistant.neural_voice` | `en-GB-SoniaNeural` | Any Microsoft Edge neural voice. Free, no API key. |
| `assistant.barge_in` | `voice` | How to interrupt mid-reply. `voice` cuts her off as soon as you speak. |
| `assistant.automation_approval` | `risky` | When to ask before acting. `risky` gates destructive steps; `all` gates everything. |
| `assistant.allow_shell_commands` | `false` | Off by default, deliberately. |
| `ollama.auto_select` | `true` | Routes each request to the fast, reasoning, code, CAD or vision model. |
| `ollama.auto_start` | `true` | Starts the Ollama server on launch if it isn't running. |
| `proactive.level` | `normal` | How often she speaks up unprompted, bounded by `quiet_hours` and `max_per_hour`. |
| `camera.start_on_launch` | `false` | The webcam stays off until asked. Gestures need it running. |
| `ui.theme` | `arc` | Window theme and which controls appear in the dock. |

### Multiple cameras

Amy supports several named cameras — a desk cam, a bench cam, one pointed at a 3D printer:

```jsonc
"camera": {
  "devices": [
    { "name": "Desk",    "index": 0 },
    { "name": "Bench",   "index": 1 },
    { "name": "Printer", "index": 2 }
  ],
  "active": "Desk"
}
```

Say **"Amy, scan for cameras"** to detect and add attached devices automatically, then **"switch to the bench camera"** to change feeds. You can also rename or pick a camera from the dropdown in the camera view.

| Command | Does |
|---|---|
| `scan for cameras` | Probe devices and add any new ones |
| `what cameras do I have` | List configured cameras |
| `switch to the <name> camera` | Change the active feed |
| `rename the <old> camera to <new>` | Rename |
| `add a camera called <name> on device 2` | Add by index |
| `remove the <name> camera` | Forget it |

---

## Privacy

Inference runs against `localhost`. Mail, messaging and smart-home integrations are opt-in, and credentials live only in your own `amy_config.json`, which stays on your machine.

`amy_data/` holds your transcripts, memory and anything Amy generates. It's excluded by `.gitignore` — don't commit it.

---

## Project layout

```
amy.py              the application — UI, voice, vision, CAD, automation
amy_config.json     every key and tunable
requirements.txt    dependencies, with optional extras marked
docs/index.html     the project site, served by GitHub Pages
amy_data/           runtime state (gitignored)
```

---

## Contributing

Issues and pull requests are welcome. If you're reporting a bug, `amy_data/amy_debug.log` usually has the detail — check it for credentials before pasting.

## License

No license has been chosen yet. Until one is added, all rights are reserved; open an issue if you'd like to use the code.
