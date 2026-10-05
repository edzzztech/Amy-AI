package io.github.edzzztech.amy.core

import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow

/**
 * What she is doing, shared between the service that does the work and the UI
 * that draws it. Mirrors the desktop's orb states so both front ends describe
 * the same machine.
 */
enum class Listening { Idle, Listening, Thinking, Speaking, Muted }

object AmyState {

    private val _state = MutableStateFlow(Listening.Idle)
    val state: StateFlow<Listening> = _state.asStateFlow()

    /**
     * Whether she has been put to sleep, held separately from [state].
     *
     * Inferring it from the status does not work: being told to sleep, she says
     * "I'll stop listening", the status passes through Speaking, and when the
     * sentence ends the voice reported ready — awake on screen, asleep in fact.
     */
    private val _asleep = MutableStateFlow(false)
    val asleep: StateFlow<Boolean> = _asleep.asStateFlow()

    fun setAsleep(value: Boolean) {
        _asleep.value = value
    }

    /** The status to fall back to when she finishes talking or thinking. */
    fun settled(): Listening = if (_asleep.value) Listening.Muted else Listening.Idle

    /** The live transcript, updated as partial results arrive. */
    private val _heard = MutableStateFlow("")
    val heard: StateFlow<String> = _heard.asStateFlow()

    /** Her most recent reply. */
    private val _reply = MutableStateFlow("")
    val reply: StateFlow<String> = _reply.asStateFlow()

    /** Last thing that went wrong, shown rather than swallowed. */
    private val _problem = MutableStateFlow<String?>(null)
    val problem: StateFlow<String?> = _problem.asStateFlow()

    /** The conversation on screen. */
    private val _turns = MutableStateFlow<List<Turn>>(emptyList())
    val turns: StateFlow<List<Turn>> = _turns.asStateFlow()

    /** The drawer's list of past conversations. */
    private val _history = MutableStateFlow<List<Conversation>>(emptyList())
    val history: StateFlow<List<Conversation>> = _history.asStateFlow()

    fun setTurns(list: List<Turn>) {
        _turns.value = list
    }

    fun setHistory(list: List<Conversation>) {
        _history.value = list
    }

    /** Whether the floating orb is on screen, so the UI can show its state. */
    private val _overlayOn = MutableStateFlow(false)
    val overlayOn: StateFlow<Boolean> = _overlayOn.asStateFlow()

    fun setOverlay(on: Boolean) {
        _overlayOn.value = on
    }

    /** Live microphone level, 0..1, for the orb to react to. */
    private val _level = MutableStateFlow(0f)
    val level: StateFlow<Float> = _level.asStateFlow()

    fun setLevel(value: Float) {
        _level.value = value.coerceIn(0f, 1f)
    }

    fun setState(next: Listening) {
        _state.value = next
    }

    fun setHeard(text: String) {
        _heard.value = text
    }

    fun setReply(text: String) {
        _reply.value = text
    }

    fun setProblem(text: String?) {
        _problem.value = text
    }

    fun clear() {
        _heard.value = ""
        _reply.value = ""
        _problem.value = null
    }
}
