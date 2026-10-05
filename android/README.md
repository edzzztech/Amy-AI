# Amy for Android

A standalone build of Amy that runs **on the phone**, not a remote control for the desktop.

That follows from the requirement "I want to access it anywhere, even if my PC is off". If the PC is off there is nothing to connect to, so the assistant itself — model, speech and conversations — lives on the device.

---

## What she does

- **Always listening** for "Amy" (and the usual mis-hearings: Aimee, Amie, Emmy…). After each reply there is a 20-second window where follow-ups don't need her name. Say "go to sleep" or tap the mic to stop; she stays asleep, even across restarts, until woken.
- **An on-device model** through MediaPipe: any `.task` model in her folder, GPU first with a CPU fallback. Her voice starts on the first sentence rather than waiting for the whole reply.
- **Answers from the phone itself** for the time, the date and the battery — a small model would guess those.
- **Phone controls**: open apps by name, set timers and alarms (through your clock app), switch the torch, change the media volume.
- **Acting on the screen**, with her accessibility service on: read what is on screen, tap things by name, type into the focused field, go back, go home.
- **Files**: attach text, Markdown, CSV, JSON, code or Word (`.docx`) documents and ask about them. Long ones are cut to what the model can take, keeping the beginning. PDFs are not supported yet.
- **Your computer**: pair with Amy on the desktop, then say "on my PC, …" to send it a command.
- A **floating orb** over other apps (drag it, tap to open her, long-press to sleep or wake), a **camera** view, **conversation history**, and an **action log** — ask "what have you done today?".
- **After a restart** she listens again by herself on Android 10. From Android 11 the system no longer lets a microphone service start itself at boot, so she posts a notification instead and one tap brings her back.

### Things to say

| Say | What happens |
|---|---|
| "Amy, open Spotify" | Opens it |
| "Set a timer for 10 minutes" / "a five minute timer" | Timer in your clock app |
| "Wake me up at 7:30" / "set an alarm in 20 minutes" | Alarm in your clock app |
| "What time is it?" / "What's the date?" / "How much battery have I got?" | Answered from the phone |
| "Turn the torch on" / "Volume up" / "Set the volume to 40 percent" | Done directly |
| "What's on my screen?" / "Tap Send" / "Type see you soon" | Through the accessibility service |
| "On my PC, open Chrome" | Sent to Amy on your computer |
| "What have you done today?" | Read back from the action log |
| "Go to sleep" | Stops listening until woken |

Polite forms work too: "could you open Spotify for me, please" is the same command.

---

## Permissions, and why

| Permission | Why |
|---|---|
| Microphone | Listening. The core of it. |
| Notifications | The "listening" notification Android requires for an always-on service, and the after-restart reminder. |
| Accessibility service (optional) | Reading and acting on the screen. Also lets her open apps while she is in the background. |
| Display over other apps (optional) | The floating orb. Also lets her open apps from the background. |
| Camera (optional) | The camera view, asked for when you first open it. |
| Battery: Unrestricted | Without it, Android eventually stops the listening service. |

Since Android 10 an app in the background cannot open other apps unless one of those optional permissions is on. Without either, she says so instead of claiming she opened something.

---

## What does not carry over from the desktop

| Desktop | Why |
|---|---|
| **CAD** | CadQuery is Python + OpenCascade, roughly 450 MB of native desktop code. There is no Android build. |
| **Reading your PC's screen** | A phone can read *its own* screen, not your computer's. |
| **Driving Windows applications** | Win32 UI automation has no Android equivalent. The phone drives *Android* apps instead. |
| **Full-size models** | A phone runs 1–3B parameter models. Expect noticeably weaker reasoning than a desktop running 14B+. |
| **Routines, memory search, the proactive engine** | Desktop only for now. |

So this is **Amy for Android**, a sibling with the same personality and orb, not a port.

---

## Root

Optional. Everything above works unrooted. Today root is used for one thing: opening an app when Android would block it from the background and neither of the optional permissions is on.

---

## Known limits

- **She cannot describe photos yet.** The camera captures, but describing an image needs a vision model (Gemma 3n is the candidate), which is not wired in.
- **No voice interruptions while she talks.** Android's speech recogniser has no echo cancellation, so she would hear herself; she stops listening while speaking. Tap the mic, or long-press the orb, to cut her off.
- **Speech recognition is your phone's own.** She asks it to work offline; whether it can depends on the phone and its installed language packs.

---

## Building

```
Android Studio (Ladybug or newer), its bundled JDK 17
minSdk 29 (Android 10), targetSdk 36 (Android 16)
```

Open the `android/` folder as a project, let Gradle sync, and run on a device. The main README has the step-by-step, including where the model file goes.

The parsing behind the commands — what counts as a timer, an alarm, a polite request — and the file readers are covered by JVM unit tests:

```
gradlew :app:testDebugUnitTest
```
