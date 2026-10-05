package io.github.edzzztech.amy.core

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The microphone is always open, so this decides what she acts on. A false
 * positive means she butts into conversations; a false negative means she
 * ignores you.
 */
class WakeWordsTest {

    @Test
    fun `addressed when her name opens the utterance`() {
        assertTrue(WakeWords.isAddressed("Amy what's the time"))
        assertTrue(WakeWords.isAddressed("Amy, open Spotify"))
        assertTrue(WakeWords.isAddressed("amy"))
    }

    @Test
    fun `addressed after a polite opener`() {
        assertTrue(WakeWords.isAddressed("Hey Amy, what's the weather"))
        assertTrue(WakeWords.isAddressed("OK Amy turn it up"))
        assertTrue(WakeWords.isAddressed("um so Amy can you help"))
    }

    @Test
    fun `accepts the ways recognisers spell her name`() {
        listOf("Aimee", "Amie", "Emmy", "Ami", "Aimie", "Amey", "Aimy").forEach {
            assertTrue("should wake on $it", WakeWords.isAddressed("$it what's the time"))
        }
    }

    @Test
    fun `not addressed when she is only mentioned`() {
        assertFalse(WakeWords.isAddressed("I told Amy about it yesterday"))
        assertFalse(WakeWords.isAddressed("what do you think of Amy"))
    }

    @Test
    fun `not woken by words that merely contain the letters`() {
        assertFalse(WakeWords.isAddressed("steamy windows this morning"))
        assertFalse(WakeWords.isAddressed("the family is here"))
        assertFalse(WakeWords.isAddressed("dynamic range"))
        assertFalse(WakeWords.containsWake("steamy"))
    }

    @Test
    fun `nothing said is not addressed`() {
        assertFalse(WakeWords.isAddressed(""))
        assertFalse(WakeWords.isAddressed("   "))
        assertFalse(WakeWords.isAddressed("hey"))
    }

    @Test
    fun `strips her name and anything before it`() {
        assertEquals("what's the time?", WakeWords.stripWake("Hey Amy, what's the time?"))
        assertEquals("open Spotify", WakeWords.stripWake("Amy open Spotify"))
        assertEquals("turn it up", WakeWords.stripWake("Aimee - turn it up"))
        assertEquals("", WakeWords.stripWake("Amy"))
    }

    @Test
    fun `strip leaves text without her name alone`() {
        assertEquals("open Spotify", WakeWords.stripWake("  open Spotify "))
    }

    @Test
    fun `strip does not cut inside a longer word`() {
        // "family" contains "ami"; the real name comes later.
        assertEquals("call home", WakeWords.stripWake("family Amy call home"))
    }
}
