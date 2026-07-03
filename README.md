# LANCOM

A LAN-only voice calling and text messaging Android app. Python/Kivy, no
internet connection, no server — devices on the same WiFi network discover
each other and talk directly, peer-to-peer.

## How it works

Set a display name on first launch. LANCOM broadcasts your presence over
UDP; any other device running the app on the same WiFi will show up in the
device list within a couple of seconds. From there you can send text
messages, share files, or start a voice call.

On networks where the router filters UDP broadcast (common in offices),
automatic discovery can't work — use the "+ IP" button on the contacts
screen and type the other device's IP address (each device shows its own
IP at the top of that screen). This sends a direct unicast HELLO probe;
the other device answers straight back, so both sides learn each other
from one probe. Known contacts that go quiet are also re-probed directly
every broadcast interval, which keeps contacts online (and re-finds them
after an app restart) without any broadcast traffic at all.

Every contact you've ever seen — online or not — stays in your device list
with an online/offline status and a "last seen" time, and every message,
call, and file transfer is saved locally (SQLite, in the app's private
storage) so history survives app restarts even for contacts that are
currently offline. There is no server and nothing is synced anywhere:
"offline" messaging means the app remembers what *you* sent/received, not
that it delivers messages to someone who wasn't there to receive them.

| Purpose            | Protocol / Port |
|---------------------|-----------------|
| Peer discovery       | UDP broadcast, 55555 |
| Text messaging        | TCP, 55556 |
| Call signaling (invite/accept/reject/hangup) | TCP, 55557 |
| Call audio (raw PCM16)  | UDP, 55558 |
| File transfer          | TCP, 55559 |

Call audio is captured/played on Android via `pyjnius` bindings to the
native `android.media.AudioRecord` / `AudioTrack` APIs — `pyaudio` has no
python-for-android recipe, so it can't be bundled into the APK. On desktop,
`main.py` falls back to `pyaudio` (if installed) purely for local testing;
that path is never included in the Android build.

File transfer streams the raw file bytes directly over its own TCP
connection (after a small JSON header announcing the filename/size) rather
than wrapping the payload in base64/JSON, so it isn't limited to small
files the way the messaging/signaling protocol is.

## Security

Every device generates a persistent X25519 identity keypair on first
launch (`crypto_util.Identity`, stored in the app's private storage). A
device's id is `SHA-256(public_key)[:16]` — self-certifying, so nothing on
the LAN can claim to be a contact it doesn't hold the private key for.

- **Messaging, call signaling, and file transfer are all
  authenticated-encrypted** (ChaCha20-Poly1305), never plaintext. The
  symmetric key for a given pair of devices is derived via X25519 ECDH
  between their identity keys. There's no plaintext fallback: if we don't
  have a peer's public key (i.e. we've never seen a discovery broadcast
  from them), we simply can't talk to them.
- **Call audio** gets its own key per call (derived from the pair's shared
  secret plus a random salt exchanged during signaling), so audio keys
  aren't reused across calls.
- **Local storage**: message text and filenames are encrypted at rest
  (key derived from the device's own identity key, domain-separated from
  the transport key, never transmitted) — protects the SQLite file if
  pulled off the device.
- **Manual verification**: tap "ID" in a chat to see a short fingerprint
  for both devices (like a Signal/SSH safety number) - read them aloud to
  confirm you're really talking to who you think, the same way TOFU
  ("trust on first use") systems recommend.

**What this does *not* give you**: the shared key per pair is static (same
key every time, derived from long-term identity keys), not renegotiated
per session — deliberately: a simple, correct scheme beats a home-grown
ratcheting/forward-secrecy protocol implemented under time pressure. This
means compromise of either device's long-term private key can
retroactively decrypt captured traffic between that pair. It does stop
passive eavesdropping and active tampering/spoofing by any other device on
the LAN, and messages/calls/files can't be intercepted or forged by a
third device even with router/ARP-level access to the network.

This is new and only unit/integration-tested locally (two real separate
processes on loopback, not two phones) — see "Known limitations".

## Building the APK

### GitHub Actions (recommended)

Pushing to `main` or any `claude/**` branch (when `main.py`,
`buildozer.spec`, or `assets/` change) triggers
`.github/workflows/build.yml`, which uses
[`ArtemSBulgakov/buildozer-action@v1`](https://github.com/ArtemSBulgakov/buildozer-action)
to run `buildozer android debug` in a prebuilt Android SDK/NDK container and
uploads the resulting APK as a workflow artifact (`lancom-debug-apk`). You
can also trigger it manually via `workflow_dispatch`.

### Local build

```bash
python3 -m venv ~/buildenv
source ~/buildenv/bin/activate
pip install buildozer cython
buildozer android debug
```

The APK lands in `bin/`.

## Build history & fixes applied

1. `pip install buildozer` needed `--break-system-packages` on the original
   host; later moved into a dedicated venv (`~/buildenv`) to avoid that
   entirely.
2. Building from a `/mnt/c/...` WSL mount failed with a `chmod` error —
   buildozer needs a native Linux filesystem. Build from a path inside the
   WSL filesystem (e.g. `~/lancomm`), never a Windows mount.
3. `pyaudio` was removed from requirements — there is no python-for-android
   recipe for it. Call audio now goes through `pyjnius` +
   `AudioRecord`/`AudioTrack` directly (see `AudioIO` in `main.py`) instead
   of a dead/no-op audio layer.
4. Kivy 2.3.0 + the Python version p4a bundles by default (3.14.x) failed to
   compile Cython extensions. Pinning `python3==3.11.6` downward failed too
   (`hostpython3`/`python3` version mismatch from a stale cache). The fix
   that actually works: **upgrade Kivy to 2.3.1+ instead**, which added
   support for newer CPython — see `buildozer.spec`. If a fresh build still
   hits Cython errors, clear the cache first:
   ```bash
   buildozer android clean
   rm -rf ~/.buildozer/android/packages/{hostpython3,python3}
   ```
5. NDK bumped from 25b to 28c per python-for-android's own recommendation
   (`android.ndk` in `buildozer.spec`).
6. Build moved to GitHub Actions because local builds require a Linux (or
   WSL) machine with the full SDK/NDK toolchain, which isn't always
   available.
7. The app crashed on every launch: `LancomApp.build()` parsed the KV
   markup with `Builder.load_string(KV)` but discarded the returned root
   widget and returned a fresh, empty `ScreenManager()` instead - so
   `on_start()`'s `self.root.current = "setup"` always raised
   `ScreenManagerException: No Screen with name "setup"`. Fixed by
   returning `Builder.load_string(KV)` directly. Only caught by actually
   running the app (Kivy + Xvfb locally); `py_compile` can't catch a wrong
   root widget.
8. Discovery never worked even after the crash fix: `get_local_ip()`
   connected a UDP socket to the broadcast address `255.255.255.255` to
   read back the local interface IP via `getsockname()`. Connecting to a
   broadcast destination requires `SO_BROADCAST` first; without it this
   raises `PermissionError` (an `OSError` subclass) on Linux/Android,
   which was silently caught and fell back to `127.0.0.1` - so every
   device broadcast to its own loopback address and never reached the
   network. Fixed by connecting to a plain unicast address instead
   (`8.8.8.8:80` - no packets are actually sent for a UDP `connect()`, so
   this needs no special permissions and no real internet access, just a
   route). Also added an Android-specific path that reads the IP straight
   from `WifiManager`, used first when available, since this app must work
   even on WiFi networks with no internet gateway where OS route
   resolution can be unreliable.
9. Added `cryptography` to `buildozer.spec` requirements for the
   encryption layer (see "Security" above) - it has an actively-maintained
   python-for-android recipe, but this is the project's first dependency
   with native/Rust build steps, so it's the first thing to check if a
   build fails after this point.
10. `cryptography`'s Rust build needs a real Rust toolchain, which the
    `kivy/buildozer` Docker image doesn't ship. Installing it (rustup) as
    a separate step is safe, but the toolchain then failed to cross-compile
    the `_openssl` C shim for `armeabi-v7a` (32-bit ARM) specifically:
    `"LONG_BIT definition appears wrong for platform"`, from `pyport.h`
    while building against what looks like a mismatched host/target Python
    header set inside p4a's `hostpython3` build - not something fixable
    from application code. Fixed by dropping `armeabi-v7a` from
    `android.archs` (now `arm64-v8a` only, see `buildozer.spec`); all real
    test hardware for this app is 64-bit, so this isn't a functional loss.

## Known limitations / open work

- **Audio quality is unverified end-to-end.** The `NativeAudioIO` path
  (pyjnius/AudioRecord/AudioTrack) replaces the previous no-op audio layer
  but hasn't been confirmed on-device yet. Verify by placing a call between
  two phones and confirming two-way audio.
- No packet loss handling / jitter buffer on the UDP audio stream — it's
  best-effort raw PCM, fine for a LAN but will glitch under WiFi congestion.
- "Offline" contacts show saved history and let you attempt to send a
  message/file, but there's no real store-and-forward: if they're actually
  offline the send just fails (shown inline in the chat). It does not
  retry or deliver once they come back online.
- **Router AP isolation** is the #1 cause of "no devices found" — some
  routers block device-to-device traffic on the same WiFi (common on guest
  networks / some mesh systems). If discovery doesn't work, check the
  router's AP/client isolation setting first.
- **Discovery assumes a /24 subnet** (`get_broadcast_ip` in `main.py` builds
  the broadcast address as `x.y.z.255`). On networks with a different mask
  (common on enterprise WiFi or some hotspot configs), the broadcast won't
  reach every device and this looks identical to the AP-isolation failure
  above.
- **A peer's IP is cached for up to `PEER_TIMEOUT` (7s) after it disappears.**
  If another device is assigned that IP by DHCP in that window, a call or
  message aimed at the original peer's name can briefly reach the new
  device instead. Low-probability on a typical home network, but worth
  knowing before relying on this for anything sensitive.
- **File transfer is resumable but unverified on-device.** Large transfers
  (built for multi-GB files) survive a dropped connection: the receiver
  tracks progress in the `transfers` table plus an on-disk `.partial` file,
  and a retry picks up from the last acknowledged byte instead of starting
  over (each retry attempt still gets its own fresh encryption key/salt, so
  nothing about resuming weakens the encryption). `ChatScreen` shows a
  "Retry" button inline in the chat when a send fails. `plyer.filechooser`
  can return a `content://` URI instead of a plain filesystem path on
  modern Android (Storage Access Framework) - handled by copying it to a
  local temp file via `ContentResolver` first
  (`resolve_android_content_uri` in `main.py`). None of this has run on a
  real device yet; needs a real-device test alongside audio.
- **Background operation and notifications are unverified on-device.** The
  app requests all permissions (including `POST_NOTIFICATIONS`) up front on
  first launch, asks to be exempted from battery optimization, and posts a
  system notification (with sound) for incoming messages/calls when not in
  the foreground - see `android_notify.py`. This is the lighter-weight
  `on_pause() -> True` approach, not a full Android foreground service (see
  that file's docstring for the trade-off); very aggressive OEM battery
  managers (common on some Chinese Android skins) may still kill it in the
  background regardless.
- **Encryption is new and unverified on real devices.** It's covered by
  integration tests (two real OS processes talking over loopback,
  simulating two phones) that confirm messages/files decrypt correctly,
  raw DB bytes aren't plaintext, and tampered/wrong-key ciphertext is
  rejected - but it hasn't run between two actual Android phones yet. If
  messaging/calls/files stop working after updating, this is the first
  thing to suspect (e.g. a peer's stored public key not being found).
- Peer discovery (the UDP broadcast HELLO) is authenticated (the id must
  match the claimed public key) but not encrypted - your display name and
  the fact that you're running LANCOM are visible to anyone on the LAN.
  Nothing else is.
- No forward secrecy (see "Security" above) - a deliberate simplicity
  trade-off, not an oversight.
- Discovery, messaging, and call signaling have no rate limiting - a
  malicious device on the LAN could still flood a phone with connection
  attempts (denial of service), even though it can't forge or read
  content anymore.
- There is no iOS build. Kivy apps *can* target iOS via the separate
  `kivy-ios` toolchain, but that requires Xcode on a real Mac and, to
  install on an actual iPhone/iPad, code-signing with an Apple ID (free
  personal signing needs a Mac + USB cable; wider distribution needs a
  paid Apple Developer account). Separately, very old iOS versions (e.g.
  iOS 12, released 2018) may not be a supported deployment target for
  current Xcode/kivy-ios at all, independent of the signing question -
  this needs checking before investing time in a build aimed at old
  hardware.

## Testing on a device

1. Download the APK from the GitHub Actions artifact (or build locally).
2. Sideload it onto the test device (Pixel 6a, arm64-v8a is the
   currently-targeted hardware).
3. Grant the microphone permission when prompted.
4. With two phones on the same WiFi, confirm each shows up in the other's
   device list, then test messaging and a call in both directions.

## Permissions

`INTERNET`, `RECORD_AUDIO`, `ACCESS_WIFI_STATE`, `ACCESS_NETWORK_STATE`,
`MODIFY_AUDIO_SETTINGS`, `READ_EXTERNAL_STORAGE`, `WRITE_EXTERNAL_STORAGE`
— required for LAN discovery/messaging, voice-call audio, and picking/
saving files for transfer. No data ever leaves the local network; there is
no server and no analytics.
