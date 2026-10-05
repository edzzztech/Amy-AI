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

    var onStart: (() -> Unit)? = null
    var onDone: (() -> Unit)? = null

    private val engine = TextToSpeech(context.applicationContext) { status ->
        ready.trySend(status == TextToSpeech.SUCCESS)
    }.apply {
        setOnUtteranceProgressListener(object : UtteranceProgressListener() {
            override fun onStart(utteranceId: String?) { onStart?.invoke() }
            override fun onDone(utteranceId: String?) { onDone?.invoke() }
            @Deprecated("Required by the base class")
            override fun onError(utteranceId: String?) { onDone?.invoke() }
        })
    }

    suspend fun awaitReady(): Boolean = ready.receive()

    fun setVoice(locale: Locale = Locale.UK, rate: Float = 1.0f, pitch: Float = 1.0f) {
        engine.language = locale
        engine.setSpeechRate(rate)
        engine.setPitch(pitch)
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
