package io.github.edzzztech.amy.core

import org.junit.Assert.assertEquals
import org.junit.Test

class SpeakerTagTest {

    private fun run(vararg pieces: String): String {
        val tag = SpeakerTag()
        return buildString {
            pieces.forEach { append(tag.push(it)) }
            append(tag.flush())
        }
    }

    @Test
    fun aLabelIsRemovedHoweverItArrives() {
        // As Gemma 3n actually streamed it.
        assertEquals(" There are 3 days until Friday.", run("Amy", ":", " There", " are 3 days until Friday."))
        assertEquals("Hello.", run("Amy: Hello."))
        assertEquals("Sure.", run("  assistant:", "Sure."))
        assertEquals("Fine.", run("Model : ", "Fine."))
    }

    @Test
    fun ordinaryRepliesPassUntouched() {
        assertEquals("Amy is my name.", run("Amy", " is my name."))
        assertEquals("Paris is the capital of France.", run("Paris", " is", " the capital of France."))
        assertEquals("Am I right? Yes.", run("Am", " I right? Yes."))
        assertEquals("Amy", run("Amy"))                       // a reply that is just the name
        assertEquals("Later, Amy: hi", run("Later, ", "Amy: hi")) // only at the start
    }
}
