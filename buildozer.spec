[app]

title = LANCOM
package.name = lancomm
package.domain = com.localnet

source.dir = .
source.include_exts = py,png,jpg,kv,atlas

version = 0.5

# The entire networking stack runs in this sticky foreground service
# (service.py), so messages/calls/files keep arriving with the app
# closed, the task swiped away, or the phone locked. dataSync is the
# closest API-34 foreground-service type for "keeps a communication
# channel synced"; sticky + setAutoRestartService(True) inside
# service.py bring it back if Android or the user kills it.
services = lancomnet:service.py:foreground:sticky:foregroundServiceType=dataSync

# Kivy 2.3.1 added support for newer CPython (3.13+), which matches the
# Python version python-for-android currently bundles by default. Pinning
# python3 downward to chase Kivy 2.3.0 caused a hostpython3/python3 version
# mismatch (see project README "Build history"); upgrading Kivy instead
# avoids fighting p4a's default toolchain.
# cryptography has an actively-maintained p4a recipe (Rust + OpenSSL
# bindings) - used for the X25519/ChaCha20-Poly1305 end-to-end encryption
# layer. This is the first time this project has added a dependency with
# native/Rust build steps; if it fails in CI, that's the first thing to
# check (see README "Build history").
requirements = python3,kivy==2.3.1,pyjnius,android,plyer,sqlite3,cryptography

orientation = portrait
fullscreen = 0

icon.filename = %(source.dir)s/assets/icon.png
presplash.filename = %(source.dir)s/assets/presplash.png
# Matches the presplash/app background so the splash doesn't flash white.
android.presplash_color = #060810

# WAKE_LOCK + CHANGE_WIFI_MULTICAST_STATE: required for the background
# locks (android_notify.acquire_background_locks) that keep discovery and
# message delivery working with the screen off. Both are install-time
# permissions - no user prompt.
android.permissions = INTERNET,RECORD_AUDIO,ACCESS_WIFI_STATE,ACCESS_NETWORK_STATE,MODIFY_AUDIO_SETTINGS,READ_EXTERNAL_STORAGE,WRITE_EXTERNAL_STORAGE,POST_NOTIFICATIONS,FOREGROUND_SERVICE,FOREGROUND_SERVICE_DATA_SYNC,WAKE_LOCK,WAKE_LOCK,CHANGE_WIFI_MULTICAST_STATE

android.api = 34
android.minapi = 24
# armeabi-v7a (32-bit) dropped: cryptography's Rust build (_openssl shim)
# fails cross-compiling for that target specifically ("LONG_BIT definition
# appears wrong for platform" - a host/target Python header mismatch deep in
# p4a's hostpython3 build, not something fixable from this project's side).
# All actual test hardware for this app is 64-bit, so arm64-v8a-only is a
# pragmatic drop, not a real feature loss.
android.archs = arm64-v8a

# NDK version p4a recommended after the 25b build failures noted in the
# project history.
android.ndk = 28c

android.accept_sdk_license = True

# The APK from the release p4a crashed on device with
#   ImportError: dlopen failed: cannot locate symbol "_Py_TrueStruct"
#   referenced by .../cryptography/hazmat/bindings/_rust.abi3.so
# Rust-built extensions weren't explicitly linked against libpython, and
# Android's bionic linker (no lazy binding) refuses to resolve those
# symbols at load time. Fixed in p4a develop by PR #3333 (2026-05-21,
# adds -Clink-arg=-lpython3.x to RUSTFLAGS), which is NOT in any tagged
# release yet (latest: v2026.05.09). Pinned to a develop commit that
# contains the fix; drop these two lines once a release includes it.
p4a.branch = develop
p4a.commit = 10d4798ecfec22c0f6765638c4f81fd1811b351a

[buildozer]
log_level = 2
warn_on_root = 1
