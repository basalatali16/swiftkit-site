# LANCOM

A LAN-only voice calling and text messaging Android app. Python/Kivy, no
internet connection, no server — devices on the same WiFi network discover
each other and talk directly, peer-to-peer.

## How it works

Set a display name on first launch. LANCOM broadcasts your presence over
UDP; any other device running the app on the same WiFi will show up in the
device list within a couple of seconds. From there you can send text
messages, share files, or start a voice call.

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
- **File transfer is unverified on-device.** `ChatScreen.on_send_file` uses
  `plyer.filechooser`, which on modern Android goes through the system's
  Storage Access Framework and can return a `content://` URI rather than a
  plain filesystem path on some Android versions/OEMs - `open(path, "rb")`
  would fail on that. Needs a real-device test alongside audio; if file
  sending fails immediately, this is the first thing to check.
- No encryption on any protocol - discovery, messaging, calls, and file
  transfer are all plaintext, appropriate for a trusted home LAN and
  nothing more sensitive than that.
- There is no iOS build. Kivy apps *can* target iOS via the separate
  `kivy-ios` toolchain, but that requires Xcode on a real Mac and, to
  install on an actual iPhone, code-signing with an Apple ID (free personal
  signing needs a Mac + USB cable; wider distribution needs a paid Apple
  Developer account) - none of which is available from this build
  environment. GitHub Actions does offer `macos-latest` runners with Xcode
  pre-installed, so a CI job that runs `kivy-ios` to confirm the source at
  least *builds* for iOS is realistic as a follow-up; turning that into
  something installable on your phone still requires your own Apple ID and
  Mac (or Xcode Cloud) for signing.

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
