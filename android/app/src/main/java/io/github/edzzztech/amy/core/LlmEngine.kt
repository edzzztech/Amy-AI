package io.github.edzzztech.amy.core

import kotlinx.coroutines.flow.Flow

/**
 * What the rest of the app talks to, so the model backend can change without
 * touching anything else.
 *
 * Implemented by [MediaPipeLlm]. A phone realistically runs a 1–3B parameter
 * model; the desktop's larger models stay on the desktop, and the phone can
 * hand work over when the two are on the same network.
 */
interface LlmEngine {

    val isLoaded: Boolean

    /** Load a model file from local storage. Returns false rather than throwing. */
    suspend fun load(modelPath: String): Boolean

    /**
     * Stream a reply token by token. The caller accumulates sentences and hands
     * each completed one to [Tts], which is what lets her start talking before
     * the model has finished.
     */
    fun generate(prompt: String, system: String? = null, maxTokens: Int = 320): Flow<String>

    /** Stop generating now; used for barge-in. */
    fun cancel()

    fun unload()
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
