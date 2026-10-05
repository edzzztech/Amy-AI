package io.github.edzzztech.amy

import android.app.Activity
import android.app.Application
import android.app.NotificationChannel
import android.app.NotificationManager
import android.os.Bundle
import io.github.edzzztech.amy.core.AmyState

class AmyApp : Application() {

    companion object {
        const val CHANNEL_ID = "amy_listening"
    }

    override fun onCreate() {
        super.onCreate()
        val channel = NotificationChannel(
            CHANNEL_ID,
            getString(R.string.service_channel),
            NotificationManager.IMPORTANCE_LOW,      // persistent, never buzzes
        ).apply {
            description = "Shown while Amy is running in the background."
            setShowBadge(false)
        }
        getSystemService(NotificationManager::class.java).createNotificationChannel(channel)
        registerActivityLifecycleCallbacks(VisibleScreens)
    }

    /**
     * Counts her visible screens to keep [AmyState.inForeground] true.
     *
     * Started and stopped rather than resumed and paused: moving from one of
     * her screens to another starts the next before stopping the last, so the
     * count never touches zero in between. All calls arrive on the main thread.
     */
    private object VisibleScreens : ActivityLifecycleCallbacks {
        private var started = 0

        override fun onActivityStarted(activity: Activity) {
            started++
            AmyState.inForeground = true
        }

        override fun onActivityStopped(activity: Activity) {
            started = (started - 1).coerceAtLeast(0)
            AmyState.inForeground = started > 0
        }

        override fun onActivityCreated(activity: Activity, savedInstanceState: Bundle?) {}
        override fun onActivityResumed(activity: Activity) {}
        override fun onActivityPaused(activity: Activity) {}
        override fun onActivitySaveInstanceState(activity: Activity, outState: Bundle) {}
        override fun onActivityDestroyed(activity: Activity) {}
    }
}
