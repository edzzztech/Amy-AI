package io.github.edzzztech.amy

import android.Manifest
import android.app.Notification
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import io.github.edzzztech.amy.core.ListenSetting

/**
 * Brings her back after the phone restarts, if she was listening when it went
 * off. Asleep stays asleep.
 *
 * On Android 10 she simply starts listening again. Later versions do not allow
 * that: from 11 a microphone service started at boot hears nothing, because
 * the microphone is kept for apps the user has in front, and from 14 it is
 * refused outright. So there she posts a notification instead, and one tap
 * has her listening.
 */
class BootReceiver : BroadcastReceiver() {

    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action != Intent.ACTION_BOOT_COMPLETED) return
        if (!ListenSetting.wanted(context)) return
        if (context.checkSelfPermission(Manifest.permission.RECORD_AUDIO) !=
            PackageManager.PERMISSION_GRANTED
        ) return

        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.R) {
            try {
                context.startForegroundService(Intent(context, AmyService::class.java))
                return
            } catch (e: Exception) {
                // Fall through to the notification.
            }
        }
        ResumeNotice.post(context)
    }
}

/**
 * "Tap to start listening": shown after a restart, and whenever Android stops
 * her listening in the background, so the way back is always one tap.
 */
object ResumeNotice {

    private const val ID = 2

    fun post(context: Context) {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU &&
            context.checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) !=
            PackageManager.PERMISSION_GRANTED
        ) return
        val open = PendingIntent.getActivity(
            context, 0,
            Intent(context, MainActivity::class.java)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )
        val notice = Notification.Builder(context, AmyApp.CHANNEL_ID)
            .setContentTitle("Amy")
            .setContentText("Tap to start listening again")
            .setSmallIcon(android.R.drawable.ic_btn_speak_now)
            .setContentIntent(open)
            .setAutoCancel(true)
            .build()
        context.getSystemService(NotificationManager::class.java)?.notify(ID, notice)
    }

    fun clear(context: Context) {
        context.getSystemService(NotificationManager::class.java)?.cancel(ID)
    }
}
