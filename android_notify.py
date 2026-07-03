"""
Android notifications and background-friendliness for LANCOM. No-ops
everywhere else (desktop testing).

Kivy-free on purpose: this module is used by BOTH processes - the app
UI and the foreground service (service.py). Everything resolves its
Android context at call time via _context(), which returns the activity
in the app process and the service otherwise.
"""

import os

IS_ANDROID = "ANDROID_ARGUMENT" in os.environ

CHANNEL_MESSAGES = "lancom_messages"
CHANNEL_CALLS = "lancom_calls"

# Locks held for the process's lifetime (never released on purpose) -
# kept here so the GC can't collect them, which would release them.
_held_locks = []


def _context():
    from jnius import autoclass
    activity = autoclass("org.kivy.android.PythonActivity").mActivity
    if activity is not None:
        return activity
    return autoclass("org.kivy.android.PythonService").mService


def acquire_background_locks():
    """Grab the three locks a LAN communicator needs on Android:

    - MulticastLock: without it Android silently DROPS incoming UDP
      broadcast packets on most devices - discovery can fail even in the
      foreground. This one is not optional for this app.
    - WifiLock (FULL_HIGH_PERF): keeps WiFi awake and out of power-save
      when the screen goes off, so messages/calls still arrive.
    - Partial WakeLock: keeps the CPU serviceable in the background so
      the listener threads actually run.

    Deliberately never released: LANCOM's whole job is to be reachable.
    The battery cost is the documented trade-off, softened by the
    battery-optimization exemption the app already asks for."""
    if not IS_ANDROID or _held_locks:
        return
    try:
        from jnius import autoclass
        Context = autoclass("android.content.Context")
        PowerManager = autoclass("android.os.PowerManager")
        app_ctx = _context().getApplicationContext()

        wifi = app_ctx.getSystemService(Context.WIFI_SERVICE)
        multicast = wifi.createMulticastLock("lancom:multicast")
        multicast.setReferenceCounted(False)
        multicast.acquire()
        _held_locks.append(multicast)

        wifi_lock = wifi.createWifiLock(3, "lancom:wifi")  # 3 = WIFI_MODE_FULL_HIGH_PERF
        wifi_lock.setReferenceCounted(False)
        wifi_lock.acquire()
        _held_locks.append(wifi_lock)

        power = app_ctx.getSystemService(Context.POWER_SERVICE)
        wake = power.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "lancom:net")
        wake.setReferenceCounted(False)
        wake.acquire()
        _held_locks.append(wake)
    except Exception:
        pass  # degraded background behavior beats crashing


def _get_notification_manager():
    from jnius import autoclass, cast
    Context = autoclass("android.content.Context")
    NotificationManager = autoclass("android.app.NotificationManager")
    ctx = _context()
    return ctx, cast(NotificationManager,
                     ctx.getSystemService(Context.NOTIFICATION_SERVICE))


def _ensure_channel(manager, channel_id, name, importance_high=True):
    from jnius import autoclass
    Build_VERSION = autoclass("android.os.Build$VERSION")
    if Build_VERSION.SDK_INT < 26:
        return
    NotificationChannel = autoclass("android.app.NotificationChannel")
    NotificationManager = autoclass("android.app.NotificationManager")
    String = autoclass("java.lang.String")
    importance = (NotificationManager.IMPORTANCE_HIGH if importance_high
                  else NotificationManager.IMPORTANCE_DEFAULT)
    channel = NotificationChannel(String(channel_id), String(name), importance)
    channel.enableVibration(True)
    manager.createNotificationChannel(channel)


def _notify(channel_id, channel_name, title, text, notif_id, ringtone=False):
    if not IS_ANDROID:
        return
    try:
        from jnius import autoclass
        ctx, manager = _get_notification_manager()
        _ensure_channel(manager, channel_id, channel_name)

        NotificationBuilder = autoclass("android.app.Notification$Builder")
        Build_VERSION = autoclass("android.os.Build$VERSION")
        String = autoclass("java.lang.String")
        RingtoneManager = autoclass("android.media.RingtoneManager")

        builder = (NotificationBuilder(ctx, String(channel_id))
                   if Build_VERSION.SDK_INT >= 26 else NotificationBuilder(ctx))
        builder.setContentTitle(String(title))
        builder.setContentText(String(text))
        builder.setSmallIcon(ctx.getApplicationInfo().icon)
        builder.setAutoCancel(True)
        builder.setPriority(2)  # Notification.PRIORITY_MAX, ignored on API 26+ (channel importance rules instead)

        # Tapping the notification opens (or foregrounds) LANCOM. The
        # launch intent works from both the activity and the service.
        PendingIntent = autoclass("android.app.PendingIntent")
        launch = ctx.getPackageManager().getLaunchIntentForPackage(
            ctx.getPackageName())
        if launch is not None:
            pending = PendingIntent.getActivity(
                ctx, 0, launch,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE)
            builder.setContentIntent(pending)

        sound_type = RingtoneManager.TYPE_RINGTONE if ringtone else RingtoneManager.TYPE_NOTIFICATION
        builder.setSound(RingtoneManager.getDefaultUri(sound_type))

        manager.notify(notif_id, builder.build())
    except Exception:
        pass  # a notification failure should never crash the app


def notify_message(peer_name, text):
    preview = text if len(text) <= 80 else text[:77] + "..."
    _notify(CHANNEL_MESSAGES, "Messages", peer_name, preview, notif_id=1)


def notify_incoming_call(peer_name):
    _notify(CHANNEL_CALLS, "Calls", "Incoming call", peer_name, notif_id=2, ringtone=True)


def request_ignore_battery_optimizations():
    """Prompts the user to exempt LANCOM from Android's battery
    optimization, which otherwise can suspend/kill background network
    activity - needed for messages/calls to arrive reliably while the app
    isn't in the foreground. A system settings dialog, not a runtime
    permission, so it's requested separately from request_permissions().
    Only callable from the app process (needs the activity)."""
    if not IS_ANDROID:
        return
    try:
        from jnius import autoclass
        PythonActivity = autoclass("org.kivy.android.PythonActivity")
        Intent = autoclass("android.content.Intent")
        Settings = autoclass("android.provider.Settings")
        Uri = autoclass("android.net.Uri")
        Context = autoclass("android.content.Context")
        String = autoclass("java.lang.String")

        activity = PythonActivity.mActivity
        package_name = activity.getPackageName()
        power_manager = activity.getSystemService(Context.POWER_SERVICE)
        if power_manager.isIgnoringBatteryOptimizations(package_name):
            return
        intent = Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS)
        intent.setData(Uri.parse(String("package:" + package_name)))
        activity.startActivity(intent)
    except Exception:
        pass
