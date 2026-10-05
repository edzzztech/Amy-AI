package io.github.edzzztech.amy.core

import android.content.Context
import android.speech.tts.TextToSpeech
import android.speech.tts.UtteranceProgressListener
import kotlinx.coroutines.channels.Channel
import java.util.Locale
import java.util.concurrent.atomic.AtomicInteger

/**
 * Speech out, with the desktop's two rules preserved:
 *
 *  - sentences are queued and spoken in order, so a streaming reply can start
 *    talking before the model has finished thinking
 *  - a barge-in drops everything still queued rather than letting her finish
 */
class Tts(context: Context) {

    private val ready = Channel<Boolean>(Channel.CONFLATED)
    private val counter = AtomicInteger(0)

    /** Bumped on every interruption; anything queued under an older turn is dropped. */
    @Volatile
    var generation: Int = 0
        private set

    // Deliberately not named onStart/onDone: inside the listener object below
    // those names resolve to its own methods, not to these properties.
    var onSpeakStart: (() -> Unit)? = null
    var onSpeakDone: (() -> Unit)? = null

    private val engine = TextToSpeech(context.applicationContext) { status ->
        ready.trySend(status == TextToSpeech.SUCCESS)
    }.apply {
        setOnUtteranceProgressListener(object : UtteranceProgressListener() {
            override fun onStart(utteranceId: String?) {
                onSpeakStart?.invoke()
            }

            override fun onDone(utteranceId: String?) {
                onSpeakDone?.invoke()
            }

            @Deprecated("Required by the base class")
            override fun onError(utteranceId: String?) {
                onSpeakDone?.invoke()
            }
        })
    }

    suspend fun awaitReady(): Boolean = ready.receive()

    fun setVoice(locale: Locale = Locale.UK, rate: Float = 1.0f, pitch: Float = 1.0f) {
        engine.language = locale
        engine.setSpeechRate(rate)
        engine.setPitch(pitch)
    }

    /**
     * Get as close to the desktop's voice as Android allows.
     *
     * The desktop speaks through edge-tts with en-GB-SoniaNeural. That voice
     * is a Microsoft cloud endpoint, so it cannot be used offline here — this
     * picks the nearest thing installed: a British English female voice,
     * preferring a network-free, higher-quality one.
     */
    fun useDesktopVoice() {
        setVoice(Locale.UK)
        val best = try {
            engine.voices
                ?.filter { it.locale.language == "en" && it.locale.country == "GB" }
                ?.filterNot { it.isNetworkConnectionRequired }
                ?.sortedWith(
                    compareByDescending<android.speech.tts.Voice> { v ->
                        // Android does not expose gender, so go on the name.
                        if (FEMALE_HINTS.any { it in v.name.lowercase() }) 1 else 0
                    }.thenByDescending { it.quality },
                )
                ?.firstOrNull()
        } catch (e: Exception) {
            null
        }
        if (best != null) engine.voice = best
    }

    private companion object {
        val FEMALE_HINTS = listOf(
            "female", "sonia", "libby", "hazel", "susan", "serena", "kate", "-f-",
        )
    }

    /** Queue one sentence. Pass the generation the text was produced under. */
    fun speak(text: String, turn: Int = generation) {
        if (turn != generation) return          // produced before an interruption
        val clean = text.trim()
        if (clean.isEmpty()) return
        engine.speak(clean, TextToSpeech.QUEUE_ADD, null, "amy-${counter.incrementAndGet()}")
    }

    /** Barge-in: stop now and invalidate everything still queued. */
    fun stop() {
        generation++
        engine.stop()
    }

    fun shutdown() {
        engine.stop()
        engine.shutdown()
    }
}
