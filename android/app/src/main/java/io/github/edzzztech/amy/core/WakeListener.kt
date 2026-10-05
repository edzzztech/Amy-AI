package io.github.edzzztech.amy.core

import android.content.Context
import android.os.Handler
import android.os.Looper
import android.speech.SpeechRecognizer

/**
 * Always listening, in the desktop's sense: the microphone is open, but she
 * only acts when addressed by name — or when you are already mid-conversation.
 *
 * ## The rules, and the bug behind each one
 *
 *  - **Deaf while she talks.** Her own voice comes back through the
 *    microphone, and every reply opens the follow-up window, so without this
 *    she hears herself and answers herself, forever. [pauseForSpeech] and
 *    [resumeAfterSpeech] are driven by the voice starting and stopping.
 *    Interrupting by voice is therefore not possible on the phone — tap to
 *    interrupt — because Android's recogniser exposes no echo cancellation.
 *  - **The follow-up window opens when she stops speaking**, not when you
 *    finished. Timed from your command, a twenty-second answer used up the
 *    whole twenty-second window before she had finished saying it.
 *  - **A watchdog.** A reused recogniser occasionally stops calling back
 *    altogether. Without a timeout she would simply never hear anything again.
 *  - **Backoff.** Android's recogniser stops after every utterance, so
 *    continuous means restarting it. One that fails instantly and is restarted
 *    instantly spins a core flat.
 *  - **Silence is not an error.** No-match and timeout are what happens when
 *    nobody is talking. Treating them as faults flickered the status every few
 *    seconds and eventually raised a false alarm.
 *
 * Everything here runs on the main thread; callbacks from other threads are
 * posted to it.
 */
class WakeListener(
    context: Context,
    private val onCommand: (String) -> Unit,
) {

    /** Listening is wanted. False only when she has been put to sleep. */
    var enabled: Boolean = false
        private set

    /** Milliseconds after she stops talking during which her name is not needed. */
    var conversationWindowMs: Long = 20_000

    private val stt = Stt(context)
    private val main = Handler(Looper.getMainLooper())

    /** When the follow-up window last opened. */
    private var windowOpenedAt = 0L
    private var talking = false
    private var faults = 0

    private val watchdog = Runnable {
        // No result, no error, nothing: the recogniser has wedged. Replace it.
        if (enabled && !talking && stt.isListening) {
            stt.reset()
            scheduleListen(RESTART_MS)
        }
    }

    init {
        stt.onReadyForSpeech = {
            faults = 0
            AmyState.setProblem(null)
            if (!talking) AmyState.setState(Listening.Listening)
        }
        stt.onPartial = { if (!talking) AmyState.setHeard(it) }
        stt.onFinal = { text ->
            main.removeCallbacks(watchdog)
            AmyState.setHeard("")
            if (!talking) handle(text)
            scheduleListen(RESTART_MS)
        }
        stt.onError = { code, description ->
            main.removeCallbacks(watchdog)
            onRecognitionError(code, description)
        }
    }

    // --- lifecycle -----------------------------------------------------------

    fun start() {
        if (!stt.isAvailable) {
            AmyState.setProblem("No speech recogniser on this device.")
            return
        }
        enabled = true
        faults = 0
        AmyState.setAsleep(false)
        if (!talking) AmyState.setState(Listening.Idle)
        if (!talking) scheduleListen(0)
    }

    /** Put her to sleep. The recogniser is kept, so waking makes no sound. */
    fun stop() {
        enabled = false
        main.removeCallbacksAndMessages(null)
        stt.pause()
        AmyState.setHeard("")
        AmyState.setAsleep(true)
        AmyState.setState(Listening.Muted)
    }

    /** Release the recogniser for good. Only when the service is going away. */
    fun release() {
        enabled = false
        main.removeCallbacksAndMessages(null)
        stt.stop()
    }

    // --- her own voice -------------------------------------------------------

    /** She has started talking: stop hearing, so she cannot hear herself. */
    fun pauseForSpeech() = main.post {
        talking = true
        main.removeCallbacksAndMessages(null)
        stt.pause()
        AmyState.setHeard("")
    }

    /** She has finished: open the follow-up window and listen again. */
    fun resumeAfterSpeech() = main.post {
        talking = false
        windowOpenedAt = System.currentTimeMillis()
        // A short pause lets the end of her voice die away before listening,
        // or the recogniser catches the tail of her last word.
        scheduleListen(AFTER_SPEECH_MS)
    }

    // --- recognition ---------------------------------------------------------

    private fun inConversation(): Boolean =
        System.currentTimeMillis() - windowOpenedAt < conversationWindowMs

    private fun handle(heardRaw: String) {
        val heard = heardRaw.trim()
        if (heard.isEmpty()) return

        val addressed = WakeWords.isAddressed(heard)
        if (!addressed && !inConversation()) return          // not for her

        // Spoken switches only when she is being spoken to, so a television
        // saying "stop listening" cannot put her to sleep.
        val lower = heard.lowercase()
        if (STOP.containsMatchIn(lower)) {
            stop()
            onCommand(INTERNAL_STOPPED)
            return
        }

        val command = if (addressed) WakeWords.stripWake(heard) else heard
        if (command.isBlank()) {
            onCommand(INTERNAL_NAME_ONLY)
            return
        }
        onCommand(command)
    }

    private fun onRecognitionError(code: Int, description: String) {
        when (code) {
            // Nobody spoke, or nothing intelligible. The normal case: carry on.
            SpeechRecognizer.ERROR_NO_MATCH,
            SpeechRecognizer.ERROR_SPEECH_TIMEOUT -> {
                faults = 0
                scheduleListen(RESTART_MS)
            }

            SpeechRecognizer.ERROR_INSUFFICIENT_PERMISSIONS -> {
                AmyState.setProblem("Amy needs the microphone permission to listen.")
                stop()
            }

            // A wedged recogniser: throw it away and build a fresh one.
            SpeechRecognizer.ERROR_RECOGNIZER_BUSY,
            SpeechRecognizer.ERROR_CLIENT -> {
                faults++
                stt.reset()
                scheduleListen(backoff())
            }

            else -> {
                faults++
                if (faults >= FAULTS_BEFORE_REPORTING) AmyState.setProblem(description)
                scheduleListen(backoff())
            }
        }
    }

    private fun backoff(): Long = when {
        faults <= 2 -> 500L
        faults <= 5 -> 1_500L
        else -> 4_000L
    }

    private fun scheduleListen(delayMs: Long) {
        if (!enabled || talking) return
        main.removeCallbacksAndMessages(null)
        main.postDelayed({
            if (enabled && !talking) {
                stt.listen()
                main.postDelayed(watchdog, WATCHDOG_MS)
            }
        }, delayMs)
    }

    companion object {
        /** Sentinels so the caller can respond without parsing strings. */
        const val INTERNAL_NAME_ONLY = "\u0000name"
        const val INTERNAL_STOPPED = "\u0000stopped"

        private const val RESTART_MS = 250L
        private const val AFTER_SPEECH_MS = 450L
        private const val WATCHDOG_MS = 20_000L
        private const val FAULTS_BEFORE_REPORTING = 6

        // Deliberately narrow. "That's enough" used to be here and would mute
        // her in the middle of an ordinary conversation.
        private val STOP = Regex("stop listening|stop the mic|mute yourself|go to sleep")
    }
}
