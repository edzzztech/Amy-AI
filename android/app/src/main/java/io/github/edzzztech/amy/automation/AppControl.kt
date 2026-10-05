package io.github.edzzztech.amy.automation

import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.content.pm.ResolveInfo
import android.provider.Settings
import io.github.edzzztech.amy.core.AmyState
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext

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

    @Volatile
    private var cache: List<App> = emptyList()

    /** Every launchable app, cached until [refresh] is called. */
    suspend fun apps(): List<App> {
        if (cache.isEmpty()) refresh()
        return cache
    }

    /**
     * Re-read the installed apps. Off the main thread: every label is loaded
     * from its own package, which with a couple of hundred apps is long enough
     * to drop frames.
     */
    suspend fun refresh(): Int = withContext(Dispatchers.IO) {
        val pm = context.packageManager
        val intent = Intent(Intent.ACTION_MAIN).addCategory(Intent.CATEGORY_LAUNCHER)
        val found: List<ResolveInfo> = try {
            pm.queryIntentActivities(intent, PackageManager.MATCH_ALL)
        } catch (e: Exception) {
            emptyList()
        }
        val list = found
            .mapNotNull { info ->
                val label = info.loadLabel(pm).toString().trim()
                val pkg = info.activityInfo?.packageName.orEmpty()
                if (label.isEmpty() || pkg.isEmpty()) null else App(label, pkg)
            }
            .distinctBy { it.packageName }
            .sortedBy { it.label.lowercase() }
        cache = list
        list.size
    }

    /**
     * Best match for a spoken app name, or null.
     *
     * [loose] also accepts an app name inside a longer phrase ("spotify
     * please"). That suits "open", which is nearly always an app; "start" and
     * "run" are said about plenty else, so they pass false.
     */
    suspend fun find(spoken: String, loose: Boolean = true): App? {
        val want = normalise(spoken)
        if (want.isEmpty()) return null
        return match(want, apps(), loose) ?: run {
            // Installed since the list was read? Look again before giving up.
            refresh()
            match(want, cache, loose)
        }
    }

    private fun match(want: String, all: List<App>, loose: Boolean): App? {
        val named = all.map { it to normalise(it.label) }.filter { it.second.isNotEmpty() }
        named.firstOrNull { it.second == want }?.let { return it.first }
        // Partial matches need a few letters: "open it" must not open the
        // first app with "it" somewhere in its name.
        if (want.length >= MIN_PARTIAL) {
            // The shortest label wins, being the closest to what was said.
            named.filter { it.second.startsWith(want) }
                .minByOrNull { it.second.length }?.let { return it.first }
            named.filter { it.second.contains(want) }
                .minByOrNull { it.second.length }?.let { return it.first }
        }
        if (!loose) return null
        // An app name inside the phrase: the longest wins, and very short names
        // are skipped, or the app called "X" would match any sentence with an x.
        return named
            .filter { it.second.length >= MIN_PARTIAL && want.contains(it.second) }
            .maxByOrNull { it.second.length }?.first
    }

    /** What happened when asked to open an app. */
    sealed interface Launch {
        data class Opened(val label: String) : Launch
        data object NotFound : Launch

        /**
         * Android would refuse silently: she is in the background with none of
         * the exemptions. [component] is there for the root fallback.
         */
        data class Blocked(val label: String, val component: String?) : Launch

        /** Found, allowed, and still refused. */
        data class Failed(val label: String) : Launch
    }

    /**
     * Launch by spoken name.
     *
     * Since Android 10 an app in the background cannot start activities, and
     * the system refuses *silently* — no exception, just a line in the log — so
     * the first version announced "Opening Spotify" and nothing opened. Being
     * on screen is one way through; the others are permission to draw over
     * other apps and a connected accessibility service, which the system binds
     * itself. Check before promising anything.
     */
    suspend fun launch(spoken: String, loose: Boolean = true): Launch {
        val app = find(spoken, loose) ?: return Launch.NotFound
        val intent = context.packageManager
            .getLaunchIntentForPackage(app.packageName)
            ?: return Launch.NotFound
        return when (start(intent)) {
            Start.Started -> Launch.Opened(app.label)
            Start.Blocked -> Launch.Blocked(app.label, intent.component?.flattenToShortString())
            Start.Failed -> Launch.Failed(app.label)
        }
    }

    enum class Start { Started, Blocked, Failed }

    /** Whether Android will let her start an activity right now. */
    fun mayStartActivities(): Boolean =
        AmyState.inForeground ||
            AmyAccessibilityService.instance != null ||
            Settings.canDrawOverlays(context)

    /** Start any activity — an app, a timer, an alarm — within the rules above. */
    fun start(intent: Intent): Start {
        if (!mayStartActivities()) return Start.Blocked
        return try {
            // From the accessibility service when it is on: it is the
            // exemption, so let it be the caller.
            (AmyAccessibilityService.instance ?: context)
                .startActivity(intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
            Start.Started
        } catch (e: Exception) {
            Start.Failed
        }
    }

    private fun normalise(s: String) =
        s.lowercase().filter { it.isLetterOrDigit() }

    private companion object {
        const val MIN_PARTIAL = 3
    }
}
