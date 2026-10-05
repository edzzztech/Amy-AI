package io.github.edzzztech.amy.core

import android.content.Context
import io.github.edzzztech.amy.automation.Commands
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

/**
 * The brain, owned by neither the activity nor the service.
 *
 * Once she listens in the background the work has to outlive the UI, and two
 * copies of the model is not an option on a phone, so there is one of these.
 *
 * ## Threading
 *
 * Every piece of conversation state is touched by one worker thread, [brain].
 * Input arrives from the UI thread (typing), the main looper (speech) and the
 * network (the desktop link); funnelling it all through a single-threaded
 * dispatcher means turns are stored in order, nothing races on the
 * conversation file, and no disk I/O happens on the UI thread — which is
 * where jank comes from.
 */
object Amy {

    /** One thread for all conversation state. Order in, order out. */
    @Suppress("OPT_IN_USAGE")
    private val brain = Dispatchers.IO.limitedParallelism(1)
    private val scope = CoroutineScope(SupervisorJob() + brain)

    lateinit var actions: ActionLog
        private set
    lateinit var conversations: Conversations
        private set
    lateinit var commands: Commands
        private set
    lateinit var llm: MediaPipeLlm
        private set
    lateinit var desktop: DesktopLink
        private set

    var tts: Tts? = null
        private set

    /**
     * Told when she starts and stops talking, so the listener can stop hearing
     * her. Without that, her own voice comes back through the microphone inside
     * the follow-up window and is taken as a command — she talks to herself.
     */
    @Volatile
    var onTalkingChanged: ((Boolean) -> Unit)? = null

    @Volatile
    private var started = false

    /** Safe to call repeatedly from any component; only the first call does anything. */
    @Synchronized
    fun start(appContext: Context) {
        if (started) return
        val app = appContext.applicationContext
        actions = ActionLog(app)
        conversations = Conversations(app)
        commands = Commands(app, actions)
        llm = MediaPipeLlm(app)
        desktop = DesktopLink(app)
        tts = Tts(app).also { engine ->
            engine.onSpeakStart = {
                AmyState.setState(Listening.Speaking)
                onTalkingChanged?.invoke(true)
            }
            engine.onSpeakDone = {
                AmyState.setState(AmyState.settled())
                onTalkingChanged?.invoke(false)
            }
            scope.launch {
                if (engine.awaitReady()) engine.useDesktopVoice()
                else AmyState.setProblem("Text to speech is unavailable on this device.")
            }
        }
        started = true
        // Reading every saved conversation is disk work; not on the UI thread.
        scope.launch { AmyState.setHistory(conversations.list()) }
    }

    /** One path for typed, spoken and wake-word input, so nothing drifts apart. */
    fun submit(text: String, spoken: Boolean) {
        val clean = text.trim()
        if (clean.isEmpty()) return
        AmyState.setState(Listening.Thinking)
        scope.launch { handle(clean, spoken) }
    }

    private suspend fun handle(clean: String, spoken: Boolean) {
        conversations.append("you", clean)
        AmyState.setTurns(conversations.turns())
        actions.record("message", "${if (spoken) "Said" else "Typed"}: $clean")

        // Anything aimed at the computer goes there first, before the phone
        // tries to answer it itself.
        forDesktop(clean)?.let { onPc ->
            val problem = desktop.send(onPc)
            deliver(problem ?: "Sent that to your computer.", spoken)
            return
        }

        // Deterministic commands next: "open Spotify" should open Spotify every
        // time, not depend on a 1B model choosing a tool correctly. They touch
        // the accessibility service and start activities, so the main thread.
        val reply = withContext(Dispatchers.Main) { commands.handle(clean) }
        if (reply != null) {
            deliver(reply, spoken)
            return
        }
        converse(withHistory(clean), spoken)
    }

    /**
     * Submit where what the model sees and what the conversation shows differ —
     * an attached file, where the prompt carries the document but the transcript
     * only shows its name. Never routed through [Commands]: a file is data, and
     * matching its contents as instructions would let a document act.
     */
    fun submitRaw(shown: String, prompt: String) {
        AmyState.setState(Listening.Thinking)
        scope.launch {
            conversations.append("you", shown)
            AmyState.setTurns(conversations.turns())
            converse(prompt, spoken = false)
        }
    }

    private suspend fun converse(prompt: String, spoken: Boolean) {
        if (!llm.ensureLoaded()) {
            val message = if (llm.findModel() == null) {
                "I have no model yet. Put a .task model in ${llm.expectedPath()}."
            } else {
                "I found a model but couldn't load it. It may be the wrong format, " +
                    "or too large for this phone's memory."
            }
            deliver(message, spoken = false)
            return
        }

        // Captured now, so an interruption mid-reply can be recognised: once
        // the turn moves on, nothing more from this reply is spoken.
        val turn = tts?.turn ?: 0
        val buffer = SentenceBuffer()
        val whole = StringBuilder()
        try {
            llm.generate(prompt, system = SYSTEM_PROMPT).collect { token ->
                if (tts?.turn != turn) {
                    llm.cancel()            // interrupted: stop forwarding text
                    return@collect
                }
                val clean = scrub(token)
                if (clean.isEmpty()) return@collect
                whole.append(clean)
                if (spoken) buffer.push(clean).forEach { tts?.speak(it, turn) }
                AmyState.setReply(whole.toString())
            }
        } catch (e: Throwable) {
            AmyState.setReply("")
            deliver("Something went wrong while thinking: ${e.message ?: "unknown error"}", false)
            return
        }
        if (spoken) buffer.flush().takeIf { it.isNotEmpty() }?.let { tts?.speak(it, turn) }

        val reply = whole.toString().trim().ifEmpty { "I have no answer for that." }
        conversations.append("amy", reply)
        AmyState.setTurns(conversations.turns())
        AmyState.setReply("")
        AmyState.setHistory(conversations.list())
        // A spoken reply goes idle when the voice finishes; a typed one now.
        if (!spoken || tts?.isTalking != true) AmyState.setState(AmyState.settled())
    }

    /**
     * Strip a "on my PC" style prefix and return what is left, or null if the
     * request was never aimed at the computer. Requiring an explicit mention is
     * deliberate: silently sending "open Spotify" to the desktop when you meant
     * the phone in your hand would be maddening.
     */
    internal fun forDesktop(text: String): String? {
        val m = DESKTOP_PREFIX.find(text) ?: return null
        val rest = text.substring(m.range.last + 1).trim(' ', ',', ':')
        return rest.ifBlank { null }
    }

    /**
     * Remove control tokens the model sometimes emits verbatim. Spoken aloud,
     * "start of turn model" makes an assistant sound broken.
     */
    private fun scrub(text: String): String = text.replace(TURN_TOKENS, "")

    /**
     * Give the model the last few turns, so follow-ups like "and the other one?"
     * mean something. Kept short: every token of history is one less for the
     * answer, and on a phone prefill is most of the wait before she speaks.
     */
    private fun withHistory(prompt: String): String {
        val recent = conversations.turns()
            .dropLast(1)                       // the current message is the prompt
            .takeLast(HISTORY_TURNS)
        if (recent.isEmpty()) return prompt
        return buildString {
            recent.forEach { turn ->
                append(if (turn.role == "you") "User: " else "Amy: ")
                append(turn.text.take(HISTORY_CHARS))
                append('\n')
            }
            append("User: ")
            append(prompt)
        }
    }

    private suspend fun deliver(reply: String, spoken: Boolean) {
        conversations.append("amy", reply)
        AmyState.setTurns(conversations.turns())
        AmyState.setHistory(conversations.list())
        if (spoken) tts?.speak(reply) else AmyState.setState(AmyState.settled())
    }

    /**
     * Cut off any reply in progress, then switch. Clearing the screen first and
     * switching later let a reply that was still streaming finish, save into
     * the old conversation, and put its turns straight back on screen.
     */
    fun newConversation() {
        stopSpeaking()
        scope.launch {
            conversations.start()
            AmyState.setTurns(emptyList())
            AmyState.clear()
        }
    }

    fun openConversation(id: String) {
        scope.launch { conversations.load(id)?.let { AmyState.setTurns(it.turns) } }
    }

    /** Barge-in: stop talking now, and stop the reply that was still coming. */
    fun stopSpeaking() {
        tts?.stop()
        llm.cancel()
    }

    /** Load the model before it is first asked for, so the first reply is not slow. */
    fun warmUp() {
        scope.launch { llm.ensureLoaded() }
    }

    private const val HISTORY_TURNS = 6
    private const val HISTORY_CHARS = 300

    // Doubled backslash: in a Kotlin string "\b" is a backspace character, not
    // a word boundary, so the single-backslash version could never match.
    private val DESKTOP_PREFIX = Regex(
        "^(?:on|using|with) (?:my |the )?(?:pc|computer|desktop|laptop)\\b",
        RegexOption.IGNORE_CASE,
    )

    private val TURN_TOKENS =
        Regex("<\\s*/?\\s*(start_of_turn|end_of_turn|eos|bos|pad)\\s*>", RegexOption.IGNORE_CASE)

    const val SYSTEM_PROMPT =
        "You are Amy, a concise assistant running on the user's phone. " +
            "Answer in one or two short sentences unless asked for detail. " +
            "Never invent facts about the user's device, files or apps."
}
