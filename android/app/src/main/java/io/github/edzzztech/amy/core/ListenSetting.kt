package io.github.edzzztech.amy.core

import android.content.Context
import androidx.core.content.edit

/**
 * Whether she should be listening, remembered across restarts.
 *
 * "Always listening unless I tell it not to" has to survive the phone turning
 * off and the service being recreated: put to sleep, she stays asleep until
 * woken, and otherwise she comes back listening.
 */
object ListenSetting {

    private const val FILE = "amy_settings"
    private const val KEY = "listening"

    fun save(context: Context, listening: Boolean) {
        context.getSharedPreferences(FILE, Context.MODE_PRIVATE)
            .edit { putBoolean(KEY, listening) }
    }

    /** True until she has been told to sleep. */
    fun wanted(context: Context): Boolean =
        context.getSharedPreferences(FILE, Context.MODE_PRIVATE).getBoolean(KEY, true)
}
