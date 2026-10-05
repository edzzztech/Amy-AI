package io.github.edzzztech.amy.core

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

/**
 * Which commands go to the computer. This exists because the original pattern
 * used "\b" in a Kotlin string — a backspace character, not a word boundary —
 * so nothing ever matched and no command reached the PC. It compiled, and a
 * test of a Python translation passed; only a test of the real code catches it.
 */
class DesktopRoutingTest {

    @Test
    fun `routes requests that name the computer`() {
        assertEquals("open chrome", Amy.forDesktop("on my pc open chrome"))
        assertEquals("summarise the report", Amy.forDesktop("On my computer, summarise the report"))
        assertEquals("open spotify", Amy.forDesktop("using the laptop open spotify"))
        assertEquals("take a screenshot", Amy.forDesktop("with my desktop: take a screenshot"))
        assertEquals("lock it", Amy.forDesktop("ON MY PC lock it"))
    }

    @Test
    fun `leaves everything else on the phone`() {
        assertNull(Amy.forDesktop("open spotify"))
        assertNull(Amy.forDesktop("what's on my screen?"))
        assertNull(Amy.forDesktop("tell me about my computer"))
    }

    @Test
    fun `needs something to send`() {
        assertNull(Amy.forDesktop("on my pc"))
        assertNull(Amy.forDesktop("on my pc,  "))
    }

    @Test
    fun `does not match a longer word`() {
        // "pcs" is not "pc": the boundary is what was broken before.
        assertNull(Amy.forDesktop("on my pcs open chrome"))
    }
}
