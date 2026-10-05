package io.github.edzzztech.amy.core

import android.content.Context
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json
import java.io.File
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/**
 * The same append-only record the desktop keeps, in the same JSONL format, so
 * the two can be merged when the devices sync. One object per line, flushed as
 * it is written.
 */
class ActionLog(context: Context) {

    @Serializable
    data class Entry(
        val at: String,
        val kind: String,
        val summary: String,
        val outcome: String = "done",
        val detail: String? = null,
        val device: String = "phone",
    )

    private val file = File(context.filesDir, "actions.jsonl")
    private val json = Json { encodeDefaults = true; ignoreUnknownKeys = true }
    private val stamp = SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ss", Locale.UK)

    @Synchronized
    fun record(kind: String, summary: String, outcome: String = "done", detail: String? = null) {
        val entry = Entry(
            at = stamp.format(Date()),
            kind = kind,
            summary = summary.take(400),
            outcome = outcome,
            detail = detail?.take(2000),
        )
        try {
            file.appendText(json.encodeToString(Entry.serializer(), entry) + "\n")
        } catch (e: Exception) {
            // Logging must never break the action it is recording.
        }
    }

    fun since(hours: Int = 24): List<Entry> {
        if (!file.exists()) return emptyList()
        val cutoff = System.currentTimeMillis() - hours * 3_600_000L
        return file.readLines().mapNotNull { line ->
            runCatching { json.decodeFromString(Entry.serializer(), line) }.getOrNull()
        }.filter { entry ->
            runCatching { stamp.parse(entry.at)?.time ?: 0L }.getOrDefault(0L) >= cutoff
        }
    }
}
