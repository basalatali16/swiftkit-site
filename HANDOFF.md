# LANCOM — Session Handoff (updated 2026-07-03)

Read this first when continuing work on this project in a new session.
The README.md has the full build history and known limitations; this file
is the "where we are right now and what's next" state.

## What this project is

LANCOM: a LAN-only (same-WiFi, no internet, no server) Android app for
voice calls, text messaging, and large file sharing between devices,
built with Python/Kivy and packaged via buildozer in GitHub Actions
(this machine has no Android toolchain — ALL APK builds happen in CI,
workflow: `.github/workflows/build.yml`, artifact name
`lancom-debug-apk`). Target customer: large offices — IP-based
calling/texting/file-sharing within one WiFi network. The owner intends
to sell this software, so security and polish matter.

This repo was originally an unrelated static site ("swiftkit-site");
the owner explicitly chose to build LANCOM in it anyway.

Work happens on branch `claude/lancom-android-apk-kc8qpp`, PR #1 (draft,
open).

## Current state (as of 2026-07-03)

- **CI run #11 (commit 4dfd856) was GREEN** — first successful build
  after the cryptography/Rust/armeabi-v7a failures. arm64-v8a only.
- Everything from the previous handoff is still in place: discovery,
  E2E-encrypted messaging/calls/files, resumable GB-scale file transfer
  with Retry, notifications + background operation, all permissions
  requested up front.
- **NEW: WhatsApp-style UI polish** (this session): real rounded chat
  bubbles (own right/gold-dark, theirs left/card, max 75% width,
  timestamp inside bubble, no sender names in 1:1), Today/Yesterday/date
  separator chips, rounded buttons with pressed states everywhere,
  pill-shaped message input with "+" attach button and pill Send,
  slimmer chat header ("‹" back). Contact rows: drawn online/offline dot
  on the avatar corner (text dots ● / ○ and emoji are NOT renderable in
  Kivy's bundled Roboto — they show as hollow boxes; never use emoji in
  Kivy labels), ellipsized single-line name/status.
- **NEW: manual add-contact-by-IP** (this session): "+ IP" button on the
  contacts screen header → popup → `PeerDiscovery.probe_ip(ip)` sends a
  unicast HELLO with `"probe": true` from the *listen socket* (so the
  reply targets the packet's source address = our listener). Listener
  answers a probe with a unicast HELLO back. The broadcast loop also
  re-probes known contacts whose live last_seen is stale
  (> PEER_TIMEOUT/2) — keeps contacts online and re-finds stored
  contacts after restart on broadcast-filtering networks.

### Verification done locally (Windows, Kivy 2.3.1 on py 3.11)

- Two-OS-process loopback test: probe handshake discovers both sides
  from one probe, and peers stay online past PEER_TIMEOUT via re-probe.
- Runtime UI smoke test (real app run, stubbed discovery): contacts
  screen, add-by-IP popup, chat with bubbles/chips all render and don't
  crash; screenshots reviewed.
- NONE of this session's work is device-verified yet.

## Crash investigation + signing (2026-07-03, later)

- Owner reported the run #11 APK "installed but crashing on install".
- main.py is now a thin crash-reporter entry point; the real app moved
  to lancom_app.py. Any startup crash shows the full traceback on the
  phone screen (screenshot = crash log) and appends to lancom_crash.log
  in app storage.
- Static APK inspection (run #13 artifact) showed packaging is CLEAN:
  libcrypto/libssl present, cryptography 46.0.3 with arm64
  _rust.abi3.so, all modules in private.tar. So a missing/wrong-arch
  native lib is ruled out; awaiting the on-screen traceback.
- ROOT CAUSE FOUND (owner sent the crash-screen traceback - the
  reporter worked): `ImportError: dlopen failed: cannot locate symbol
  "_Py_TrueStruct" referenced by ..._rust.abi3.so`. p4a's Rust build
  didn't link extensions against libpython; bionic has no lazy binding.
  Fixed upstream in p4a develop PR #3333 (2026-05-21), not in any
  release (latest v2026.05.09). buildozer.spec now pins p4a.branch =
  develop @ commit 10d4798 which contains the fix. Drop the pin when a
  p4a release includes it.
- LIKELY install-failure cause found: CI regenerated the debug keystore
  every run → different signature each build → Android refuses
  install-over-existing ("App not installed"). Fixed: stable PKCS12
  debug keystore stored as repo secret LANCOM_KEYSTORE_B64, injected in
  CI and mounted at /home/user/.android. Local backup (NOT in the repo -
  repo is public):
  `C:\Users\basal\Documents\CHROME EXTENTIONS\claude code\lancom-signing\debug.keystore`
  (alias androiddebugkey, store/key password "android"). Owner should
  back this file up; for a real release a proper private keystore is
  still needed.
- Workflow paths filter now triggers on any **.py (was main.py only -
  would have silently skipped builds after the entry-point split).
- New app icon (gold L monogram + green presence dot on navy) and
  presplash shipped in v0.2.
- Owner asked for "blockchain, not easy to crack": explained blockchain
  adds nothing to a serverless LAN app; E2E crypto already covers
  network security; proposed license-key activation as the real
  anti-piracy feature (not built yet - awaiting owner decision).

## Feature round 2 (2026-07-03, after owner's first device test)

Build #15 booted fine on device (crash fix confirmed). Owner feedback
round implemented in v0.3 (build #16):

- Multicast/Wifi/Wake locks (android_notify.acquire_background_locks):
  MulticastLock is REQUIRED for receiving UDP broadcast on Android at
  all; wifi+wake locks keep networking alive in background. New perms:
  WAKE_LOCK, CHANGE_WIFI_MULTICAST_STATE.
- Store-and-forward outbox: queue_message/flush_outbox on
  MessageServer; flush on send, on chat open, and on discovery's new
  on_peer_online callback. msg_id dedup on receive; RECEIPT frames
  (delivered/seen) on the message port; WhatsApp ticks in bubbles
  ("…"/gray ✓✓/gold ✓✓) via bundled DejaVuSans (Roboto lacks U+2713;
  fonts dir is NOT on resource path - resolve via kivy.__file__).
- Notification tap opens the app (contentIntent).
- Call status: "Ringing..." when INVITE lands, "busy on another call".
- File picker: SAF ACTION_OPEN_DOCUMENT via android.activity result
  binding (plyer filechooser broken on API 33+); content:// copy moved
  off the UI thread.
- Tests: msg_peer.py two-process store-and-forward test passed
  (offline queue -> reconnect delivery -> dedup -> delivered/seen);
  probe test re-passed; UI smoke re-passed with tick rendering.
- Owner's file-share crash screenshot never arrived (attach failed
  twice); SAF picker most likely fixes it, but confirm on device.

## Feature round 3 (2026-07-03, v0.4 / build #17)

Owner's build #16 device test: file transfer, ticks, UI all confirmed
working on device. Two fixes shipped in v0.4:

- Background notifications: Kivy's Clock is PAUSED while backgrounded;
  notifications routed via Clock.schedule_once only fired on app
  reopen (matches owner's exact symptom). Now fired directly from the
  worker threads when not foregrounded (messages, incoming calls, and
  now completed incoming files). KEY LESSON: never route
  background-critical work through the Kivy Clock.
- Received files: exported to public Downloads/LANCOM (MediaStore API
  29+, direct write below), images keep a private copy and render as
  inline ChatImageBubble previews; non-images are moved not copied.

STILL OPEN: if the owner needs delivery after the app is SWIPED AWAY
from recents (not just backgrounded), that requires a p4a foreground
service running the networking stack in the service process (major
refactor: UI<->service IPC, service-owned DB writes). Owner asked to
test the backgrounded-vs-swiped distinction with build #17.

## Foreground service architecture (2026-07-03, v0.5)

Owner confirmed they need WhatsApp-grade background operation: receive
messages/calls/files with the app CLOSED (swiped away) and phone
locked. Delivered via a full process split:

- netcore.py (NEW, Kivy-free): all networking - PeerDiscovery,
  MessageServer (outbox/receipts), CallManager, FileTransferManager,
  NetworkCore orchestrator emitting one JSON-safe event stream, and
  EventPolicy (the single notify-vs-mark-seen decision point).
- service.py (NEW): p4a sticky foreground service entrypoint. Runs
  NetworkCore + ControlServer, holds the multicast/wifi/wake locks,
  posts notifications, calls setAutoRestartService(True) so a swiped
  task comes back. buildozer.spec: `services = lancomnet:service.py:
  foreground:sticky:foregroundServiceType=dataSync` (+ FOREGROUND_SERVICE,
  FOREGROUND_SERVICE_DATA_SYNC, WAKE_LOCK permissions).
- lancom_ipc.py (NEW): localhost:55560 control channel, newline-JSON,
  token = sha256(identity storage key + "lancom-control") so only this
  app's processes can connect. ControlClient auto-reconnects and
  replays state (start/set_view/call_state) after every connect.
- lancom_app.py: UI only. DirectBackend (desktop, in-process core) /
  ServiceBackend (Android, IPC client) behind one method surface.
  UI reads history straight from the shared SQLite DB (WAL +
  busy_timeout=5000 in store.py for cross-process safety); ALL writes
  happen in the networking process.
- android_notify.py: Kivy-free, context-agnostic (activity OR service),
  notifications now open the app when tapped (getLaunchIntentForPackage).

Verified locally: py_compile all; IPC handshake/broadcast/bad-token
test; two-process probe test; two-process store-and-forward messaging
test (pending->delivered->seen, dedup); windowed UI smoke with
screenshots. NOT yet verified on a phone.

Data-path note: both processes resolve the same files dir (Kivy
user_data_dir == ANDROID_PRIVATE on android), so identity/DB carry over.

## Session end state (2026-07-03 ~23:55 local, owner hit usage limit)

Owner asked to auto-continue when their limit resets (Sat 2026-07-04
~03:09 local). A one-time scheduled task handles the resume.

Where things stand RIGHT NOW:
- Build #21 (run id 28678453829) = v0.7.1 FINAL Android APK was
  in_progress when the session ended. It contains EVERYTHING: sticky
  foreground service, real call ringing + missed/no-answer timeouts,
  IP Phone rebrand (icon/presplash/violet-teal palette), typing
  indicators, long-press delete + copy + clear chat, PIN lock, and
  contact-details view with device model. Builds #19 (green) and #20
  are superseded - ignore them.
- FIRST ACTION on resume: check run 28678453829 conclusion via the
  GitHub API (unauthenticated works). If green: give the owner the
  artifact link + the v0.7.1 test checklist (typing, delete, clear,
  PIN incl. answer-call-while-locked, contact details device model,
  ringing regression). If red: read job logs before changing anything.
- Owner has NOT yet device-tested v0.7.x. Waiting on their report.
- NEXT MILESTONE (owner-confirmed order): after the owner confirms the
  final APK on both phones -> build the WINDOWS PC app. netcore.py is
  already Kivy-free and desktop-capable; lancom_app runs on desktop via
  DirectBackend. Plan: package UI+core for Windows (PyInstaller),
  desktop-appropriate window sizing, tray/notifications later.
  device_label() already reports computer name for desktop builds.
- Owner communication style: short bullet requests, tests on real
  phones, sends screenshots (sometimes forgets the attachment - ask
  them to re-send if a referenced image is missing).

## Next steps

1. A new CI build was pushed after these changes — check its result on
   PR #1. If green, owner downloads `lancom-debug-apk` from the run
   artifacts, extracts the zip FULLY (past confusion: tried installing
   from inside a RAR viewer), sideloads, tests on two phones. Key things
   to device-test: new chat UI, add-by-IP on a phone-hotspot network,
   file resume, background notifications.
2. After Android is finalized: iOS ("first finalize android then we'll
   move on ios"). Owner has a Mac but prefers testing on an old iPad
   running iOS 12 — current kivy-ios/Xcode may not target iOS 12 at
   all; verify before promising anything.
3. Selling-readiness ideas not yet requested explicitly: release
   signing config, app icon, onboarding screen.

## Owner's standing requirements (their words, condensed)

- Background operation with notifications + sound: DONE, unverified.
- WhatsApp-smooth interface: DONE this session, unverified on device.
- File sharing must work and resume after connection loss, files "in
  GBs": DONE, unverified on device.
- Best solution for large offices (IP-based calls/texts/files on one
  WiFi): add-by-IP DONE this session, unverified on device.
- All permissions requested at once on first open: DONE.
- Security "the best" since they'll sell it: E2E encryption DONE.

## Working practices that saved time (keep doing these)

- CI failures: fetch actual job logs (GitHub REST API works
  unauthenticated for this public repo; `gh` CLI is NOT installed on
  this machine); every guess-based fix wasted a 15–40 min CI cycle.
- Runtime bugs: actually run the app — Kivy 2.3.1 + cryptography are
  installed in Windows Python 3.11 (`py -3.11`); a windowed smoke test
  with `Window.screenshot` catches what py_compile can't. WSL has only
  a buildozer venv (~/buildenv), no Kivy.
- Networking/crypto tests: two separate OS processes over loopback —
  `IDENTITY` is a per-process global; one-process simulations lie.
- Kivy fonts: bundled Roboto has NO emoji and no ●/○ glyphs — draw
  shapes (Ellipse/RoundedRectangle) instead of using symbol characters.
- The owner tests on real hardware and reports back with screenshots.
