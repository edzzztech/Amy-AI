package io.github.edzzztech.amy.core

import android.content.Context
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

    /** e.g. "192.168.1.24:8765" */
    var address: String
        get() = prefs.getString(KEY_ADDRESS, "").orEmpty()
        set(value) = prefs.edit().putString(KEY_ADDRESS, value.trim()).apply()

    var code: String
        get() = prefs.getString(KEY_CODE, "").orEmpty()
        set(value) = prefs.edit().putString(KEY_CODE, value.trim()).apply()

    val isPaired: Boolean
        get() = address.isNotBlank() && code.isNotBlank()

    fun forget() {
        prefs.edit().clear().apply()
    }

    data class Status(val reachable: Boolean, val detail: String)

    /** Is the desktop awake and does it accept our code? */
    suspend fun status(): Status = withContext(Dispatchers.IO) {
        if (!isPaired) return@withContext Status(false, "Not paired with a computer yet.")
        try {
            val conn = open("/status", "GET")
            when (val code = conn.responseCode) {
                200 -> Status(true, "Connected")
                401 -> Status(false, "The pairing code was refused.")
                403 -> Status(false, "That computer only accepts local network connections.")
                else -> Status(false, "The computer answered with $code.")
            }
        } catch (e: Exception) {
            Status(false, "No answer. The computer may be off or on another network.")
        }
    }

    /** Send a command. Returns null on success, or something to say on failure. */
    suspend fun send(text: String): String? = withContext(Dispatchers.IO) {
        if (!isPaired) return@withContext "I'm not paired with a computer yet."
        try {
            val conn = open("/command", "POST")
            conn.doOutput = true
            conn.outputStream.use { out ->
                out.write(JSONObject().put("text", text).toString().toByteArray())
            }
            when (conn.responseCode) {
                200 -> null
                401 -> "The computer refused the pairing code."
                403 -> "That computer only accepts connections from its own network."
                else -> "The computer answered with ${conn.responseCode}."
            }
        } catch (e: Exception) {
            "I couldn't reach your computer. It may be off, asleep, or on another network."
        }
    }

    private fun open(path: String, method: String): HttpURLConnection {
        val url = URL("http://" + address.removeSuffix("/") + path)
        return (url.openConnection() as HttpURLConnection).apply {
            requestMethod = method
            setRequestProperty("X-Amy-Token", code)
            setRequestProperty("Content-Type", "application/json")
            connectTimeout = TIMEOUT_MS
            readTimeout = TIMEOUT_MS
        }
    }

    private companion object {
        const val KEY_ADDRESS = "address"
        const val KEY_CODE = "code"
        // Short: a desktop on the same network answers immediately, and a long
        // wait before "I couldn't reach your computer" just feels broken.
        const val TIMEOUT_MS = 4000
    }
}
