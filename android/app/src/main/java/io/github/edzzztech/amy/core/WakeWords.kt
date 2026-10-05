package io.github.edzzztech.amy.core

/**
 * Deciding whether something was said *to* her.
 *
 * Speech recognisers rarely spell a name the way you would. "Amy" comes back
 * as "Aimee", "Amie", "Emmy" or "Ami" depending on accent and device, and a
 * plain substring test both misses those and fires on words that merely
 * contain the letters — "steamy", "Kamiya". So: whole words only, a set of
 * known spellings, and the name has to come near the start of what was said.
 *
 * That last rule matters for a microphone that is always open. "I told Amy
 * about it yesterday" is about her, not to her.
 */
object WakeWords {

    /** Spellings a recogniser may return for "Amy". Lower case. */
    val SPELLINGS = listOf("amy", "aimee", "aimie", "amie", "ami", "emmy", "amey", "aimy")

    /** Polite openers that may come before her name. */
    private val OPENERS = setOf("hey", "hi", "ok", "okay", "oi", "yo", "um", "uh", "so")

    /** How far into an utterance her name may appear and still count. */
    private const val MAX_POSITION = 2

    private val WORD = Regex("[a-z']+")

    private fun words(text: String): List<String> = WORD.findAll(text.lowercase()).map { it.value }.toList()

    /** Her name appears anywhere, as a whole word. Used to pick between hypotheses. */
    fun containsWake(text: String): Boolean = words(text).any { it in SPELLINGS }

    /** Position of her name among the words, or -1 if it does not open the utterance. */
    fun wakePosition(text: String): Int {
        val w = words(text)
        var i = 0
        // skip openers: "hey amy", "ok so amy"
        while (i < w.size && w[i] in OPENERS) i++
        val limit = minOf(w.size, i + MAX_POSITION)
        for (j in i until limit) if (w[j] in SPELLINGS) return j
        return -1
    }

    /** Was she addressed? Her name must open the utterance, after any opener. */
    fun isAddressed(text: String): Boolean = wakePosition(text) >= 0

    /**
     * What was asked, with her name and anything before it removed.
     * "Hey Amy, what's the time?" -> "what's the time?"
     */
    fun stripWake(text: String): String {
        val lower = text.lowercase()
        // Find the first occurrence of any spelling as a whole word.
        var cut = -1
        var length = 0
        for (s in SPELLINGS) {
            val m = Regex("\\b" + Regex.escape(s) + "\\b").find(lower) ?: continue
            if (cut < 0 || m.range.first < cut) {
                cut = m.range.first
                length = s.length
            }
        }
        if (cut < 0) return text.trim()
        return text.substring(cut + length)
            .trimStart(' ', ',', '.', '?', '!', ':', ';', '-')
            .trim()
    }
}
