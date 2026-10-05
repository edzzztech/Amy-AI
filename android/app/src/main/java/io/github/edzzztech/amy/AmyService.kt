package io.github.edzzztech.amy

import android.app.Notification
import android.app.PendingIntent
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder
import androidx.lifecycle.LifecycleService
import io.github.edzzztech.amy.core.Amy
import io.github.edzzztech.amy.core.AmyState
import io.github.edzzztech.amy.core.Listening
import io.github.edzzztech.amy.core.WakeListener

/**
 * Keeps Amy alive when the app is not in front, and owns the listening loop.
 *
 * Android will kill a background process holding the microphone, so always-on
 * voice has to live in a foreground service with a visible notification. That
 * is the platform's bargain, not a design choice.
 */
class AmyService : LifecycleService() {

    private var listener: WakeListener? = null

    override fun onCreate() {
        super.onCreate()
        Amy.start(this)

        val notification = buildNotification("Listening for “Amy”")
        // From Android 14 the type must be declared at start time as well as in
        // the manifest, or the platform throws instead of starting.
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.UPSIDE_DOWN_CAKE) {
            startForeground(
                NOTIFICATION_ID,
                notification,
                ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE,
            )
        } else {
            startForeground(NOTIFICATION_ID, notification)
        }

        listener = WakeListener(this) { heard -> onHeard(heard) }.also { it.start() }
        Amy.actions.record("system", "Listening started")
    }

    private fun onHeard(heard: String) {
        when (heard) {
            WakeListener.INTERNAL_NAME_ONLY -> Amy.tts?.speak("Yes?")
            WakeListener.INTERNAL_STOPPED -> {
                AmyState.setState(Listening.Muted)
                Amy.actions.record("system", "Listening stopped by voice")
                Amy.tts?.speak("I'll stop listening.")
            }
            else -> {
                Amy.submit(heard, spoken = true)
                listener?.markExchange()
            }
        }
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        super.onStartCommand(intent, flags, startId)
        when (intent?.action) {
            ACTION_LISTEN -> listener?.start()
            ACTION_SLEEP -> {
                listener?.stop()
                AmyState.setState(Listening.Muted)
            }
        }
        return START_STICKY      // restart if Android kills us under pressure
    }

    override fun onBind(intent: Intent): IBinder? {
        super.onBind(intent)
        return null
    }

    override fun onDestroy() {
        listener?.release()
        listener = null
        Amy.actions.record("system", "Listening stopped")
        super.onDestroy()
    }

    private fun buildNotification(state: String): Notification {
        val open = PendingIntent.getActivity(
            this, 0,
            Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE,
        )
        return Notification.Builder(this, AmyApp.CHANNEL_ID)
            .setContentTitle("Amy")
            .setContentText(state)
            .setSmallIcon(android.R.drawable.ic_btn_speak_now)
            .setContentIntent(open)
            .setOngoing(true)
            .build()
    }

    companion object {
        private const val NOTIFICATION_ID = 1
        const val ACTION_LISTEN = "io.github.edzzztech.amy.LISTEN"
        const val ACTION_SLEEP = "io.github.edzzztech.amy.SLEEP"
    }
}
