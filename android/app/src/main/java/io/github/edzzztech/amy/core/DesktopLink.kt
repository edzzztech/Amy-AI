package io.github.edzzztech.amy.core

import android.content.Context
import androidx.core.content.edit
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL

/**
 * Talks to Amy on the desktop, so the phone can drive the PC.
 *
 * Pairing is an address and a code the desktop shows you — no account, no
 * server in between, nothing leaving your network. The desktop refuses
 * anything that is not from a private address and logs every command it
 * accepts, so this cannot quietly do things you have no record of.
 *
 * If the PC is off or asleep, calls fail and the phone answers for itself.
 * That is the whole point of the phone build being standalone.
 */
class DesktopLink(context: Context) {

    private val prefs =
        context.getSharedPreferences("amy_desktop_link", Context.MODE_PRIVATE)

    /** e.g. "192.168.1.24:8765". Stored tidied: see [normalise]. */
    var address: String
        get() = prefs.getString(KEY_ADDRESS, "").orEmpty()
        set(value) = prefs.edit { putString(KEY_ADDRESS, normalise(value)) }

    var code: String
        get() = prefs.getString(KEY_CODE, "").orEmpty()
        set(value) = prefs.edit { putString(KEY_CODE, value.trim()) }

    val isPaired: Boolean
        get() = address.isNotBlank() && code.isNotBlank()

    fun forget() {
        prefs.edit { clear() }
    }

    data class Status(val reachable: Boolean, val detail: String)

    /** Is the desktop awake and does it accept our code? */
    suspend fun status(): Status = withContext(Dispatchers.IO) {
        if (!isPaired) return@withContext Status(false, "Not paired with a computer yet.")
        var conn: HttpURLConnection? = null
        try {
            conn = open("/status", "GET")
            when (val code = conn.responseCode) {
                200 -> Status(true, "Connected")
                401 -> Status(false, "The pairing code was refused.")
                403 -> Status(false, "That computer only accepts local network connections.")
                else -> Status(false, "The computer answered with $code.")
            }
        } catch (e: Exception) {
            Status(false, "No answer. The computer may be off, or on another network.")
        } finally {
            conn?.disconnect()
        }
    }

    /** Send a command. Returns null on success, or something to say on failure. */
    suspend fun send(text: String): String? = withContext(Dispatchers.IO) {
        if (!isPaired) return@withContext "I'm not paired with a computer yet."
        var conn: HttpURLConnection? = null
        try {
            conn = open("/command", "POST")
            conn.doOutput = true
            conn.outputStream.use { out ->
                out.write(JSONObject().put("text", text).toString().toByteArray())
            }
            when (val code = conn.responseCode) {
                200 -> null
                401 -> "The computer refused the pairing code."
                403 -> "That computer only accepts connections from its own network."
                else -> "The computer answered with $code."
            }
        } catch (e: Exception) {
            "I couldn't reach your computer. It may be off, asleep, or on another network."
        } finally {
            conn?.disconnect()
        }
    }

    private fun open(path: String, method: String): HttpURLConnection {
        val url = URL("http://$address$path")
        return (url.openConnection() as HttpURLConnection).apply {
            requestMethod = method
            setRequestProperty("X-Amy-Token", code)
            setRequestProperty("Content-Type", "application/json")
            connectTimeout = TIMEOUT_MS
            readTimeout = TIMEOUT_MS
        }
    }

    companion object {
        private const val KEY_ADDRESS = "address"
        private const val KEY_CODE = "code"
        private const val DEFAULT_PORT = 8765

        // Short: a desktop on the same network answers immediately, and a long
        // wait before "I couldn't reach your computer" just feels broken.
        private const val TIMEOUT_MS = 4000

        /**
         * What people type, made into host:port. "http://192.168.1.24:8765/"
         * used to become "http://http://192.168.1.24:8765//status", and an
         * address without a port went to port 80, where nothing answers.
         */
        internal fun normalise(typed: String): String {
            val bare = typed.trim()
                .replace(Regex("^[a-zA-Z]+://"), "")
                .substringBefore('/')
                .trim()
            if (bare.isEmpty()) return ""
            return when {
                bare.endsWith(":") -> bare + DEFAULT_PORT
                // IPv6 in brackets carries its port after the bracket.
                bare.startsWith("[") ->
                    if (bare.substringAfter("]", "").startsWith(":")) bare
                    else "$bare:$DEFAULT_PORT"
                // Bare IPv6: its colons are not a port, and it needs brackets.
                bare.count { it == ':' } > 1 -> "[$bare]:$DEFAULT_PORT"
                ':' in bare -> bare
                else -> "$bare:$DEFAULT_PORT"
            }
        }
    }
}
