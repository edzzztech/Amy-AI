package io.github.edzzztech.amy.automation

import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.content.pm.ResolveInfo

/**
 * Finding and launching the apps on the phone — the Android counterpart to the
 * desktop's AppIndex.
 *
 * Matching is deliberately forgiving: speech recognition will hand us
 * "you tube" for YouTube and "what's app" for WhatsApp, so the comparison
 * strips everything that is not a letter or digit before looking.
 */
class AppControl(private val context: Context) {

    data class App(val label: String, val packageName: String)

    private var cache: List<App> = emptyList()

    /** Every launchable app, cached until [refresh] is called. */
    fun apps(): List<App> {
        if (cache.isEmpty()) refresh()
        return cache
    }

    fun refresh(): Int {
        val pm = context.packageManager
        val intent = Intent(Intent.ACTION_MAIN).addCategory(Intent.CATEGORY_LAUNCHER)
        val found: List<ResolveInfo> = try {
            pm.queryIntentActivities(intent, PackageManager.MATCH_ALL)
        } catch (e: Exception) {
            emptyList()
        }
        cache = found
            .mapNotNull { info ->
                val label = info.loadLabel(pm)?.toString()?.trim().orEmpty()
                val pkg = info.activityInfo?.packageName.orEmpty()
                if (label.isEmpty() || pkg.isEmpty()) null else App(label, pkg)
            }
            .distinctBy { it.packageName }
            .sortedBy { it.label.lowercase() }
        return cache.size
    }

    /** Best match for a spoken app name, or null. */
    fun find(spoken: String): App? {
        val want = normalise(spoken)
        if (want.isEmpty()) return null
        val all = apps()
        // Exact, then starts-with, then contains — in that order, so "play"
        // prefers "Play Store" over "Google Play Games".
        return all.firstOrNull { normalise(it.label) == want }
            ?: all.firstOrNull { normalise(it.label).startsWith(want) }
            ?: all.firstOrNull { normalise(it.label).contains(want) }
            ?: all.firstOrNull { want.contains(normalise(it.label)) }
    }

    /** Launch by spoken name. Returns the label launched, or null if not found. */
    fun launch(spoken: String): String? {
        val app = find(spoken) ?: return null
        val intent = context.packageManager
            .getLaunchIntentForPackage(app.packageName)
            ?.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            ?: return null
        return try {
            context.startActivity(intent)
            app.label
        } catch (e: Exception) {
            null
        }
    }

    private fun normalise(s: String) =
        s.lowercase().filter { it.isLetterOrDigit() }
}
