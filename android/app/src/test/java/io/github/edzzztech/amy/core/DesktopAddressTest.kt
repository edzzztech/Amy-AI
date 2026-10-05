package io.github.edzzztech.amy.core

import org.junit.Assert.assertEquals
import org.junit.Test

class DesktopAddressTest {

    private fun tidy(typed: String) = DesktopLink.normalise(typed)

    @Test
    fun theFormShownOnTheComputerIsKept() {
        assertEquals("192.168.1.24:8765", tidy("192.168.1.24:8765"))
        assertEquals("192.168.1.24:8765", tidy("  192.168.1.24:8765 "))
    }

    @Test
    fun schemesAndPathsAreRemoved() {
        assertEquals("192.168.1.24:8765", tidy("http://192.168.1.24:8765/"))
        assertEquals("192.168.1.24:8765", tidy("HTTP://192.168.1.24:8765/status"))
        assertEquals("my-pc.local:9000", tidy("https://my-pc.local:9000"))
    }

    @Test
    fun aMissingPortGetsTheDefault() {
        assertEquals("192.168.1.24:8765", tidy("192.168.1.24"))
        assertEquals("192.168.1.24:8765", tidy("192.168.1.24:"))
        assertEquals("desktop:8765", tidy("desktop"))
    }

    @Test
    fun ipv6IsBracketed() {
        assertEquals("[fe80::1]:8765", tidy("fe80::1"))
        assertEquals("[fe80::1]:8765", tidy("[fe80::1]"))
        assertEquals("[fe80::1]:9000", tidy("[fe80::1]:9000"))
    }

    @Test
    fun emptyStaysEmpty() {
        assertEquals("", tidy(""))
        assertEquals("", tidy("   "))
        assertEquals("", tidy("http://"))
    }
}
