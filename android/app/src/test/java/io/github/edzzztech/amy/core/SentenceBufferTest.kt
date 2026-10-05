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

    @Test
    fun `decimals do not split a sentence`() {
        val (spoken, _) = stream("It costs 4.99 today. Shall I order it?")
        assertEquals(listOf("It costs 4.99 today.", "Shall I order it?"), spoken)
    }
}
