package io.github.edzzztech.amy

import android.app.Notification
import android.app.PendingIntent
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder
import androidx.lifecycle.LifecycleService
import io.github.edzzztech.amy.core.Amy
import io.github.edzzztech.amy.core.ListenSetting
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

        // Android 14+ requires the type at start time as well as in the manifest.
        try {
            val notice = buildNotification("Listening for “Amy”")
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
                startForeground(NOTIFICATION_ID, notice, ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE)
            } else {
                // Android 10 has no microphone type; the manifest covers it.
                startForeground(NOTIFICATION_ID, notice)
            }
        } catch (e: Exception) {
            // Android refuses a microphone service started from the
            // background — a restart after being killed, on recent versions.
            // Uncaught, that crashed the app every time Android retried.
            // Leave a one-tap way back instead.
            Amy.actions.record("system", "Not allowed to listen in the background",
                outcome = "failed", detail = e.message)
            ResumeNotice.post(this)
            stopSelf()
            return
        }
        ResumeNotice.clear(this)

        val wake = WakeListener(this) { heard -> onHeard(heard) }
        listener = wake
        // Stop hearing her while she talks, or she answers her own voice.
        Amy.onTalkingChanged = { talking ->
            if (talking) wake.pauseForSpeech() else wake.resumeAfterSpeech()
        }
        // Put to sleep before the service was last stopped? Then stay asleep.
        val listening = ListenSetting.wanted(this)
        if (listening) wake.start() else wake.stop()
        Amy.warmUp()        // load the model now, not on the first question
        Amy.actions.record("system", if (listening) "Listening started" else "Started asleep")
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
