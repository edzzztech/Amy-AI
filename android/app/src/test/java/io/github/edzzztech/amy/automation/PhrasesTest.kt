package io.github.edzzztech.amy.automation

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class PhrasesTest {

    private fun cmd(said: String) = Phrases.bare(said).lowercase()

    // --- tidying -------------------------------------------------------------

    @Test
    fun politeFormsReduceToTheCommand() {
        assertEquals("open Spotify", Phrases.bare("Could you please open Spotify for me?"))
        assertEquals("open spotify", Phrases.bare("open spotify, please."))
        assertEquals("turn the torch on", Phrases.bare("Can you just turn the torch on now"))
        // Words that are the whole message are left alone.
        assertEquals("thank you", Phrases.bare("thank you"))
        assertEquals("please", Phrases.bare("please"))
    }

    @Test
    fun typedTextKeepsItsEnding() {
        // "now" and "please" are part of what to type, not politeness.
        assertEquals("I'll be there now", Phrases.typed(Phrases.politeStartRemoved("please type I'll be there now")))
        assertEquals("Hello World!", Phrases.typed("Type Hello World!"))
    }

    // --- things that must not be commands -------------------------------------

    @Test
    fun conversationIsNotMistakenForCommands() {
        val chat = listOf(
            "what apps should i use for budgeting",
            "write me a poem about cats",
            "type of dog that suits a flat",
            "what's the time in tokyo",
            "how do i set a timer on my oven",
            "tell me about the battery in electric cars",
        )
        for (said in chat) {
            val lower = cmd(said)
            assertFalse(said, Phrases.asksAppCount(lower))
            assertFalse(said, Phrases.asksTime(lower))
            assertFalse(said, Phrases.asksBattery(lower))
            assertNull(said, Phrases.typed(said))
            assertNull(said, Phrases.timer(lower))
            assertNull(said, Phrases.torch(lower))
            assertNull(said, Phrases.volume(lower))
        }
    }

    // --- questions answered by the phone ----------------------------------------

    @Test
    fun timeDateBatteryAndLog() {
        assertTrue(Phrases.asksTime(cmd("What time is it?")))
        assertTrue(Phrases.asksTime(cmd("what's the time")))
        assertTrue(Phrases.asksDate(cmd("What's the date today?")))
        assertTrue(Phrases.asksDate(cmd("what day is it")))
        assertTrue(Phrases.asksBattery(cmd("What's my battery at?")))
        assertTrue(Phrases.asksBattery(cmd("how much battery have I got")))
        assertTrue(Phrases.asksAppCount(cmd("How many apps can you see?")))
        assertTrue(Phrases.asksScreen(cmd("What's on my screen?")))
        assertTrue(Phrases.asksToLook(cmd("What am I looking at?")))
        assertTrue(Phrases.asksToLook(cmd("Could you take a look at this?")))
        assertTrue(Phrases.asksToLook(cmd("what can you see")))
        // The screen is a different question, and "this" alone is too vague.
        assertFalse(Phrases.asksToLook(cmd("what can you see on my screen")))
        assertFalse(Phrases.asksToLook(cmd("what's this")))
        assertEquals(24, Phrases.actionLogHours(cmd("What have you done today?")))
        assertEquals(3, Phrases.actionLogHours(cmd("what have you done in the last 3 hours")))
        assertNull(Phrases.actionLogHours(cmd("what have you done to my phone")))
    }

    // --- opening apps ------------------------------------------------------------

    @Test
    fun openingApps() {
        assertEquals(Phrases.Open("open", "spotify"), Phrases.open(cmd("Open Spotify")))
        assertEquals("camera", Phrases.open(cmd("open up the camera app"))?.name)
        assertTrue(Phrases.open(cmd("launch maps"))!!.definite)
        assertFalse(Phrases.open(cmd("start a conversation about bees"))!!.definite)
        assertNull(Phrases.open(cmd("opening hours for the library")))
    }

    // --- device controls -----------------------------------------------------------

    @Test
    fun torchAndVolume() {
        assertEquals(true, Phrases.torch(cmd("turn on the torch")))
        assertEquals(false, Phrases.torch(cmd("Flashlight off")))
        assertEquals(true, Phrases.torch(cmd("turn the flashlight on please")))
        assertEquals(Phrases.Volume.Step(up = true), Phrases.volume(cmd("volume up")))
        assertEquals(Phrases.Volume.Step(up = false), Phrases.volume(cmd("turn it down")))
        assertEquals(Phrases.Volume.Step(up = true), Phrases.volume(cmd("louder")))
        assertEquals(Phrases.Volume.Percent(40), Phrases.volume(cmd("set the volume to 40 percent")))
        assertEquals(Phrases.Volume.Percent(100), Phrases.volume(cmd("max volume")))
        assertEquals(Phrases.Volume.Percent(100), Phrases.volume(cmd("volume 250")))
    }

    // --- timers ----------------------------------------------------------------------

    @Test
    fun timers() {
        assertEquals(300, Phrases.timer(cmd("Set a timer for 5 minutes")))
        assertEquals(600, Phrases.timer(cmd("start a ten minute timer")))
        assertEquals(90, Phrases.timer(cmd("timer for 90 seconds")))
        assertEquals(5400, Phrases.timer(cmd("set a timer for an hour and a half")))
        assertEquals(5400, Phrases.timer(cmd("set a timer for one and a half hours")))
        assertEquals(1800, Phrases.timer(cmd("set a timer for half an hour")))
        assertEquals(1500, Phrases.timer(cmd("set a timer for twenty five minutes")))
        assertEquals(4830, Phrases.timer(cmd("set a timer for 1 hour 20 minutes and 30 seconds")))
        assertEquals(300, Phrases.timer(cmd("set a timer for 5")))          // bare = minutes
        assertNull(Phrases.timer(cmd("set a timer")))
        assertNull(Phrases.timer(cmd("cancel the timer")))
        assertNull(Phrases.timer(cmd("set a timer for 30 hours")))           // past a day
    }

    @Test
    fun describingDurations() {
        assertEquals("5 minutes", Phrases.describe(300))
        assertEquals("1 hour and 30 minutes", Phrases.describe(5400))
        assertEquals("1 hour, 20 minutes and 30 seconds", Phrases.describe(4830))
        assertEquals("1 second", Phrases.describe(1))
    }

    // --- alarms ------------------------------------------------------------------------

    @Test
    fun alarmTimes() {
        // Said at 14:00.
        assertEquals(7 to 0, Phrases.alarm(cmd("Set an alarm for 7am"), 14, 0))
        assertEquals(19 to 30, Phrases.alarm(cmd("set an alarm for 7:30 pm"), 14, 0))
        assertEquals(19 to 30, Phrases.alarm(cmd("set an alarm for 7.30 p.m."), 14, 0))
        assertEquals(6 to 30, Phrases.alarm(cmd("wake me up at half past six in the morning"), 14, 0))
        assertEquals(7 to 45, Phrases.alarm(cmd("set an alarm for quarter to 8 am"), 14, 0))
        assertEquals(12 to 0, Phrases.alarm(cmd("set an alarm for noon"), 14, 0))
        assertEquals(21 to 15, Phrases.alarm(cmd("set an alarm for 21:15"), 14, 0))
        assertEquals(6 to 15, Phrases.alarm(cmd("set an alarm for six fifteen am"), 14, 0))
        assertEquals(7 to 0, Phrases.alarm(cmd("wake me up in the morning at 7"), 22, 0))
    }

    @Test
    fun alarmWithoutAmOrPmMeansTheNextOne() {
        assertEquals(19 to 0, Phrases.alarm(cmd("set an alarm for 7"), 14, 0))   // afternoon -> 7pm
        assertEquals(7 to 0, Phrases.alarm(cmd("set an alarm for 7"), 22, 0))    // late night -> 7am
        assertEquals(7 to 0, Phrases.alarm(cmd("set an alarm for 7"), 3, 0))     // small hours -> 7am
    }

    @Test
    fun alarmsRelativeToNow() {
        assertEquals(14 to 20, Phrases.alarm(cmd("set an alarm in 20 minutes"), 14, 0))
        assertEquals(15 to 30, Phrases.alarm(cmd("wake me up in an hour and a half"), 14, 0))
        assertEquals(0 to 10, Phrases.alarm(cmd("set an alarm for 20 minutes from now"), 23, 50))
    }

    @Test
    fun nonsenseTimesAreRefused() {
        assertNull(Phrases.alarm(cmd("set an alarm for 25:00"), 14, 0))
        assertNull(Phrases.alarm(cmd("set an alarm for 13 pm"), 14, 0))
        assertNull(Phrases.alarm(cmd("set an alarm for 7:75"), 14, 0))
        assertNull(Phrases.alarm(cmd("set an alarm for whenever"), 14, 0))
    }
}
