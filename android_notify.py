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
CHANNEL_RING = "lancom_ring"  # silent channel: we loop the ringtone ourselves

CALL_NOTIF_ID = 2

# Locks held for the process's lifetime (never released on purpose) -
# kept here so the GC can't collect them, which would release them.
_held_locks = []

# Currently ringing incoming call: looping MediaPlayer (or Ringtone
# fallback) + vibrator, held so stop_ringing() can silence them.
_ring_player = None
_ringtone = None
_vibrator = None


def _ring_log(msg):
    """Ring failures are otherwise invisible on a sideloaded phone -
    keep a breadcrumb file we can ask the owner for."""
    try:
        base = os.environ.get("ANDROID_PRIVATE", ".")
        with open(os.path.join(base, "ring_debug.log"), "a",
                  encoding="utf-8") as f:
            f.write(msg + "\n")
    except Exception:
        pass


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
    _notify(CHANNEL_CALLS, "Calls", "Incoming call", peer_name,
            notif_id=CALL_NOTIF_ID, ringtone=True)


def start_ringing(peer_name, show_notification=True):
    """Ring like a phone until stop_ringing(): loop the device ringtone
    with MediaPlayer on the ring stream (reliable on every Android
    version - Ringtone.setLooping needs API 28 and quietly fails on some
    OEMs), vibrate in a call pattern, and with show_notification post a
    full-screen CATEGORY_CALL notification with Answer/Decline buttons
    that wakes a locked screen."""
    if not IS_ANDROID:
        return
    global _ring_player, _ringtone, _vibrator
    stop_ringing()

    from jnius import autoclass
    ctx = _context()

    # -- looping ringtone --------------------------------------------------
    try:
        MediaPlayer = autoclass("android.media.MediaPlayer")
        RingtoneManager = autoclass("android.media.RingtoneManager")
        AudioAttributes = autoclass("android.media.AudioAttributes")
        AABuilder = autoclass("android.media.AudioAttributes$Builder")
        uri = RingtoneManager.getDefaultUri(RingtoneManager.TYPE_RINGTONE)
        player = MediaPlayer()
        player.setDataSource(ctx, uri)
        attrs = (AABuilder()
                 .setUsage(AudioAttributes.USAGE_NOTIFICATION_RINGTONE)
                 .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION)
                 .build())
        player.setAudioAttributes(attrs)
        player.setLooping(True)
        player.prepare()
        player.start()
        _ring_player = player
    except Exception as exc:
        _ring_log(f"MediaPlayer ring failed: {exc!r}")
        # Fallback: plain Ringtone (single play on API < 28, still audible)
        try:
            RingtoneManager = autoclass("android.media.RingtoneManager")
            Build_VERSION = autoclass("android.os.Build$VERSION")
            uri = RingtoneManager.getDefaultUri(RingtoneManager.TYPE_RINGTONE)
            ringtone = RingtoneManager.getRingtone(ctx, uri)
            if ringtone is not None:
                if Build_VERSION.SDK_INT >= 28:
                    ringtone.setLooping(True)
                ringtone.play()
                _ringtone = ringtone
        except Exception as exc2:
            _ring_log(f"Ringtone fallback failed: {exc2!r}")

    # -- vibration ---------------------------------------------------------
    try:
        Context = autoclass("android.content.Context")
        vib = ctx.getSystemService(Context.VIBRATOR_SERVICE)
        if vib is not None and vib.hasVibrator():
            # 0ms wait, 900ms buzz, 700ms pause, repeat from index 0
            vib.vibrate([0, 900, 700], 0)
            _vibrator = vib
    except Exception as exc:
        _ring_log(f"vibrate failed: {exc!r}")

    if not show_notification:
        return
    try:
        from jnius import autoclass
        ctx, manager = _get_notification_manager()

        # Channel with NO sound of its own (we loop the ringtone above) -
        # a channel sound would play once, overlapping ours.
        Build_VERSION = autoclass("android.os.Build$VERSION")
        if Build_VERSION.SDK_INT >= 26:
            NotificationChannel = autoclass("android.app.NotificationChannel")
            NotificationManager = autoclass("android.app.NotificationManager")
            String = autoclass("java.lang.String")
            channel = NotificationChannel(String(CHANNEL_RING),
                                          String("Incoming calls"),
                                          NotificationManager.IMPORTANCE_HIGH)
            channel.setSound(None, None)
            channel.enableVibration(True)
            manager.createNotificationChannel(channel)

        NotificationBuilder = autoclass("android.app.Notification$Builder")
        Notification = autoclass("android.app.Notification")
        PendingIntent = autoclass("android.app.PendingIntent")
        String = autoclass("java.lang.String")

        builder = (NotificationBuilder(ctx, String(CHANNEL_RING))
                   if Build_VERSION.SDK_INT >= 26 else NotificationBuilder(ctx))
        builder.setContentTitle(String("Incoming call"))
        builder.setContentText(String(peer_name))
        builder.setSmallIcon(ctx.getApplicationInfo().icon)
        builder.setOngoing(True)
        builder.setCategory(Notification.CATEGORY_CALL)
        builder.setPriority(2)

        Intent = autoclass("android.content.Intent")
        launch = ctx.getPackageManager().getLaunchIntentForPackage(
            ctx.getPackageName())
        if launch is not None:
            flags = PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE
            pending = PendingIntent.getActivity(ctx, 0, launch, flags)
            builder.setContentIntent(pending)
            # Full-screen intent = wake the screen and show the call UI,
            # the way real dialers do. Needs USE_FULL_SCREEN_INTENT.
            builder.setFullScreenIntent(pending, True)

            # Answer / Decline buttons: same activity launch but tagged
            # with an extra the UI reads on (re)open and forwards to the
            # service. Distinct requestCodes keep the intents separate.
            icon = ctx.getApplicationInfo().icon
            answer = Intent(launch)
            answer.putExtra(String("ipphone_call_action"), String("accept"))
            decline = Intent(launch)
            decline.putExtra(String("ipphone_call_action"), String("reject"))
            builder.addAction(icon, String("Answer"),
                              PendingIntent.getActivity(ctx, 101, answer, flags))
            builder.addAction(icon, String("Decline"),
                              PendingIntent.getActivity(ctx, 102, decline, flags))

        manager.notify(CALL_NOTIF_ID, builder.build())
    except Exception as exc:
        _ring_log(f"call notification failed: {exc!r}")


def stop_ringing():
    """Silence ringtone + vibration, drop the incoming-call notification."""
    if not IS_ANDROID:
        return
    global _ring_player, _ringtone, _vibrator
    player, _ring_player = _ring_player, None
    if player is not None:
        try:
            player.stop()
            player.release()
        except Exception:
            pass
    ringtone, _ringtone = _ringtone, None
    if ringtone is not None:
        try:
            ringtone.stop()
        except Exception:
            pass
    vib, _vibrator = _vibrator, None
    if vib is not None:
        try:
            vib.cancel()
        except Exception:
            pass
    try:
        _ctx, manager = _get_notification_manager()
        manager.cancel(CALL_NOTIF_ID)
    except Exception:
        pass


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
