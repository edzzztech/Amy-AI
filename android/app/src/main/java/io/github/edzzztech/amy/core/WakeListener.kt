package io.github.edzzztech.amy.core

import android.content.Context
import android.os.Handler
import android.os.Looper

/**
 * Always listening, in the desktop's sense: the microphone is open, but she
 * only acts when she hears her name — or when you are already mid-conversation.
 *
 * Android's recogniser stops after each utterance, so "continuous" means
 * restarting it in a loop. That is the standard approach and it is why there
 * is a backoff here: a recogniser that fails instantly and is restarted
 * instantly will spin a core flat.
 *
 * A conversation window follows every exchange, exactly as on the desktop, so
 * follow-ups do not each need the wake word.
 */
class WakeListener(
    context: Context,
    private val onCommand: (String) -> Unit,
) {

    var wakeWord: String = "amy"
    var enabled: Boolean = false
        private set

    /** Seconds after a reply during which the wake word is not needed. */
    var conversationWindowMs: Long = 20_000

    private val stt = Stt(context)
    private val main = Handler(Looper.getMainLooper())
    private var lastExchange = 0L
    private var consecutiveFailures = 0
    private var stopping = false

    init {
        stt.onReadyForSpeech = {
            AmyState.setState(Listening.Listening)
            consecutiveFailures = 0
        }
        stt.onPartial = { AmyState.setHeard(it) }
        stt.onFinal = { text ->
            AmyState.setHeard("")
            handle(text)
            // Short gap only: the recogniser is reused, so resuming is silent.
            restartSoon(250)
        }
        stt.onError = {
            // Silence and no-match are the normal case when nobody is talking,
            // so they are not surfaced — only a persistent fault is.
            consecutiveFailures++
            AmyState.setState(Listening.Idle)
            if (consecutiveFailures >= 8) {
                AmyState.setProblem("The recogniser keeps failing. Check the microphone permission.")
            }
            restartSoon(backoff())
        }
    }

    fun start() {
        if (!stt.isAvailable) {
            AmyState.setProblem("No speech recogniser on this device.")
            return
        }
        enabled = true
        stopping = false
        consecutiveFailures = 0
        restartSoon(0)
    }

    fun stop() {
        enabled = false
        stopping = true
        main.removeCallbacksAndMessages(null)
        // pause(), not stop(): keeping the recogniser alive means waking her
        // again does not replay the device's start cue.
        stt.pause()
        AmyState.setState(Listening.Idle)
    }

    /** Release the recogniser for good. Only when the service is going away. */
    fun release() {
        enabled = false
        stopping = true
        main.removeCallbacksAndMessages(null)
        stt.stop()
    }

    /** Called after she finishes replying, to open the follow-up window. */
    fun markExchange() {
        lastExchange = System.currentTimeMillis()
    }

    private fun inConversation(): Boolean =
        System.currentTimeMillis() - lastExchange < conversationWindowMs

    private fun handle(heardRaw: String) {
        val heard = heardRaw.trim()
        if (heard.isEmpty()) return

        // Spoken switches, checked before anything else so you can always
        // turn her off by voice.
        val lower = heard.lowercase()
        if (STOP.containsMatchIn(lower)) {
            stop()
            onCommand(INTERNAL_STOPPED)
            return
        }

        val spokenTo = lower.contains(wakeWord.lowercase())
        if (!spokenTo && !inConversation()) return      // not for her

        val command = if (spokenTo) stripWake(heard) else heard
        if (command.isBlank()) {
            // Just her name: acknowledge and keep the window open.
            lastExchange = System.currentTimeMillis()
            onCommand(INTERNAL_NAME_ONLY)
            return
        }
        lastExchange = System.currentTimeMillis()
        onCommand(command)
    }

    /** Remove the wake word and any filler immediately around it. */
    private fun stripWake(text: String): String {
        val idx = text.lowercase().indexOf(wakeWord.lowercase())
        if (idx < 0) return text
        return text.substring(idx + wakeWord.length)
            .trimStart(' ', ',', '.', '?', '!', ':', ';')
            .trim()
    }

    private fun backoff(): Long =
        when {
            consecutiveFailures <= 2 -> 500L
            consecutiveFailures <= 5 -> 1_500L
            else -> 4_000L
        }

    private fun restartSoon(delayMs: Long) {
        if (!enabled || stopping) return
        main.removeCallbacksAndMessages(null)
        main.postDelayed({
            if (enabled && !stopping) stt.listen()
        }, delayMs)
    }

    companion object {
        /** Sentinels so the caller can respond without parsing strings. */
        const val INTERNAL_NAME_ONLY = "\u0000name"
        const val INTERNAL_STOPPED = "\u0000stopped"

        private val STOP = Regex(
            "stop listening|stop the mic|mute yourself|go to sleep|that's enough"
        )
    }
}
