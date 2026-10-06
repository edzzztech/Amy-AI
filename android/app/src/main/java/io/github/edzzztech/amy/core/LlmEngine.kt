package io.github.edzzztech.amy.core

import kotlinx.coroutines.flow.Flow
import java.io.File

/**
 * One on-device model runtime. Two implement it: [LiteRtLlm] for `.litertlm`
 * models (Gemma 3n, which can see) and [MediaPipeLlm] for `.task` models.
 * [LocalModel] picks between them by the model file on the phone, so nothing
 * else needs to know which is running.
 *
 * A phone realistically runs a 1–4B parameter model; the desktop's larger
 * models stay on the desktop, and the phone can hand work over when the two
 * are on the same network.
 */
interface LlmEngine {

    /** The model files this runtime loads, by extension. */
    val fileExtension: String

    val isLoaded: Boolean

    /** Whether the loaded model takes a picture as well as text. */
    val supportsVision: Boolean

    /** The largest model file of this runtime's kind in the model folder, if any. */
    fun findModel(): File?

    /** Load [findModel]'s file if nothing is loaded. False rather than throwing. */
    suspend fun ensureLoaded(): Boolean

    /** Characters of prompt that fit beside [system] in the loaded model's window. */
    fun promptRoom(system: String?): Int

    /**
     * Stream a reply, piece by piece. The caller accumulates sentences and
     * hands each completed one to [Tts], which is what lets her start talking
     * before the model has finished.
     */
    fun generate(prompt: String, system: String? = null): Flow<String>

    /** Stream an answer about a picture (encoded PNG or JPEG bytes). */
    fun describe(image: ByteArray, question: String, system: String? = null): Flow<String>

    /** Stop generating now; used for barge-in. */
    fun cancel()

    /** Free the model, once nothing is running on it. */
    suspend fun unloadWhenIdle()
}

/**
 * Splits a token stream into speakable chunks. Ported from the desktop, where
 * the first flush is deliberately looser than the rest: the opening clause is
 * spoken as soon as it stands alone so audio starts sooner, and after that it
 * waits for whole sentences.
 */
class SentenceBuffer {
    private val buffer = StringBuilder()
    private var spokenAnything = false

    fun push(token: String): List<String> {
        buffer.append(token)
        val out = mutableListOf<String>()
        while (true) {
            val end = if (!spokenAnything) firstClause() else firstSentence()
            if (end <= 0) break
            val chunk = buffer.substring(0, end).trim()
            buffer.delete(0, end)
            if (chunk.isNotEmpty()) {
                out += chunk
                spokenAnything = true
            }
        }
        return out
    }

    /** Anything still buffered when generation ends. */
    fun flush(): String = buffer.toString().trim().also { buffer.setLength(0) }

    fun reset() {
        buffer.setLength(0)
        spokenAnything = false
    }

    private fun firstSentence(): Int {
        for (m in SENTENCE_END.findAll(buffer)) {
            if (!isAbbreviation(m.range.first)) return m.range.last + 1
        }
        return -1
    }

    private fun firstClause(): Int {
        // Skip boundaries too short to be worth speaking, and keep looking.
        // Returning -1 on the first short one stalls forever: the same match
        // is found again on every token, so nothing is ever flushed.
        for (m in CLAUSE_END.findAll(buffer)) {
            val end = m.range.last + 1
            if (end > MIN_FIRST_CHUNK && !isAbbreviation(m.range.first)) return end
        }
        return -1
    }

    /**
     * A full stop that does not end a sentence: "Dr. Patel", "J. K. Rowling",
     * "e.g. this". Treating one as an ending makes her say "Doctor." and then,
     * after a pause, "Patel".
     */
    private fun isAbbreviation(at: Int): Boolean {
        if (buffer[at] != '.') return false
        var start = at
        while (start > 0 && (buffer[start - 1].isLetter() || buffer[start - 1] == '.')) start--
        val word = buffer.substring(start, at).lowercase()
        return word in ABBREVIATIONS ||
            (word.length == 1 && word[0].isLetter()) ||
            '.' in word
    }

    private companion object {
        const val MIN_FIRST_CHUNK = 11

        // Punctuation counts only once whitespace follows it. Text arrives in
        // fragments, so a full stop at the end of what has arrived so far may
        // be the "." of "4.99" or "Dr." with the rest still to come; taking it
        // as an ending split numbers and names in two.
        //
        // A plain string with every backslash doubled: in a raw string
        // ("""...""") the $ that joins these patterns is awkward to write, and
        // a single backslash in a plain one is a Kotlin escape, not a regex one.
        private const val CLOSERS = "[\"')\\]]*"
        private const val WS = "[ \\t\\n\\r]"
        val SENTENCE_END = Regex("[.!?]$CLOSERS$WS")
        val CLAUSE_END = Regex("(?:[,;:]|[.!?]$CLOSERS)$WS")

        val ABBREVIATIONS = setOf(
            "mr", "mrs", "ms", "dr", "prof", "sr", "jr", "st", "vs", "etc",
            "approx", "fig", "inc", "ltd", "co", "mt", "ft",
        )
    }
}

/**
 * Strips a speaker label from the start of a reply. The conversation so far
 * reaches the model as "User: ... / Amy: ..." lines, and models continue the
 * pattern: tried with Gemma 3n, a follow-up came back as "Amy: There are 3
 * days until Friday", and she read her own name out. Text is held back only
 * while it could still turn out to be a label.
 */
class SpeakerTag {
    private val held = StringBuilder()
    private var decided = false

    fun push(text: String): String {
        if (decided) return text
        held.append(text)
        val start = held.trimStart().toString()
        TAG.find(start)?.let {
            decided = true
            held.setLength(0)
            return start.substring(it.range.last + 1)
        }
        val squeezed = start.lowercase().replace(" ", "")
        if (start.length < LONGEST && LABELS.any { it.startsWith(squeezed) }) return ""
        decided = true
        return held.toString().also { held.setLength(0) }
    }

    /** Whatever is still held when the reply ends. */
    fun flush(): String {
        decided = true
        return held.toString().also { held.setLength(0) }
    }

    private companion object {
        val LABELS = listOf("amy:", "assistant:", "model:")
        const val LONGEST = 14
        val TAG = Regex("^(?:amy|assistant|model)\\s*:\\s*", RegexOption.IGNORE_CASE)
    }
}
