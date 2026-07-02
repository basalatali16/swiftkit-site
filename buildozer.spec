[app]

title = LANCOM
package.name = lancomm
package.domain = com.localnet

source.dir = .
source.include_exts = py,png,jpg,kv,atlas

version = 0.1

# Kivy 2.3.1 added support for newer CPython (3.13+), which matches the
# Python version python-for-android currently bundles by default. Pinning
# python3 downward to chase Kivy 2.3.0 caused a hostpython3/python3 version
# mismatch (see project README "Build history"); upgrading Kivy instead
# avoids fighting p4a's default toolchain.
requirements = python3,kivy==2.3.1,pyjnius,android,plyer,sqlite3

orientation = portrait
fullscreen = 0

icon.filename = %(source.dir)s/assets/icon.png

android.permissions = INTERNET,RECORD_AUDIO,ACCESS_WIFI_STATE,ACCESS_NETWORK_STATE,MODIFY_AUDIO_SETTINGS,READ_EXTERNAL_STORAGE,WRITE_EXTERNAL_STORAGE

android.api = 34
android.minapi = 24
android.archs = arm64-v8a, armeabi-v7a

# NDK version p4a recommended after the 25b build failures noted in the
# project history.
android.ndk = 28c

android.accept_sdk_license = True

[buildozer]
log_level = 2
warn_on_root = 1
