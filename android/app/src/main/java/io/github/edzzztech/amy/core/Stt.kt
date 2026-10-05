package io.github.edzzztech.amy.core

import android.content.Context
import android.content.Intent
import android.os.Bundle
import android.speech.RecognitionListener
import android.speech.RecognizerIntent
import android.speech.SpeechRecognizer

/**
 * Speech in, through Android's own recogniser.
 *
 * This is the pragmatic first step rather than the final one: it is already on
 * the device, needs no model shipped with the app, and on most modern phones
 * runs on-device when [preferOffline] is set. whisper.cpp will replace it once
 * the rest of the loop works, at which point this class keeps its shape and
 * only the innards change.
 *
 * Must be constructed and driven from the main thread — that is a
 * SpeechRecognizer requirement, not a choice.
 */
class Stt(private val context: Context) {

    var onPartial: ((String) -> Unit)? = null
    var onFinal: ((String) -> Unit)? = null
    var onError: ((String) -> Unit)? = null
    var onReadyForSpeech: (() -> Unit)? = null
    var onEndOfSpeech: (() -> Unit)? = null

    /** Ask the recogniser to stay on-device. Honoured where the phone supports it. */
    var preferOffline: Boolean = true

    private var recognizer: SpeechRecognizer? = null

    val isAvailable: Boolean
        get() = SpeechRecognizer.isRecognitionAvailable(context)

    fun start() {
        if (!isAvailable) {
            onError?.invoke("No speech recogniser on this device")
            return
        }
        stop()
        val r = SpeechRecognizer.createSpeechRecognizer(context)
        r.setRecognitionListener(object : RecognitionListener {
            override fun onReadyForSpeech(params: Bundle?) { onReadyForSpeech?.invoke() }
            override fun onBeginningOfSpeech() {}
            override fun onRmsChanged(rmsdB: Float) {}
            override fun onBufferReceived(buffer: ByteArray?) {}
            override fun onEndOfSpeech() { onEndOfSpeech?.invoke() }

            override fun onPartialResults(partialResults: Bundle?) {
                firstResult(partialResults)?.let { onPartial?.invoke(it) }
            }

            override fun onResults(results: Bundle?) {
                val text = firstResult(results)
                if (text.isNullOrBlank()) onError?.invoke("Didn't catch that")
                else onFinal?.invoke(text)
            }

            override fun onError(error: Int) {
                this@Stt.onError?.invoke(describe(error))
            }

            override fun onEvent(eventType: Int, params: Bundle?) {}
        })

        val intent = Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH).apply {
            putExtra(
                RecognizerIntent.EXTRA_LANGUAGE_MODEL,
                RecognizerIntent.LANGUAGE_MODEL_FREE_FORM,
            )
            putExtra(RecognizerIntent.EXTRA_PARTIAL_RESULTS, true)
            putExtra(RecognizerIntent.EXTRA_MAX_RESULTS, 1)
            if (preferOffline) {
                putExtra(RecognizerIntent.EXTRA_PREFER_OFFLINE, true)
            }
        }
        recognizer = r
        r.startListening(intent)
    }

    fun stop() {
        recognizer?.let { r ->
            try {
                r.stopListening()
                r.cancel()
                r.destroy()
            } catch (e: Exception) {
                // Destroying an already-dead recogniser is not worth reporting.
            }
        }
        recognizer = null
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
}
