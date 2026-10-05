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
