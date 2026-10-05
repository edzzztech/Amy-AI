package io.github.edzzztech.amy.core

import kotlinx.coroutines.flow.Flow

/**
 * What the rest of the app talks to, so the model backend can change without
 * touching anything else.
 *
 * The first implementation will wrap llama.cpp through JNI. A phone realistically
 * runs a 1–3B parameter model; the desktop's larger models stay on the desktop,
 * and the phone can hand work over when the two are on the same network.
 */
interface LlmEngine {

    val isLoaded: Boolean

    /** Load a GGUF from local storage. Returns false rather than throwing. */
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
        val m = Regex("[.!?](\s|$)").find(buffer)
        return m?.range?.last?.plus(1) ?: -1
    }

    private fun firstClause(): Int {
        // Skip boundaries too short to be worth speaking, and keep looking.
        // Returning -1 on the first short one stalls forever: the same match
        // is found again on every token, so nothing is ever flushed.
        for (m in Regex("[,;:.!?](\s|$)").findAll(buffer)) {
            val end = m.range.last + 1
            if (end > MIN_FIRST_CHUNK) return end
        }
        return -1
    }

    private companion object {
        const val MIN_FIRST_CHUNK = 11
    }
}
