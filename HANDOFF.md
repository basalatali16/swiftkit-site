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
