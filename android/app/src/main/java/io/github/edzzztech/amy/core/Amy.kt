package io.github.edzzztech.amy.core

import android.annotation.SuppressLint
import android.content.Context
import android.net.Uri
import io.github.edzzztech.amy.automation.Commands
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
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

    private val web = WebSearch()

    /** Model installs run apart from [brain], so she can still talk meanwhile. */
    private val installs = CoroutineScope(SupervisorJob() + Dispatchers.IO)

    /** Progress of a model being installed, for the screen; null when none is. */
    private val _installing = MutableStateFlow<String?>(null)
    val installing: StateFlow<String?> = _installing.asStateFlow()

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

        // The web, for what a model on the phone cannot know: asked outright
        // ("google ...", "look up ..."), or a question about news, prices,
        // scores or the weather.
        val asked = WebSearch.asked(clean)
        if (asked != null || WebSearch.needsFreshAnswer(clean)) {
            if (answerFromWeb(asked ?: clean, spoken, explicit = asked != null)) return
        }
        converse(spoken) { withHistory(clean) }
    }

    /**
     * Search, then answer from what was found, naming the sites under the
     * reply. False when nothing came back and the question was not an explicit
     * search, so the model can still try from what it knows.
     */
    private suspend fun answerFromWeb(query: String, spoken: Boolean, explicit: Boolean): Boolean {
        AmyState.setReply("Searching the web")
        val found = web.lookUp(query)
        AmyState.setReply("")
        when (found) {
            is WebSearch.Found.Answer -> {
                actions.record("web", "Looked up: $query", detail = found.source)
                deliver(found.text, spoken, sources = listOf(found.source))
                return true
            }
            is WebSearch.Found.Results -> {
                actions.record("web", "Searched: $query")
                val sites = found.results.map { it.site }.distinct().take(3)
                if (!llm.ensureLoaded()) {
                    // No model to summarise with: the best result, as found.
                    val top = found.results.first()
                    deliver(top.snippet.ifBlank { top.title }, spoken, sources = listOf(top.site))
                    return true
                }
                stream(spoken, sites) { system ->
                    llm.generate(WebSearch.prompt(query, found.results, llm.promptRoom(system)), system)
                }
                return true
            }
            WebSearch.Found.Nothing -> if (explicit) {
                deliver("I searched, but found nothing useful for that.", spoken)
                return true
            }
            WebSearch.Found.Offline -> if (explicit) {
                deliver("I couldn't reach the web just now. Check the phone is online.", spoken)
                return true
            }
        }
        return false
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
     * Ask about a picture: a photo from the camera, or an attached image. The
     * picture appears in the conversation, and her answer under it. Needs a
     * model that can see (Gemma 3n); with any other she says so, rather than
     * guessing at a picture she cannot look at.
     */
    fun askAboutImage(image: ByteArray, question: String, spoken: Boolean, caption: String = "") {
        AmyState.setState(Listening.Thinking)
        scope.launch {
            conversations.append("you", caption, image = conversations.savePicture(image))
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

    /**
     * Load the model if need be, then speak and show the reply [source]
     * streams. [sources] are named under the reply on screen, not spoken.
     */
    private suspend fun stream(
        spoken: Boolean,
        sources: List<String> = emptyList(),
        source: (system: String) -> Flow<String>,
    ) {
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

        fun take(text: String) {
            if (text.isEmpty()) return
            whole.append(text)
            if (spoken) buffer.push(text).forEach { tts?.speak(it, turn) }
            AmyState.setReply(whole.toString())
        }

        // An empty reply is asked once more: a model that says nothing has
        // usually been handed its prompt in a format it does not expect, and
        // the engine tries another on the next request.
        var attempts = 0
        while (true) {
            val tag = SpeakerTag()
            try {
                source(system).collect { token ->
                    if (tts?.turn != turn) {
                        llm.cancel()            // interrupted: stop forwarding text
                        return@collect
                    }
                    take(tag.push(scrub(token)))
                }
                take(tag.flush())
            } catch (e: Throwable) {
                AmyState.setReply("")
                deliver("Something went wrong while thinking: ${e.message ?: "unknown error"}", false)
                return
            }
            if (whole.isNotBlank() || tts?.turn != turn || ++attempts > 1) break
        }
        if (spoken) buffer.flush().takeIf { it.isNotEmpty() }?.let { tts?.speak(it, turn) }

        val answered = whole.toString().trim()
        val reply = if (answered.isEmpty()) {
            "I couldn't come up with an answer to that. Try asking another way."
        } else answered + sourceLine(sources)
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

    /** "Sources: bbc.co.uk, wikipedia.org", shown under a reply from the web. */
    private fun sourceLine(sources: List<String>): String =
        if (sources.isEmpty()) "" else "\n\nSources: " + sources.joinToString(", ")

    private suspend fun deliver(reply: String, spoken: Boolean, sources: List<String> = emptyList()) {
        conversations.append("amy", reply + sourceLine(sources))
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

    /**
     * Install a model file picked on the phone - one downloaded in the
     * browser, or copied over USB - so no computer or adb is needed. It is
     * copied into the model folder under a temporary name and renamed once
     * whole, so a half-copied file is never loaded, then loaded straight away.
     */
    fun installModel(uri: Uri, name: String) {
        if (_installing.value != null) return
        _installing.value = "Installing $name"
        installs.launch {
            val problem = llm.install(uri, name) { percent ->
                _installing.value = "Installing $name: $percent%"
            }
            _installing.value = null
            if (problem != null) {
                AmyState.setProblem(problem)
                actions.record("model", "Install failed: $name", outcome = "failed", detail = problem)
                return@launch
            }
            actions.record("model", "Installed $name")
            scope.launch {
                llm.reload()
                val ready = llm.ensureLoaded()
                deliver(
                    if (!ready) llm.unavailableReason()
                    else if (llm.supportsVision) "$name is installed and loaded. I can see pictures now."
                    else "$name is installed and loaded.",
                    spoken = false,
                )
            }
        }
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
