package io.github.edzzztech.amy

import android.app.Notification
import android.app.PendingIntent
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.IBinder
import androidx.lifecycle.LifecycleService
import io.github.edzzztech.amy.core.Amy
import io.github.edzzztech.amy.core.WakeListener

/**
 * Keeps Amy alive when the app is not in front, and owns the listening loop.
 *
 * Android kills a background process holding the microphone, so always-on
 * voice has to live in a foreground service with a visible notification. That
 * is the platform's bargain, not a design choice.
 */
class AmyService : LifecycleService() {

    private var listener: WakeListener? = null

    override fun onCreate() {
        super.onCreate()
        Amy.start(this)

        // minSdk is 29, so the typed overload is always available; Android 14+
        // requires the type at start time as well as in the manifest.
        startForeground(
            NOTIFICATION_ID,
            buildNotification("Listening for “Amy”"),
            ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE,
        )

        val wake = WakeListener(this) { heard -> onHeard(heard) }
        listener = wake
        // Stop hearing her while she talks, or she answers her own voice.
        Amy.onTalkingChanged = { talking ->
            if (talking) wake.pauseForSpeech() else wake.resumeAfterSpeech()
        }
        wake.start()
        Amy.warmUp()        // load the model now, not on the first question
        Amy.actions.record("system", "Listening started")
    }

    private fun onHeard(heard: String) {
        when (heard) {
            WakeListener.INTERNAL_NAME_ONLY -> Amy.tts?.speak("Yes?")
            WakeListener.INTERNAL_STOPPED -> {
                Amy.actions.record("system", "Listening stopped by voice")
                Amy.tts?.speak("I'll stop listening.")
            }
            else -> Amy.submit(heard, spoken = true)
        }
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        super.onStartCommand(intent, flags, startId)
        when (intent?.action) {
            ACTION_LISTEN -> listener?.start()
            ACTION_SLEEP -> {
                Amy.stopSpeaking()
                listener?.stop()
            }
        }
        return START_STICKY      // restart if Android kills us under pressure
    }

    override fun onBind(intent: Intent): IBinder? {
        super.onBind(intent)
        return null
    }

    override fun onDestroy() {
        Amy.onTalkingChanged = null
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
