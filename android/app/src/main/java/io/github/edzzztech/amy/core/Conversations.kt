package io.github.edzzztech.amy.core

import android.content.Context
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json
import java.io.File
import java.text.SimpleDateFormat
import java.util.Date
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
 * `conversations/`, which keeps loading one cheap and means a corrupt file
 * costs you a single conversation rather than the lot.
 *
 * Deliberately plain files, like the desktop's memory: you can read them,
 * copy them off, or delete one by hand.
 */
class Conversations(context: Context) {

    private val dir = File(context.filesDir, "conversations").apply { mkdirs() }
    private val json = Json { prettyPrint = true; ignoreUnknownKeys = true }
    private val stamp = SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ss", Locale.UK)
    private val human = SimpleDateFormat("d MMM, HH:mm", Locale.UK)

    private var current: Conversation? = null

    /** Start a fresh conversation. The title is filled in by the first thing you say. */
    fun start(): Conversation {
        val now = Date()
        val conversation = Conversation(
            id = "c${now.time}",
            started = stamp.format(now),
            title = "New conversation",
        )
        current = conversation
        return conversation
    }

    fun currentOrStart(): Conversation = current ?: start()

    /** Add a turn and write the whole conversation back. */
    fun append(role: String, text: String) {
        val clean = text.trim()
        if (clean.isEmpty()) return
        val base = currentOrStart()
        val title = if (base.turns.isEmpty() && role == "you") clean.take(48) else base.title
        val updated = base.copy(
            title = title,
            turns = base.turns + Turn(role, clean, stamp.format(Date())),
        )
        current = updated
        save(updated)
    }

    fun turns(): List<Turn> = current?.turns.orEmpty()

    private fun save(conversation: Conversation) {
        try {
            File(dir, "${conversation.id}.json")
                .writeText(json.encodeToString(Conversation.serializer(), conversation))
        } catch (e: Exception) {
            // Losing one write is better than crashing mid-conversation.
        }
    }

    /** Every stored conversation, newest first. */
    fun list(): List<Conversation> =
        dir.listFiles { f -> f.extension == "json" }
            ?.mapNotNull { file ->
                runCatching {
                    json.decodeFromString(Conversation.serializer(), file.readText())
                }.getOrNull()
            }
            ?.sortedByDescending { it.started }
            .orEmpty()

    fun load(id: String): Conversation? =
        runCatching {
            json.decodeFromString(
                Conversation.serializer(),
                File(dir, "$id.json").readText(),
            )
        }.getOrNull()?.also { current = it }

    fun delete(id: String) {
        runCatching { File(dir, "$id.json").delete() }
        if (current?.id == id) current = null
    }

    /** "3 Jun, 14:08" for the drawer. */
    fun whenShown(conversation: Conversation): String =
        runCatching { human.format(stamp.parse(conversation.started)!!) }
            .getOrDefault(conversation.started)
}
