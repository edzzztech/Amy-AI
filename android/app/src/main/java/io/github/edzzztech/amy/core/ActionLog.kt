package io.github.edzzztech.amy.core

import android.content.Context
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json
import java.io.File
import java.time.LocalDateTime
import java.time.format.DateTimeFormatter
import java.time.temporal.ChronoUnit

/**
 * The same append-only record the desktop keeps, in the same JSONL format, so
 * the two can be merged when the devices sync. One object per line.
 *
 * Written from several threads — the service, the brain, the desktop link — so
 * timestamps use java.time, which is immutable and thread-safe. The previous
 * SimpleDateFormat is neither, and two threads sharing one produce wrong dates
 * without any error.
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
    private var writesSinceTrim = 0

    @Synchronized
    fun record(kind: String, summary: String, outcome: String = "done", detail: String? = null) {
        val entry = Entry(
            at = LocalDateTime.now().truncatedTo(ChronoUnit.SECONDS).format(STAMP),
            kind = kind,
            summary = summary.take(400),
            outcome = outcome,
            detail = detail?.take(2000),
        )
        try {
            file.appendText(json.encodeToString(Entry.serializer(), entry) + "\n")
            if (++writesSinceTrim >= TRIM_EVERY) {
                writesSinceTrim = 0
                trimIfLarge()
            }
        } catch (e: Exception) {
            // Logging must never break the action it is recording.
        }
    }

    @Synchronized
    fun since(hours: Int = 24): List<Entry> {
        if (!file.exists()) return emptyList()
        val cutoff = LocalDateTime.now().minusHours(hours.toLong())
        return file.readLines().mapNotNull { line ->
            runCatching { json.decodeFromString(Entry.serializer(), line) }.getOrNull()
        }.filter { entry ->
            runCatching { !LocalDateTime.parse(entry.at, STAMP).isBefore(cutoff) }
                .getOrDefault(false)
        }
    }

    /**
     * An always-on assistant logs all day, every day. Unbounded, the file grows
     * forever and every read of it gets slower; past the limit, keep the newest
     * half.
     */
    private fun trimIfLarge() {
        if (!file.exists() || file.length() < MAX_BYTES) return
        val lines = file.readLines()
        val keep = lines.takeLast(lines.size / 2)
        val tmp = File(file.parentFile, file.name + ".tmp")
        tmp.writeText(keep.joinToString("\n", postfix = "\n"))
        if (!tmp.renameTo(file)) {
            file.writeText(tmp.readText())
            tmp.delete()
        }
    }

    private companion object {
        val STAMP: DateTimeFormatter = DateTimeFormatter.ISO_LOCAL_DATE_TIME
        const val MAX_BYTES = 2L * 1024 * 1024
        const val TRIM_EVERY = 200
    }
}
