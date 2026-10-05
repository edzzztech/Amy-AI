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
 *  - **One recogniser, reused.** Creating and destroying one per utterance makes
 *    most devices replay their start cue every cycle — a chime every second.
 *  - **It can be reset.** A reused recogniser occasionally wedges: it reports
 *    busy forever, or stops calling back at all. [reset] throws it away so the
 *    next pass gets a fresh one, and the listener uses it when that happens.
 *  - **Errors carry their code.** Silence and no-match are the normal case when
 *    nobody is talking; the caller needs to tell those apart from real faults.
 *
 * Must be driven from the main thread — a SpeechRecognizer requirement.
 */
class Stt(private val context: Context) {

    var onPartial: ((String) -> Unit)? = null
    var onFinal: ((String) -> Unit)? = null
    /** (error code, human description). Codes are SpeechRecognizer.ERROR_*. */
    var onError: ((Int, String) -> Unit)? = null
    var onReadyForSpeech: (() -> Unit)? = null
    var onEndOfSpeech: (() -> Unit)? = null

    /** Ask the recogniser to stay on-device. Honoured where the phone supports it. */
    var preferOffline: Boolean = true

    /**
     * Silence the device's listening cues while she is listening.
     *
     * Off by default, deliberately. Muting the system and notification streams
     * silences everything else on the phone too, so an always-on assistant
     * would keep your handset on silent all day. Reusing one recogniser already
     * removes the repeated chime; this exists only for devices that still cue
     * on every pass, and it is yours to switch on.
     */
    var suppressCues: Boolean = false

    private var recognizer: SpeechRecognizer? = null
    private var mutedStreams = false

    /** True between startListening and the matching result or error. */
    var isListening: Boolean = false
        private set

    private val audio: AudioManager? =
        context.getSystemService(Context.AUDIO_SERVICE) as? AudioManager

    val isAvailable: Boolean
        get() = SpeechRecognizer.isRecognitionAvailable(context)

    private val listener = object : RecognitionListener {
        override fun onReadyForSpeech(params: Bundle?) { onReadyForSpeech?.invoke() }
        override fun onBeginningOfSpeech() {}

        override fun onRmsChanged(rmsdB: Float) {
            // RMS arrives roughly -2..10 dB; map to 0..1 so the orb swells with
            // your voice rather than a synthetic pulse.
            AmyState.setLevel(((rmsdB + 2f) / 12f).coerceIn(0f, 1f))
        }

        override fun onBufferReceived(buffer: ByteArray?) {}

        override fun onEndOfSpeech() {
            AmyState.setLevel(0f)
            onEndOfSpeech?.invoke()
        }

        override fun onPartialResults(partialResults: Bundle?) {
            firstResult(partialResults)?.let { onPartial?.invoke(it) }
        }

        override fun onResults(results: Bundle?) {
            finishPass()
            val text = firstResult(results)
            if (text.isNullOrBlank()) {
                onError?.invoke(SpeechRecognizer.ERROR_NO_MATCH, "Didn't catch that")
            } else {
                onFinal?.invoke(text)
            }
        }

        override fun onError(error: Int) {
            finishPass()
            this@Stt.onError?.invoke(error, describe(error))
        }

        override fun onEvent(eventType: Int, params: Bundle?) {}
    }

    private fun finishPass() {
        isListening = false
        AmyState.setLevel(0f)
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
        if (isListening) return          // already going; restarting would cue again
        val r = ensureRecognizer()
        if (r == null) {
            onError?.invoke(SpeechRecognizer.ERROR_CLIENT, "No speech recogniser on this device")
            return
        }
        if (suppressCues) muteCues()
        isListening = true
        try {
            r.startListening(intent())
        } catch (e: Exception) {
            isListening = false
            onError?.invoke(SpeechRecognizer.ERROR_CLIENT, "Could not start listening")
        }
    }

    /** Stop listening but keep the recogniser, so resuming makes no sound. */
    fun pause() {
        isListening = false
        AmyState.setLevel(0f)
        try {
            recognizer?.cancel()
        } catch (e: Exception) {
            // Cancelling an idle recogniser is not worth reporting.
        }
        restoreCues()
    }

    /**
     * Throw the recogniser away. The next [listen] builds a fresh one. Used when
     * a reused instance has wedged — busy forever, or silent with no callbacks.
     */
    fun reset() {
        release()
    }

    /** Fully release. On shutdown, or to recover from a wedged recogniser. */
    fun stop() = release()

    private fun release() {
        isListening = false
        AmyState.setLevel(0f)
        try {
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
        putExtra(RecognizerIntent.EXTRA_MAX_RESULTS, 3)
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

    /**
     * The best hypothesis — but if any alternative contains her name and the
     * best does not, prefer that one. Recognisers often rank "I me" above "Amy";
     * asking for three hypotheses and checking them all is the cheap fix.
     */
    private fun firstResult(bundle: Bundle?): String? {
        val all = bundle?.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION)
            ?.map { it.trim() }
            ?.filter { it.isNotEmpty() }
            .orEmpty()
        if (all.isEmpty()) return null
        return all.firstOrNull { WakeWords.containsWake(it) } ?: all.first()
    }

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
        val CUE_STREAMS = listOf(AudioManager.STREAM_SYSTEM, AudioManager.STREAM_NOTIFICATION)
    }
}
