package io.github.edzzztech.amy.core

import android.annotation.SuppressLint
import android.content.Context
import io.github.edzzztech.amy.automation.Commands
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.time.LocalDateTime
import java.time.format.DateTimeFormatter
import java.util.Locale

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

    // Lint cannot tell these hold only the application context, which lives
    // as long as the process does and so cannot leak.
    lateinit var actions: ActionLog
        private set
    lateinit var conversations: Conversations
        private set
    @SuppressLint("StaticFieldLeak")
    lateinit var commands: Commands
        private set
    @SuppressLint("StaticFieldLeak")
    lateinit var llm: LocalModel
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
        llm = LocalModel(app)
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
        converse(spoken) { withHistory(clean) }
    }

    /**
     * Ask about an attached file. The transcript shows only its name; the
     * model sees the document, cut to fit what the loaded model can take.
     * Never routed through [Commands]: a file is data, and matching its
     * contents as instructions would let a document act.
     */
    fun askAboutFile(file: Attachments.Attached, question: String = "") {
        AmyState.setState(Listening.Thinking)
        scope.launch {
            conversations.append("you", "Attached " + file.name)
            AmyState.setTurns(conversations.turns())
            converse(spoken = false) { room -> Attachments.prompt(file, question, room) }
        }
    }

    /**
     * Ask about a picture: a photo from the camera, or an attached image.
     * Needs a model that can see (Gemma 3n); with any other she says so,
     * rather than guessing at a picture she cannot look at.
     */
    fun askAboutImage(image: ByteArray, shown: String, question: String, spoken: Boolean) {
        AmyState.setState(Listening.Thinking)
        scope.launch {
            conversations.append("you", shown)
            AmyState.setTurns(conversations.turns())
            if (!llm.ensureLoaded()) {
                deliver(llm.unavailableReason(), spoken = false)
                return@launch
            }
            if (!llm.supportsVision) {
                // Two different problems: the wrong model, or the right one on
                // a phone whose GPU its vision part could not start on.
                val why = if (llm.canSee()) {
                    "My model can see, but it needs this phone's graphics chip, which I " +
                        "couldn't use. I can still talk."
                } else {
                    "I can't see pictures with the model I have. A Gemma 3n model in " +
                        "${llm.expectedPath()} would let me."
                }
                deliver(why, spoken)
                return@launch
            }
            stream(spoken) { system -> llm.describe(image, question, system) }
        }
    }

    /**
     * Stream a reply to text. [prompt] is built only once the model is
     * loaded, from the room it actually has, so long input can be sized to
     * fit instead of being cut blindly.
     */
    private suspend fun converse(spoken: Boolean, prompt: (room: Int) -> String) =
        stream(spoken) { system -> llm.generate(prompt(llm.promptRoom(system)), system) }

    /** Load the model if need be, then speak and show the reply [source] streams. */
    private suspend fun stream(spoken: Boolean, source: (system: String) -> Flow<String>) {
        if (!llm.ensureLoaded()) {
            deliver(llm.unavailableReason(), spoken = false)
            return
        }

        // Captured now, so an interruption mid-reply can be recognised: once
        // the turn moves on, nothing more from this reply is spoken.
        val turn = tts?.turn ?: 0
        val buffer = SentenceBuffer()
        val whole = StringBuilder()
        val system = systemPrompt()
        try {
            source(system).collect { token ->
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
     * The standing instructions, with today's date and time. A model has no
     * clock; without this, "how many days until Friday?" is a guess.
     */
    private fun systemPrompt(): String =
        SYSTEM_PROMPT + " It is now " + LocalDateTime.now().format(PROMPT_CLOCK) + "."

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

    // English on purpose: the model reads it, and it was trained in English.
    private val PROMPT_CLOCK: DateTimeFormatter =
        DateTimeFormatter.ofPattern("EEEE d MMMM yyyy, HH:mm", Locale.ENGLISH)

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
