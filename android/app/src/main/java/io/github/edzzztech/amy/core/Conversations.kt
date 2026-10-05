package io.github.edzzztech.amy.core

import android.content.Context
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json
import java.io.File
import java.time.LocalDateTime
import java.time.format.DateTimeFormatter
import java.time.temporal.ChronoUnit
import java.util.Locale

@Serializable
data class Turn(
    val role: String,          // "you" or "amy"
    val text: String,
    val at: String,
)

@Serializable
data class Conversation(
    val id: String,
    val started: String,
    val title: String,
    val turns: List<Turn> = emptyList(),
)

/**
 * Conversation history on disk. One JSON file per conversation under
 * `conversations/`, so a corrupt file costs one conversation, not the lot.
 * Deliberately plain files, like the desktop's memory: readable, copyable,
 * deletable by hand.
 *
 * Three things here exist because the first version got them wrong:
 *
 *  - **Writes are atomic.** Writing straight over the file meant a process
 *    killed mid-write — routine on Android — left it truncated and the
 *    conversation unreadable. Each save goes to a temp file and is renamed
 *    into place, which either happens completely or not at all.
 *  - **The list is held in memory.** Listing re-read and re-parsed every
 *    conversation on disk after every message, so she got slower the longer
 *    you used her. Disk is now read once; saves update the index.
 *  - **Timestamps use java.time**, which is thread-safe; SimpleDateFormat is
 *    not and corrupts dates silently when shared.
 */
class Conversations(context: Context) {

    private val dir = File(context.filesDir, "conversations").apply { mkdirs() }
    private val json = Json { prettyPrint = true; ignoreUnknownKeys = true }

    private var current: Conversation? = null
    private var index: MutableMap<String, Conversation>? = null

    private fun now(): String =
        LocalDateTime.now().truncatedTo(ChronoUnit.SECONDS).format(STAMP)

    /** Start a fresh conversation. The title is filled in by the first thing you say. */
    @Synchronized
    fun start(): Conversation {
        var id = "c" + System.currentTimeMillis()
        // Two starts in the same millisecond would otherwise share a file.
        while (File(dir, "$id.json").exists() || index().containsKey(id)) id += "x"
        return Conversation(id = id, started = now(), title = "New conversation")
            .also { current = it }
    }

    @Synchronized
    fun currentOrStart(): Conversation = current ?: start()

    /** Add a turn and write the whole conversation back. */
    @Synchronized
    fun append(role: String, text: String) {
        val clean = text.trim()
        if (clean.isEmpty()) return
        val base = currentOrStart()
        val title = if (base.turns.isEmpty() && role == "you") clean.take(48) else base.title
        val updated = base.copy(title = title, turns = base.turns + Turn(role, clean, now()))
        current = updated
        save(updated)
    }

    @Synchronized
    fun turns(): List<Turn> = current?.turns.orEmpty()

    /** Every stored conversation, newest first. Served from memory after the first call. */
    @Synchronized
    fun list(): List<Conversation> = index().values.sortedByDescending { it.started }

    @Synchronized
    fun load(id: String): Conversation? =
        (index()[id] ?: read(File(dir, "$id.json")))?.also { current = it }

    @Synchronized
    fun delete(id: String) {
        runCatching { File(dir, "$id.json").delete() }
        index().remove(id)
        if (current?.id == id) current = null
    }

    /** "3 Jun, 14:08" for the drawer. */
    fun whenShown(conversation: Conversation): String =
        runCatching { LocalDateTime.parse(conversation.started, STAMP).format(HUMAN) }
            .getOrDefault(conversation.started)

    private fun index(): MutableMap<String, Conversation> =
        index ?: dir.listFiles { f -> f.extension == "json" }
            ?.mapNotNull { read(it) }
            ?.associateByTo(mutableMapOf()) { it.id }
            .orEmpty()
            .toMutableMap()
            .also { index = it }

    private fun read(file: File): Conversation? =
        runCatching { json.decodeFromString(Conversation.serializer(), file.readText()) }.getOrNull()

    private fun save(conversation: Conversation) {
        try {
            val target = File(dir, "${conversation.id}.json")
            val tmp = File(dir, "${conversation.id}.json.tmp")
            tmp.writeText(json.encodeToString(Conversation.serializer(), conversation))
            if (!tmp.renameTo(target)) {
                // Some filesystems refuse to rename over an existing file.
                target.delete()
                if (!tmp.renameTo(target)) {
                    target.writeText(tmp.readText())
                    tmp.delete()
                }
            }
            index()[conversation.id] = conversation
        } catch (e: Exception) {
            // Losing one write is better than crashing mid-conversation.
        }
    }

    private companion object {
        val STAMP: DateTimeFormatter = DateTimeFormatter.ISO_LOCAL_DATE_TIME
        val HUMAN: DateTimeFormatter = DateTimeFormatter.ofPattern("d MMM, HH:mm", Locale.UK)
    }
}
