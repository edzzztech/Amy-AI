package io.github.edzzztech.amy.core

import android.content.Context
import android.content.Intent
import android.media.AudioManager
import android.os.Bundle
import android.speech.RecognitionListener
import android.speech.RecognizerIntent
import android.speech.SpeechRecognizer

/**
 * Speech in, built for listening continuously rather than in bursts.
 *
 * Two things matter for an always-on assistant, and getting either wrong is
 * what makes it sound like it is looping:
 *
 *  - **One recogniser, reused.** Creating and destroying a SpeechRecognizer
 *    per utterance makes most devices replay their start cue every cycle, so
 *    you hear a beep every second or so. The instance is created once and
 *    simply told to listen again.
 *  - **The cues are muted while it runs.** Even reused, many devices chime on
 *    start and end. Those streams are silenced for the duration and restored
 *    afterwards, so nothing is left muted if she is stopped.
 *
 * Must be driven from the main thread — a SpeechRecognizer requirement.
 */
class Stt(private val context: Context) {

    var onPartial: ((String) -> Unit)? = null
    var onFinal: ((String) -> Unit)? = null
    var onError: ((String) -> Unit)? = null
    var onReadyForSpeech: (() -> Unit)? = null
    var onEndOfSpeech: (() -> Unit)? = null

    /** Ask the recogniser to stay on-device. Honoured where the phone supports it. */
    var preferOffline: Boolean = true

    /**
     * Silence the device's listening cues while she is listening.
     *
     * Off by default, deliberately. Muting the system and notification streams
     * is a blunt instrument: it silences everything else on the phone too, so
     * an always-on assistant would effectively keep your handset on silent all
     * day and you would miss messages. Reusing one recogniser already removes
     * the repeated chime, which was the real problem; this is only here for
     * devices that still cue on every pass, and it is yours to switch on.
     */
    var suppressCues: Boolean = false

    private var recognizer: SpeechRecognizer? = null
    private var listening = false
    private var mutedStreams = false

    private val audio: AudioManager? =
        context.getSystemService(Context.AUDIO_SERVICE) as? AudioManager

    val isAvailable: Boolean
        get() = SpeechRecognizer.isRecognitionAvailable(context)

    private val listener = object : RecognitionListener {
        override fun onReadyForSpeech(params: Bundle?) { onReadyForSpeech?.invoke() }
        override fun onBeginningOfSpeech() {}
        override fun onRmsChanged(rmsdB: Float) {
            // RMS arrives roughly -2..10 dB from the recogniser; map it to 0..1
            // so the orb swells with your voice rather than a synthetic pulse.
            AmyState.setLevel(((rmsdB + 2f) / 12f).coerceIn(0f, 1f))
        }
        override fun onBufferReceived(buffer: ByteArray?) {}

        override fun onEndOfSpeech() {
            listening = false
            onEndOfSpeech?.invoke()
        }

        override fun onPartialResults(partialResults: Bundle?) {
            firstResult(partialResults)?.let { onPartial?.invoke(it) }
        }

        override fun onResults(results: Bundle?) {
            listening = false
            val text = firstResult(results)
            if (text.isNullOrBlank()) onError?.invoke("Didn't catch that")
            else onFinal?.invoke(text)
        }

        override fun onError(error: Int) {
            listening = false
            this@Stt.onError?.invoke(describe(error))
        }

        override fun onEvent(eventType: Int, params: Bundle?) {}
    }

    /** Create the recogniser once; [listen] is then cheap to call repeatedly. */
    private fun ensureRecognizer(): SpeechRecognizer? {
        if (!isAvailable) return null
        recognizer?.let { return it }
        return try {
            SpeechRecognizer.createSpeechRecognizer(context).also {
                it.setRecognitionListener(listener)
                recognizer = it
            }
        } catch (e: Exception) {
            null
        }
    }

    /** Start one listening pass on the existing recogniser. */
    fun listen() {
        val r = ensureRecognizer()
        if (r == null) {
            onError?.invoke("No speech recogniser on this device")
            return
        }
        if (listening) return          // already going; restarting would cue again
        if (suppressCues) muteCues()
        listening = true
        try {
            r.startListening(intent())
        } catch (e: Exception) {
            listening = false
            onError?.invoke("Could not start listening")
        }
    }

    /** Kept for the old call sites; identical to [listen] now. */
    fun start() = listen()

    /** Stop listening but keep the recogniser, so resuming makes no sound. */
    fun pause() {
        listening = false
        AmyState.setLevel(0f)
        try {
            recognizer?.cancel()
        } catch (e: Exception) {
            // Cancelling an idle recogniser is not worth reporting.
        }
        restoreCues()
    }

    /** Fully release. Only on shutdown — this is what makes the next start audible. */
    fun stop() {
        listening = false
        try {
            recognizer?.stopListening()
            recognizer?.cancel()
            recognizer?.destroy()
        } catch (e: Exception) {
            // Destroying an already-dead recogniser is not worth reporting.
        }
        recognizer = null
        restoreCues()
    }

    private fun intent() = Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH).apply {
        putExtra(RecognizerIntent.EXTRA_LANGUAGE_MODEL, RecognizerIntent.LANGUAGE_MODEL_FREE_FORM)
        putExtra(RecognizerIntent.EXTRA_PARTIAL_RESULTS, true)
        putExtra(RecognizerIntent.EXTRA_MAX_RESULTS, 1)
        if (preferOffline) putExtra(RecognizerIntent.EXTRA_PREFER_OFFLINE, true)
    }

    /**
     * Silence the device's listening cues. Wrapped because the notification and
     * system streams are protected by Do Not Disturb policy on some builds and
     * throw rather than failing quietly.
     */
    private fun muteCues() {
        if (mutedStreams) return
        val am = audio ?: return
        CUE_STREAMS.forEach { stream ->
            try {
                am.adjustStreamVolume(stream, AudioManager.ADJUST_MUTE, 0)
            } catch (e: Exception) {
                // Blocked by DND policy; the cue stays audible on this device.
            }
        }
        mutedStreams = true
    }

    private fun restoreCues() {
        if (!mutedStreams) return
        val am = audio ?: return
        CUE_STREAMS.forEach { stream ->
            try {
                am.adjustStreamVolume(stream, AudioManager.ADJUST_UNMUTE, 0)
            } catch (e: Exception) {
                // Nothing to restore if muting was refused in the first place.
            }
        }
        mutedStreams = false
    }

    private fun firstResult(bundle: Bundle?): String? =
        bundle?.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION)
            ?.firstOrNull()
            ?.trim()
            ?.takeIf { it.isNotEmpty() }

    private fun describe(error: Int): String = when (error) {
        SpeechRecognizer.ERROR_AUDIO -> "Microphone trouble"
        SpeechRecognizer.ERROR_CLIENT -> "Recogniser client error"
        SpeechRecognizer.ERROR_INSUFFICIENT_PERMISSIONS -> "Microphone permission not granted"
        SpeechRecognizer.ERROR_NETWORK -> "Network error"
        SpeechRecognizer.ERROR_NETWORK_TIMEOUT -> "Network timed out"
        SpeechRecognizer.ERROR_NO_MATCH -> "Didn't catch that"
        SpeechRecognizer.ERROR_RECOGNIZER_BUSY -> "Recogniser busy"
        SpeechRecognizer.ERROR_SERVER -> "Recognition server error"
        SpeechRecognizer.ERROR_SPEECH_TIMEOUT -> "Heard nothing"
        else -> "Recognition failed ($error)"
    }

    private companion object {
        /** The streams devices use for the listening chime. */
        val CUE_STREAMS = listOf(
            AudioManager.STREAM_SYSTEM,
            AudioManager.STREAM_NOTIFICATION,
        )
    }
}
