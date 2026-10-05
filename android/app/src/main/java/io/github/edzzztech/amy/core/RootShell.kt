package io.github.edzzztech.amy.core

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import java.io.BufferedReader
import java.io.InputStreamReader
import java.io.OutputStreamWriter

/**
 * Root is optional. Everything Amy does has an unrooted path through
 * [io.github.edzzztech.amy.automation.AmyAccessibilityService]; root is strictly
 * additive, so nothing here may be required for the app to function.
 *
 * Mirrors the desktop's stance on shell access: off unless deliberately enabled.
 */
object RootShell {

    @Volatile
    private var cachedAvailability: Boolean? = null

    /** True if `su` exists and grants us a shell. Cached: the prompt is intrusive. */
    suspend fun isAvailable(): Boolean = withContext(Dispatchers.IO) {
        cachedAvailability ?: run {
            val ok = try {
                run("id").let { it.exitCode == 0 && it.stdout.contains("uid=0") }
            } catch (e: Exception) {
                false
            }
            cachedAvailability = ok
            ok
        }
    }

    data class Result(val exitCode: Int, val stdout: String, val stderr: String)

    /**
     * Run one command as root. Returns rather than throws, because a denied
     * root prompt is an ordinary outcome, not an error.
     */
    suspend fun run(command: String, timeoutMs: Long = 15_000): Result =
        withContext(Dispatchers.IO) {
            var process: Process? = null
            try {
                process = ProcessBuilder("su").redirectErrorStream(false).start()
                OutputStreamWriter(process.outputStream).use { w ->
                    w.write(command)
                    w.write("\nexit\n")
                    w.flush()
                }
                val out = StringBuilder()
                val err = StringBuilder()
                BufferedReader(InputStreamReader(process.inputStream)).use { r ->
                    r.forEachLine { out.appendLine(it) }
                }
                BufferedReader(InputStreamReader(process.errorStream)).use { r ->
                    r.forEachLine { err.appendLine(it) }
                }
                val finished = process.waitFor(timeoutMs, java.util.concurrent.TimeUnit.MILLISECONDS)
                if (!finished) {
                    process.destroyForcibly()
                    Result(-1, out.toString().trim(), "timed out after ${timeoutMs}ms")
                } else {
                    Result(process.exitValue(), out.toString().trim(), err.toString().trim())
                }
            } catch (e: Exception) {
                Result(-1, "", e.message ?: "su unavailable")
            } finally {
                try { process?.destroy() } catch (_: Exception) {}
            }
        }

    /**
     * Start an activity as root, which Android's limits on starting activities
     * from the background do not apply to. [component] is a flattened name
     * such as "com.spotify.music/.MainActivity"; anything else is refused
     * rather than passed to a root shell.
     */
    suspend fun startActivity(component: String): Boolean {
        if (!COMPONENT.matches(component) || !isAvailable()) return false
        val result = run(
            "am start -n '$component' -a android.intent.action.MAIN " +
                "-c android.intent.category.LAUNCHER"
        )
        // Older versions of am report failure in its output with exit code 0.
        return result.exitCode == 0 &&
            !result.stdout.contains("Error") && !result.stderr.contains("Error")
    }

    private val COMPONENT = Regex("[A-Za-z0-9_.]+/[A-Za-z0-9_.$]+")

    /** Inject a tap without the accessibility route. Root only. */
    suspend fun tap(x: Int, y: Int): Boolean = run("input tap $x $y").exitCode == 0

    /** Type text system-wide. Root only. */
    suspend fun type(text: String): Boolean {
        // Inside single quotes every character is literal, including the
        // backslash, so the quote is the only thing needing escaping: close
        // the quote, emit an escaped one, reopen.  ' -> '\''
        // Doubling backslashes here would corrupt them, not protect them.
        val escaped = text.replace("'", "'\\''")
        return run("input text '$escaped'").exitCode == 0
    }
}
