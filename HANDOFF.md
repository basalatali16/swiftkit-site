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
