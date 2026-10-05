package io.github.edzzztech.amy.core

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The buffer decides when she starts speaking during a streamed reply. Its
 * first version stalled: a too-short first clause was rejected and then found
 * again on every token, so nothing was ever spoken until the very end.
 */
class SentenceBufferTest {

    private val reply =
        "Of course, Sir. The bed temperature for PETG is 80 degrees on glass. " +
            "Your last print warped at the corners because it ran at 75! Shall I adjust it?"

    /** Feed the reply in fixed-size tokens, the way a model streams it. */
    private fun stream(text: String, size: Int = 7): Pair<List<String>, Int> {
        val buffer = SentenceBuffer()
        val spoken = mutableListOf<String>()
        var firstChunkAt = -1
        var consumed = 0
        text.chunked(size).forEach { token ->
            consumed += token.length
            val out = buffer.push(token)
            if (out.isNotEmpty() && firstChunkAt < 0) firstChunkAt = consumed
            spoken += out
        }
        buffer.flush().takeIf { it.isNotEmpty() }?.let { spoken += it }
        return spoken to firstChunkAt
    }

    @Test
    fun `splits a reply into natural spoken chunks`() {
        val (spoken, _) = stream(reply)
        assertEquals(
            listOf(
                "Of course, Sir.",
                "The bed temperature for PETG is 80 degrees on glass.",
                "Your last print warped at the corners because it ran at 75!",
                "Shall I adjust it?",
            ),
            spoken,
        )
    }

    @Test
    fun `starts speaking early rather than at the end`() {
        val (_, firstAt) = stream(reply)
        assertTrue("first chunk at $firstAt of ${reply.length}", firstAt in 1..(reply.length / 4))
    }

    @Test
    fun `drops nothing`() {
        listOf(1, 3, 7, 50, 500).forEach { size ->
            val (spoken, _) = stream(reply, size)
            assertEquals("token size $size", reply.replace(" ", ""), spoken.joinToString("").replace(" ", ""))
        }
    }

    @Test
    fun `short replies come out through flush`() {
        listOf("Yes.", "Hi, Sir.", "No punctuation at all here").forEach { text ->
            val (spoken, _) = stream(text)
            assertEquals(listOf(text), spoken)
        }
    }

    @Test
    fun `empty input produces nothing`() {
        val (spoken, _) = stream("")
        assertTrue(spoken.isEmpty())
    }

    /**
     * Every token size, because where the stream happens to break decides
     * whether a full stop arrives with or without what follows it. The
     * original version of the test below passed only because its seven-
     * character tokens never ended on the "." of "4.99".
     */
    private fun assertAtEveryTokenSize(text: String, expected: List<String>) {
        (1..12).forEach { size ->
            assertEquals("token size $size", expected, stream(text, size).first)
        }
    }

    @Test
    fun `decimals do not split a sentence`() {
        assertAtEveryTokenSize(
            "It costs 4.99 today. Shall I order it?",
            listOf("It costs 4.99 today.", "Shall I order it?"),
        )
    }

    @Test
    fun `titles and initials do not end a sentence`() {
        assertAtEveryTokenSize(
            "Your appointment with Dr. Patel is at noon. J. K. Rowling wrote it.",
            listOf("Your appointment with Dr. Patel is at noon.", "J. K. Rowling wrote it."),
        )
        // The opening clause is spoken early on purpose; "e.g." is not a break.
        assertAtEveryTokenSize(
            "Bring a snack, e.g. fruit, before the run. Then stretch.",
            listOf("Bring a snack,", "e.g. fruit, before the run.", "Then stretch."),
        )
    }

    @Test
    fun `thousands and clock times are not clause breaks`() {
        assertAtEveryTokenSize(
            "There were 12,500 people at 10:30 in the hall. It was packed.",
            listOf("There were 12,500 people at 10:30 in the hall.", "It was packed."),
        )
    }

    @Test
    fun `a sentence ending inside quotes or brackets still ends`() {
        assertAtEveryTokenSize(
            "She said \"see you soon.\" Then she left (quietly.) The end.",
            listOf("She said \"see you soon.\"", "Then she left (quietly.)", "The end."),
        )
    }
}
