package io.github.edzzztech.amy.core

import android.content.Context
import io.github.edzzztech.amy.automation.Commands
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.launch

/**
 * The brain, owned by neither the activity nor the service.
 *
 * It started in MainActivity, which was fine while you had to press a button.
 * Once she listens in the background the work has to outlive the UI, and two
 * copies of the model is not an option on a phone — so there is one of these,
 * created once, used by both.
 */
object Amy {

    private var context: Context? = null
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Default)

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

    private var started = false

    /** Safe to call repeatedly; only the first call does anything. */
    @Synchronized
    fun start(appContext: Context) {
        if (started) return
        val app = appContext.applicationContext
        context = app
        actions = ActionLog(app)
        conversations = Conversations(app)
        commands = Commands(app, actions)
        llm = MediaPipeLlm(app)
        desktop = DesktopLink(app)
        tts = Tts(app).also { engine ->
            engine.onSpeakStart = { AmyState.setState(Listening.Speaking) }
            engine.onSpeakDone = { AmyState.setState(Listening.Idle) }
            scope.launch {
                if (engine.awaitReady()) engine.useDesktopVoice()
                else AmyState.setProblem("Text to speech is unavailable on this device.")
            }
        }
        AmyState.setHistory(conversations.list())
        started = true
    }

    /** One path for typed, spoken and wake-word input, so nothing drifts apart. */
    fun submit(text: String, spoken: Boolean) {
        val clean = text.trim()
        if (clean.isEmpty()) return
        conversations.append("you", clean)
        AmyState.setTurns(conversations.turns())
        actions.record("message", "${if (spoken) "Said" else "Typed"}: $clean")
        AmyState.setState(Listening.Thinking)

        // Anything aimed at the computer goes there first, before the phone
        // tries to answer it itself.
        forDesktop(clean)?.let { onPc ->
            scope.launch {
                val problem = desktop.send(onPc)
                deliver(problem ?: "Sent that to your computer.", spoken)
            }
            return
        }

        // Deterministic commands next: "open Spotify" should open Spotify every
        // time, not depend on a 1B model choosing a tool correctly.
        commands.handle(clean)?.let { reply ->
            deliver(reply, spoken)
            return
        }
        scope.launch { converse(clean, spoken) }
    }

    private suspend fun converse(prompt: String, spoken: Boolean) {
        if (!llm.isLoaded) {
            val file = llm.findModel()
            if (file == null) {
                deliver("I have no model yet. Put a .task model in ${llm.expectedPath()}.", false)
                return
            }
            if (!llm.load(file.absolutePath)) {
                deliver("I found ${file.name} but couldn't load it.", false)
                return
            }
        }

        val buffer = SentenceBuffer()
        val whole = StringBuilder()
        try {
            llm.generate(withHistory(prompt), system = SYSTEM_PROMPT).collect { token ->
                val clean = scrub(token)
                if (clean.isEmpty()) return@collect
                whole.append(clean)
                if (spoken) buffer.push(clean).forEach { tts?.speak(it) }
                AmyState.setReply(whole.toString())
            }
        } catch (e: Throwable) {
            deliver("Something went wrong while thinking: ${e.message}", false)
            return
        }
        if (spoken) buffer.flush().takeIf { it.isNotEmpty() }?.let { tts?.speak(it) }

        val reply = whole.toString().trim().ifEmpty { "I have no answer for that." }
        conversations.append("amy", reply)
        AmyState.setTurns(conversations.turns())
        AmyState.setReply("")
        AmyState.setHistory(conversations.list())
        if (!spoken) AmyState.setState(Listening.Idle)
    }

    /**
     * Submit where what the model sees and what the conversation shows differ —
     * an attached file, say, where the prompt carries the whole document but the
     * transcript should only show its name.
     */
    fun submitRaw(shown: String, prompt: String) {
        conversations.append("you", shown)
        AmyState.setTurns(conversations.turns())
        AmyState.setState(Listening.Thinking)
        scope.launch { converse(prompt, spoken = false) }
    }


    /**
     * Strip a "on my PC" style prefix and return what is left, or null if the
     * request was never aimed at the computer. Requiring an explicit mention
     * is deliberate: silently sending "open Spotify" to the desktop when you
     * meant the phone in your hand would be maddening.
     */
    private fun forDesktop(text: String): String? {
        val m = DESKTOP_PREFIX.find(text) ?: return null
        val rest = text.substring(m.range.last + 1).trim(' ', ',', ':')
        return rest.ifBlank { null }
    }

    /**
     * Remove control tokens the model sometimes emits verbatim. Spoken aloud,
     * "start of turn model" is the kind of thing that makes an assistant sound
     * broken, so they are stripped before anything reaches the voice.
     */
    private fun scrub(text: String): String =
        text.replace(TURN_TOKENS, "")

    /**
     * Give the model the last few turns, so follow-ups like "and the other one?"
     * mean something. Kept short deliberately: every token of history is one
     * less of context and one more to prefill, and on a phone prefill is most
     * of the wait before she starts talking.
     */
    private fun withHistory(prompt: String): String {
        val recent = conversations.turns()
            .dropLast(1)                       // the current message is the prompt
            .takeLast(HISTORY_TURNS)
        if (recent.isEmpty()) return prompt
        return buildString {
            recent.forEach { turn ->
                append(if (turn.role == "you") "User: " else "Amy: ")
                append(turn.text.take(300))
                append('\n')
            }
            append("User: ")
            append(prompt)
        }
    }

    private fun deliver(reply: String, spoken: Boolean) {
        conversations.append("amy", reply)
        AmyState.setTurns(conversations.turns())
        AmyState.setHistory(conversations.list())
        if (spoken) tts?.speak(reply) else AmyState.setState(Listening.Idle)
    }

    fun newConversation() {
        conversations.start()
        AmyState.setTurns(emptyList())
        AmyState.clear()
    }

    fun openConversation(id: String) {
        conversations.load(id)?.let { AmyState.setTurns(it.turns) }
    }

    fun stopSpeaking() = tts?.stop()

    /** Load the model before it is first asked for, so the first reply is not slow. */
    fun warmUp() {
        scope.launch {
            if (llm.isLoaded) return@launch
            llm.findModel()?.let { llm.load(it.absolutePath) }
        }
    }

    private const val HISTORY_TURNS = 6

    private val DESKTOP_PREFIX = Regex(
        "^(?:on|using|with) (?:my |the )?(?:pc|computer|desktop|laptop)\b",
        RegexOption.IGNORE_CASE,
    )

    private val TURN_TOKENS =
        Regex("<\\s*/?\\s*(start_of_turn|end_of_turn|eos|bos|pad)\\s*>", RegexOption.IGNORE_CASE)

    const val SYSTEM_PROMPT =
        "You are Amy, a concise assistant running on the user's phone. " +
            "Answer in one or two short sentences unless asked for detail. " +
            "Never invent facts about the user's device, files or apps."
}
