package io.github.edzzztech.amy.core

import android.content.Context
import android.speech.tts.TextToSpeech
import android.speech.tts.UtteranceProgressListener
import android.speech.tts.Voice
import kotlinx.coroutines.channels.Channel
import java.util.Locale
import java.util.concurrent.atomic.AtomicInteger

/**
 * Speech out.
 *
 * Three rules, all of which earlier versions got wrong in some way:
 *
 *  - **"Speaking" means the whole reply, not each sentence.** A streamed reply
 *    is queued one sentence at a time, and the platform reports start and done
 *    per sentence. Reporting those directly made her flicker between speaking
 *    and idle mid-answer — and the listener uses that state to decide when it
 *    is safe to hear you again. So utterances are counted, and listeners hear
 *    one start when the first begins and one done when the last ends.
 *  - **Barge-in invalidates what is still coming.** A reply keeps streaming
 *    from the model after you interrupt. Each sentence carries the [turn] it was
 *    produced under; stop() advances the turn, so anything from the old reply
 *    that arrives afterwards is dropped instead of spoken.
 *  - **Callbacks arrive on a TTS thread**, so state here is atomic.
 */
class Tts(context: Context) {

    private val ready = Channel<Boolean>(Channel.CONFLATED)
    private val counter = AtomicInteger(0)
    private val pending = AtomicInteger(0)

    /** Bumped on every interruption; text produced under an older turn is dropped. */
    @Volatile
    var turn: Int = 0
        private set

    /** True from the first queued sentence until the last one finishes. */
    val isTalking: Boolean
        get() = pending.get() > 0

    // Deliberately not named onStart/onDone: inside the listener object below
    // those names resolve to its own methods, not to these properties.
    var onSpeakStart: (() -> Unit)? = null
    var onSpeakDone: (() -> Unit)? = null

    private val engine = TextToSpeech(context.applicationContext) { status ->
        ready.trySend(status == TextToSpeech.SUCCESS)
    }.apply {
        setOnUtteranceProgressListener(object : UtteranceProgressListener() {
            override fun onStart(utteranceId: String?) {}

            override fun onDone(utteranceId: String?) = finished()

            override fun onStop(utteranceId: String?, interrupted: Boolean) = finished()

            @Deprecated("Required by the base class")
            override fun onError(utteranceId: String?) = finished()

            override fun onError(utteranceId: String?, errorCode: Int) = finished()
        })
    }

    /**
     * One utterance ended; if it was the last, the reply is over.
     *
     * Only the 1 -> 0 transition reports done. After stop() zeroes the count,
     * the engine still delivers onStop for the sentence it cut off; that must
     * not report done a second time. A CAS loop rather than updateAndGet,
     * because updateAndGet may retry its lambda and fire twice under contention.
     */
    private fun finished() {
        while (true) {
            val current = pending.get()
            if (current <= 0) return
            if (pending.compareAndSet(current, current - 1)) {
                if (current == 1) onSpeakDone?.invoke()
                return
            }
        }
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
     * The desktop speaks through edge-tts with en-GB-SoniaNeural, a Microsoft
     * cloud endpoint that cannot be used offline here. This picks the nearest
     * thing installed: British English, female by name, network-free, highest
     * quality available.
     */
    fun useDesktopVoice() {
        setVoice(Locale.UK)
        val best = try {
            engine.voices
                ?.filter { it.locale.language == "en" && it.locale.country == "GB" }
                ?.filterNot { it.isNetworkConnectionRequired }
                ?.sortedWith(
                    compareByDescending<Voice> { v ->
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

    /**
     * Queue one sentence. Pass the turn captured when the reply began; text
     * from a reply that has since been interrupted is silently dropped.
     */
    fun speak(text: String, turn: Int = this.turn) {
        if (turn != this.turn) return
        val clean = text.trim()
        if (clean.isEmpty()) return
        // Count it before handing it over, so a fast engine cannot report done
        // before we have recorded that it started.
        if (pending.getAndIncrement() == 0) onSpeakStart?.invoke()
        val result = engine.speak(
            clean, TextToSpeech.QUEUE_ADD, null, "amy-${counter.incrementAndGet()}",
        )
        if (result != TextToSpeech.SUCCESS) finished()
    }

    /** Barge-in: stop now, drop the queue, and invalidate the rest of this reply. */
    fun stop() {
        turn++
        val wasTalking = pending.getAndSet(0) > 0
        engine.stop()
        if (wasTalking) onSpeakDone?.invoke()
    }

    fun shutdown() {
        pending.set(0)
        engine.stop()
        engine.shutdown()
    }

    private companion object {
        val FEMALE_HINTS = listOf(
            "female", "sonia", "libby", "hazel", "susan", "serena", "kate", "-f-",
        )
    }
}
