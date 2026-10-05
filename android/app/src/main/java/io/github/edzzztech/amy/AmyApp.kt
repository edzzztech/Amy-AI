package io.github.edzzztech.amy

import android.app.Application
import android.app.NotificationChannel
import android.app.NotificationManager

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
    }
}
