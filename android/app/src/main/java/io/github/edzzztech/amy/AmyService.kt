package io.github.edzzztech.amy

import android.app.Notification
import android.app.PendingIntent
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder
import androidx.lifecycle.LifecycleService
import io.github.edzzztech.amy.core.ActionLog
import io.github.edzzztech.amy.core.Tts

/**
 * Keeps Amy alive when the app is not in front. Android will kill a background
 * process that holds the microphone, so the listening loop has to live in a
 * foreground service with a visible notification — that is the platform's
 * bargain for always-on voice, not something we can design around.
 */
class AmyService : LifecycleService() {

    private var tts: Tts? = null
    private lateinit var actions: ActionLog

    override fun onCreate() {
        super.onCreate()
        actions = ActionLog(this)
        tts = Tts(this)
        // From Android 14 the service type must be declared at start time as
        // well as in the manifest, or the platform throws instead of starting.
        val notification = buildNotification("Listening")
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.UPSIDE_DOWN_CAKE) {
            startForeground(
                NOTIFICATION_ID,
                notification,
                ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE,
            )
        } else {
            startForeground(NOTIFICATION_ID, notification)
        }
        actions.record("system", "Service started")
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        super.onStartCommand(intent, flags, startId)
        // Restart if Android kills us under memory pressure.
        return START_STICKY
    }

    override fun onBind(intent: Intent): IBinder? {
        super.onBind(intent)
        return null
    }

    override fun onDestroy() {
        actions.record("system", "Service stopped")
        tts?.shutdown()
        tts = null
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
    }
}
