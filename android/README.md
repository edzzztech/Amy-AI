# Amy for Android

A standalone build of Amy that runs **on the phone**, not a remote control for the desktop.

That follows directly from the requirement "I want to access it anywhere, even if my PC is off". If the PC is off there is nothing to connect to, so the assistant itself — model, speech, memory — has to live on the device.

---

## What this means for the account system

**It removes the need for one.** Accounts exist to link a client to a server. With a standalone phone build there is no server: the phone *is* Amy. What's left is **sync** — keeping memory and routines consistent between two independent instances — and that needs a pairing token, not a signup.

- Phone and desktop pair once by QR code and exchange a key
- When both are on the same network they reconcile memory, routines and the action log
- When they aren't, each keeps working alone and reconciles later
- Nothing is held on a server, so there is no hosting cost, no breach liability, and the privacy claim on the site stays true

If you later want sync while away from home, the right answer is Tailscale or WireGuard — a private network between your own devices — rather than building a backend.

---

## What can and cannot carry over

Being straight about this up front, because "do everything the PC does" isn't achievable and planning around it would waste months.

### Carries over well

| Desktop | On Android |
|---|---|
| Conversation | A small local model (1–3B) via llama.cpp |
| Speech to text | whisper.cpp on-device, or Android's recogniser |
| Speech out | Android TTS, same neural voices |
| Memory, knowledge base | Same plain-text files, same embedding search |
| Routines | WorkManager / AlarmManager |
| Action log | Identical JSONL format |
| App control | AccessibilityService, plus root for the rest |
| Camera and vision | CameraX + a small vision model |
| Proactive engine | Foreground service |

### Does not carry over

| Desktop | Why |
|---|---|
| **CAD** | CadQuery is Python + OpenCascade, roughly 450 MB of native desktop code. No Android build exists and porting OCCT is not realistic. Best case: the phone sends CAD requests to the desktop when it's reachable. |
| **Reading your PC's screen** | A phone can read *its own* screen with MediaProjection, not your computer's. |
| **Driving desktop applications** | Win32 UI automation has no Android equivalent. The phone drives *Android* apps instead. |
| **Full-size models** | A phone runs 1–3B parameter models. Expect noticeably weaker reasoning than a desktop running 14B+. This is physics, not effort. |

So the honest framing is **Amy for Android**, a sibling that shares memory and personality, not a port.

---

## Why root

Root is not required to run, but it unlocks:

- Shell access as `su` for system-level actions
- Input injection without the accessibility prompt
- Reading other apps' data for context
- Disabling battery optimisation reliably, which matters a lot for an always-listening service

The design degrades gracefully: everything works unrooted through AccessibilityService, and root paths are additive.

---

## Status

Scaffolding only. **Nothing here has been compiled** — it was written without an Android toolchain available. Expect to fix build errors on first open.

### Build

```
Android Studio (Ladybug or newer)
JDK 17
minSdk 29 (Android 10), targetSdk 35
```

Open the `android/` folder as a project, let Gradle sync, and run on a device.

### Order of work

1. App shell, foreground service, permissions — *in progress*
2. TTS out, STT in, wake word
3. Local model via llama.cpp, streaming replies
4. Memory files, shared format with the desktop
5. AccessibilityService automation, root helpers
6. Routines and the action log
7. Pairing and sync with the desktop
8. Camera and vision
