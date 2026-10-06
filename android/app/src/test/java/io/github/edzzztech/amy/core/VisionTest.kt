package io.github.edzzztech.amy.core

import io.github.edzzztech.amy.core.LocalModel.Choice
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class VisionTest {

    // --- which runtime runs the model on the phone -----------------------------

    @Test
    fun aLiteRtModelWinsOnA64BitPhone() {
        assertEquals(Choice.LITERT, LocalModel.choose(hasLiteRtModel = true, hasTaskModel = false, liteRtUsable = true))
        // Both present: the one that can see is preferred.
        assertEquals(Choice.LITERT, LocalModel.choose(hasLiteRtModel = true, hasTaskModel = true, liteRtUsable = true))
    }

    @Test
    fun anExistingTaskModelKeepsWorking() {
        assertEquals(Choice.MEDIAPIPE, LocalModel.choose(hasLiteRtModel = false, hasTaskModel = true, liteRtUsable = true))
        // A 32-bit phone with both falls back to the one it can run.
        assertEquals(Choice.MEDIAPIPE, LocalModel.choose(hasLiteRtModel = true, hasTaskModel = true, liteRtUsable = false))
    }

    @Test
    fun noUsableModelIsReportedAsSuch() {
        assertEquals(Choice.UNSUPPORTED, LocalModel.choose(hasLiteRtModel = true, hasTaskModel = false, liteRtUsable = false))
        assertEquals(Choice.NONE, LocalModel.choose(hasLiteRtModel = false, hasTaskModel = false, liteRtUsable = true))
    }

    @Test
    fun liteRtNeedsA64BitProcessor() {
        assertTrue(LiteRtLlm.deviceSupported(arrayOf("arm64-v8a", "armeabi-v7a", "armeabi")))
        assertTrue(LiteRtLlm.deviceSupported(arrayOf("x86_64", "x86")))
        assertFalse(LiteRtLlm.deviceSupported(arrayOf("armeabi-v7a", "armeabi")))
        assertFalse(LiteRtLlm.deviceSupported(arrayOf("x86")))
    }

    // --- sizing pictures for the vision encoder ----------------------------------

    @Test
    fun aPhoneCameraPhotoIsDecodedSmallThenFitted() {
        // 12 MP, 4000 x 3000: decoded at a quarter (1000 x 750), then fitted to 768.
        assertEquals(4, Pictures.sampleSize(4000, 3000))
        assertEquals(768 to 576, Pictures.fit(4000 / 4, 3000 / 4))
        // Portrait keeps its shape.
        assertEquals(576 to 768, Pictures.fit(750, 1000))
    }

    @Test
    fun theDecodeNeverGoesBelowTheTarget() {
        for ((w, h) in listOf(4000 to 3000, 1536 to 1536, 1535 to 900, 800 to 600, 100 to 80, 9000 to 100)) {
            val s = Pictures.sampleSize(w, h)
            assertTrue("$w x $h at 1/$s", maxOf(w, h) / s >= minOf(Pictures.MAX_SIDE, maxOf(w, h)))
        }
    }

    @Test
    fun smallPicturesAreLeftAlone() {
        assertEquals(1, Pictures.sampleSize(640, 480))
        assertEquals(640 to 480, Pictures.fit(640, 480))
        assertEquals(768 to 768, Pictures.fit(768, 768))
    }

    @Test
    fun extremeShapesKeepAtLeastOnePixel() {
        assertEquals(768 to 1, Pictures.fit(20000, 10))
    }
}
