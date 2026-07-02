# LANCOM — Session Handoff (2026-07-02)

Read this first when continuing work on this project in a new session.
This file is the handoff from the previous Claude Code session. The
README.md has the full build history and known limitations; this file is
the "where we are right now and what's next" state.

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
open). The session was subscribed to PR activity to auto-investigate CI
failures.

## Current state (all committed & pushed as of 4dfd856)

Everything below is implemented, py_compile-clean, and pushed. Local
integration tests (two real OS processes over loopback) passed for
messaging, encryption, and resumable file transfer. NONE of it has been
verified on a real Android device since the last APK the owner tested.

- **Discovery**: UDP broadcast HELLO on 55555, identity-verified
  (peer id must hash from the claimed pubkey). Persistent contacts with
  online/offline + last-seen (SQLite via `store.py`).
- **Messaging** (TCP 55556), **call signaling** (TCP 55557), **call
  audio** (UDP 55558, native AudioRecord/AudioTrack via pyjnius),
  **file transfer** (TCP 55559).
- **End-to-end encryption everywhere** (`crypto_util.py`): X25519
  identity keys, ChaCha20-Poly1305, self-certifying device ids,
  fingerprint verification popup ("ID" button in chat). No forward
  secrecy — deliberate, documented trade-off in README. Local DB is
  encrypted at rest (message text/filenames).
- **Resumable file transfer** (built for multi-GB files): `transfers`
  DB table + on-disk `.partial` files; receiver acks resume offset;
  fresh per-attempt key/salt so resume never reuses nonces; inline
  Retry button in chat on failure. Android `content://` URIs from the
  file picker are resolved via ContentResolver
  (`resolve_android_content_uri` in main.py).
- **Notifications + background** (`android_notify.py`): message/call
  notifications with sound when backgrounded, POST_NOTIFICATIONS
  requested up front with all other permissions, battery-optimization
  exemption prompt, `on_pause() -> True`. Deliberately NOT a foreground
  service (toolchain risk — see android_notify.py docstring); if OEMs
  still kill the app in background, foreground service is the next step.
- **UI**: dark navy/gold "luxury" theme, avatars, online dots, chat
  history timeline (messages + calls + files interleaved).

## CI status — CHECK THIS FIRST

The last 3 builds failed on the `cryptography` dependency chain:
1. rustup missing → fixed (separate Docker step installs Rust; see
   comments in build.yml — do NOT override the kivy/buildozer image
   entrypoint for the build step itself, it breaks user mapping).
2. entrypoint/root crash → fixed (same).
3. armeabi-v7a Rust cross-compile failure ("LONG_BIT definition appears
   wrong") → fixed by dropping armeabi-v7a; now arm64-v8a only.

Commit `4dfd856` triggered a new build with the arch fix + all features
above. **Its result was unknown when the session ended.** First action
in a new session: check the latest "Build LANCOM APK" workflow run on
PR #1. If green, tell the owner to download `lancom-debug-apk` from the
run's artifacts, extract the zip fully, sideload, and test on two
phones. If red, read the job logs before changing anything — every
prior fix came from reading actual logs, not guessing.

## What was in progress (designed, NOT yet written)

The session ended while starting these two tasks. No code for them
exists yet:

1. **WhatsApp-style UI polish** (owner: "INTERFACE IS SO BASIC TRY
   MAKING IT MORE SMOOTH LIKE WHATSAPP HAVE"). Planned: real rounded
   chat bubbles (mine right-aligned/warm gold-dark, theirs left/card
   color, max ~75% width, timestamp inside bubble, drop sender names in
   1:1 chat), date separator chips, rounded buttons with pressed
   states, pill-shaped message input with "+" attach button, slimmer
   chat header. All in `KV` string + `ChatBubble`/`ChatEvent`/
   `ContactRow` classes in main.py.
2. **Manual add-contact-by-IP** (fallback for offices where routers
   block broadcast). Planned design: add unicast "probe" HELLOs —
   `PeerDiscovery.probe_ip(ip)` sends HELLO with `"probe": true` to
   (ip, 55555); listener replies with a unicast HELLO so both sides
   learn each other; broadcast loop also re-probes known peers whose
   last_seen is stale (> PEER_TIMEOUT/2) so broadcast-blocked pairs
   stay "online" without flicker. UI: "+ IP" button on UsersScreen
   header → popup with IP input (each device's own IP is already shown
   in the debug label on that screen).

## Owner's standing requirements (their words, condensed)

- Background operation with notifications + sound: DONE, unverified.
- WhatsApp-smooth interface: IN PROGRESS (see above).
- File sharing must work and resume after connection loss, files "in
  GBs": DONE, unverified on device.
- Best solution for large offices (IP-based calls/texts/files on one
  WiFi): add-by-IP feature above is the missing piece.
- All permissions requested at once on first open: DONE.
- Security "the best" since they'll sell it: E2E encryption DONE.
- iOS AFTER Android is finalized ("first finalize android then we'll
  move on ios"). Owner has a Mac and Windows PC but prefers testing on
  an old iPad running iOS 12 — flagged in README that current
  kivy-ios/Xcode may not target iOS 12 at all; verify before promising.

## Working practices that saved time (keep doing these)

- CI failures: fetch actual job logs (GitHub MCP `get_job_logs`);
  every guess-based fix wasted a 15–40 min CI cycle.
- Runtime bugs: actually run the app locally with Kivy + `xvfb-run`
  (py_compile misses things like the ScreenManager root-widget crash).
- Networking/crypto tests: two separate OS processes over loopback —
  `IDENTITY` is a per-process global; one-process simulations lie.
- The owner tests on real hardware and reports back with screenshots;
  known past confusion: tried installing the APK from inside a RAR
  viewer — remind them to fully extract downloads first.
