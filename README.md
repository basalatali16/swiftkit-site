# LANCOM

A LAN-only voice calling and text messaging Android app. Python/Kivy, no
internet connection, no server — devices on the same WiFi network discover
each other and talk directly, peer-to-peer.

## How it works

Set a display name on first launch. LANCOM broadcasts your presence over
UDP; any other device running the app on the same WiFi will show up in the
device list within a couple of seconds. From there you can send text
messages or start a voice call.

| Purpose            | Protocol / Port |
|---------------------|-----------------|
| Peer discovery       | UDP broadcast, 55555 |
| Text messaging        | TCP, 55556 |
| Call signaling (invite/accept/reject/hangup) | TCP, 55557 |
| Call audio (raw PCM16)  | UDP, 55558 |

Call audio is captured/played on Android via `pyjnius` bindings to the
native `android.media.AudioRecord` / `AudioTrack` APIs — `pyaudio` has no
python-for-android recipe, so it can't be bundled into the APK. On desktop,
`main.py` falls back to `pyaudio` (if installed) purely for local testing;
that path is never included in the Android build.

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

## Known limitations / open work

- **Audio quality is unverified end-to-end.** The `NativeAudioIO` path
  (pyjnius/AudioRecord/AudioTrack) replaces the previous no-op audio layer
  but hasn't been confirmed on-device yet. Verify by placing a call between
  two phones and confirming two-way audio.
- No packet loss handling / jitter buffer on the UDP audio stream — it's
  best-effort raw PCM, fine for a LAN but will glitch under WiFi congestion.
- Text messages sent while the recipient has the app backgrounded/killed
  are dropped (no store-and-forward).
- **Router AP isolation** is the #1 cause of "no devices found" — some
  routers block device-to-device traffic on the same WiFi (common on guest
  networks / some mesh systems). If discovery doesn't work, check the
  router's AP/client isolation setting first.

## Testing on a device

1. Download the APK from the GitHub Actions artifact (or build locally).
2. Sideload it onto the test device (Pixel 6a, arm64-v8a is the
   currently-targeted hardware).
3. Grant the microphone permission when prompted.
4. With two phones on the same WiFi, confirm each shows up in the other's
   device list, then test messaging and a call in both directions.

## Permissions

`INTERNET`, `RECORD_AUDIO`, `ACCESS_WIFI_STATE`, `ACCESS_NETWORK_STATE`,
`MODIFY_AUDIO_SETTINGS` — all required for LAN discovery/messaging and
voice-call audio. No data ever leaves the local network.
