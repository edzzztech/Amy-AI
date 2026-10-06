package io.github.edzzztech.amy.core

import android.content.Context
import android.content.Intent
import android.media.AudioManager
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.speech.RecognitionListener
import android.speech.RecognizerIntent
import android.speech.SpeechRecognizer
import androidx.core.content.edit

/**
 * Speech in, built for listening continuously rather than in bursts.
 *
 *  - **No chime.** Listening continuously means restarting the recogniser every
 *    few seconds, and Google's plays a cue each time it starts and stops: a
 *    sound on a loop. Android 12+ has an on-device recogniser with no cue at
 *    all, used wherever the phone has it. Elsewhere [CueGuard] mutes the
 *    media stream for the moment the cue plays, and only when nothing is
 *    playing. Notification, ringer and alarm sounds are never touched.
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
     * Mute the listening cue on phones without the on-device recogniser. Only
     * the media stream, only while nothing is playing, and only for the
     * moment the cue plays; see [CueGuard].
     */
    var suppressCues: Boolean = true

    /**
     * The on-device recogniser, which makes no sound, where the phone has
     * one. Dropped for Google's usual recogniser if it turns out not to
     * support the language or fails to start.
     */
    private var onDevice: Boolean =
        Build.VERSION.SDK_INT >= Build.VERSION_CODES.S &&
            runCatching { SpeechRecognizer.isOnDeviceRecognitionAvailable(context) }.getOrDefault(false)

    private var askedForDownload = false

    private var recognizer: SpeechRecognizer? = null

    /** True between startListening and the matching result or error. */
    var isListening: Boolean = false
        private set

    private val cues = CueGuard(context)

    val isAvailable: Boolean
        get() = onDevice || SpeechRecognizer.isRecognitionAvailable(context)

    private val listener = object : RecognitionListener {
        override fun onReadyForSpeech(params: Bundle?) {
            cues.restoreSoon()          // the start cue has played by now
            onReadyForSpeech?.invoke()
        }
        override fun onBeginningOfSpeech() {}

        override fun onRmsChanged(rmsdB: Float) {
            // RMS arrives roughly -2..10 dB; map to 0..1 so the orb swells with
            // your voice rather than a synthetic pulse.
            AmyState.setLevel(((rmsdB + 2f) / 12f).coerceIn(0f, 1f))
        }

        override fun onBufferReceived(buffer: ByteArray?) {}

        override fun onEndOfSpeech() {
            AmyState.setLevel(0f)
            hush()                      // the stop cue plays as recognition ends
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
            if (onDevice && error in ON_DEVICE_GIVE_UP) {
                // No on-device model for this language, or the service is
                // missing: fall back to the usual recogniser. Ask the phone to
                // fetch the language once, so a later start can be silent.
                if (error == LANGUAGE_UNAVAILABLE && !askedForDownload &&
                    Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU
                ) {
                    askedForDownload = true
                    runCatching { recognizer?.triggerModelDownload(intent()) }
                }
                // The listener resets the recogniser on ERROR_CLIENT, and the
                // next one is built without onDevice.
                onDevice = false
                this@Stt.onError?.invoke(SpeechRecognizer.ERROR_CLIENT, "Switching recogniser")
                return
            }
            this@Stt.onError?.invoke(error, describe(error))
        }

        override fun onEvent(eventType: Int, params: Bundle?) {}
    }

    private fun finishPass() {
        isListening = false
        AmyState.setLevel(0f)
        cues.restoreSoon()
    }

    /** Mute the media stream for a cue, unless the recogniser makes none. */
    private fun hush() {
        if (suppressCues && !onDevice) cues.hush()
    }

    /** Sound back on now: she is about to talk. Safe from any thread. */
    fun restoreCues() = cues.restore()

    /** Create the recogniser once; [listen] is then cheap to call repeatedly. */
    private fun ensureRecognizer(): SpeechRecognizer? {
        if (!isAvailable) return null
        recognizer?.let { return it }
        return try {
            val created = if (onDevice && Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
                SpeechRecognizer.createOnDeviceSpeechRecognizer(context)
            } else {
                SpeechRecognizer.createSpeechRecognizer(context)
            }
            created.also {
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
        hush()                          // the start cue plays as listening begins
        isListening = true
        try {
            r.startListening(intent())
        } catch (e: Exception) {
            isListening = false
            cues.restore()
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
        cues.restore()
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
        cues.restore()
    }

    private fun intent() = Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH).apply {
        putExtra(RecognizerIntent.EXTRA_LANGUAGE_MODEL, RecognizerIntent.LANGUAGE_MODEL_FREE_FORM)
        putExtra(RecognizerIntent.EXTRA_PARTIAL_RESULTS, true)
        putExtra(RecognizerIntent.EXTRA_MAX_RESULTS, 3)
        if (preferOffline) putExtra(RecognizerIntent.EXTRA_PREFER_OFFLINE, true)
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
        // SpeechRecognizer's codes for these, added in Android 12, written out
        // so older phones are not asked for a constant they lack.
        const val SERVER_DISCONNECTED = 11
        const val LANGUAGE_NOT_SUPPORTED = 12
        const val LANGUAGE_UNAVAILABLE = 13
        const val CANNOT_CHECK_SUPPORT = 14
        val ON_DEVICE_GIVE_UP = setOf(
            SERVER_DISCONNECTED, LANGUAGE_NOT_SUPPORTED, LANGUAGE_UNAVAILABLE, CANNOT_CHECK_SUPPORT,
            SpeechRecognizer.ERROR_CLIENT,
        )
    }
}

/**
 * Keeps Google's listening cue quiet without silencing the phone.
 *
 * The cue plays on the media stream as listening starts and stops. This mutes
 * that stream for just those moments, and only when nothing is playing - so
 * music is never cut, and notifications, calls and alarms, which have streams
 * of their own, are never affected. It never mutes for longer than
 * [LONGEST_MS], and a mute left behind by the app being killed is undone the
 * next time it starts. If you had muted media yourself, it is left alone.
 */
internal class CueGuard(context: Context) {

    private val audio = context.getSystemService(Context.AUDIO_SERVICE) as? AudioManager
    private val prefs = context.applicationContext.getSharedPreferences("cues", Context.MODE_PRIVATE)
    private val main = Handler(Looper.getMainLooper())
    private val restoreNow = Runnable { restore() }
    private var muted = false

    init {
        // Killed while muted last time: put the sound back.
        if (prefs.getBoolean(KEY, false)) {
            runCatching { audio?.adjustStreamVolume(AudioManager.STREAM_MUSIC, AudioManager.ADJUST_UNMUTE, 0) }
            prefs.edit { putBoolean(KEY, false) }
        }
    }

    @Synchronized
    fun hush() {
        val am = audio ?: return
        main.removeCallbacks(restoreNow)
        main.postDelayed(restoreNow, LONGEST_MS)
        if (muted) return
        if (am.isMusicActive || am.isStreamMute(AudioManager.STREAM_MUSIC)) return
        try {
            am.adjustStreamVolume(AudioManager.STREAM_MUSIC, AudioManager.ADJUST_MUTE, 0)
            muted = true
            prefs.edit { putBoolean(KEY, true) }
        } catch (e: Exception) {
            // Refused on this device; the cue stays audible.
        }
    }

    /** Unmute shortly: the cue is still finishing as the callback arrives. */
    @Synchronized
    fun restoreSoon() {
        if (!muted) return
        main.removeCallbacks(restoreNow)
        main.postDelayed(restoreNow, AFTER_CUE_MS)
    }

    @Synchronized
    fun restore() {
        main.removeCallbacks(restoreNow)
        if (!muted) return
        muted = false
        runCatching { audio?.adjustStreamVolume(AudioManager.STREAM_MUSIC, AudioManager.ADJUST_UNMUTE, 0) }
        prefs.edit { putBoolean(KEY, false) }
    }

    private companion object {
        const val KEY = "media_muted_for_cue"
        const val AFTER_CUE_MS = 450L
        const val LONGEST_MS = 3_000L
    }
}
