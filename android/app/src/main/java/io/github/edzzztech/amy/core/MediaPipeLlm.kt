package io.github.edzzztech.amy.core

import android.content.Context
import com.google.mediapipe.tasks.genai.llminference.LlmInference
import kotlinx.coroutines.channels.awaitClose
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.callbackFlow
import kotlinx.coroutines.sync.Semaphore
import kotlinx.coroutines.sync.withPermit
import kotlinx.coroutines.withTimeoutOrNull
import java.io.File
import java.util.concurrent.atomic.AtomicBoolean

/**
 * On-device inference through MediaPipe's LLM Inference task.
 *
 * Chosen over hand-rolling llama.cpp through JNI: that means an NDK toolchain,
 * a C++ build per ABI, and maintaining the bindings ourselves. MediaPipe is a
 * plain Maven dependency that already does GPU delegation and streaming. If we
 * later need GGUF or a model MediaPipe will not take, [LlmEngine] is the seam.
 *
 * ## Concurrency
 *
 * The native engine does one thing at a time and throws if asked to overlap.
 * That bites in two ways that are easy to miss:
 *
 *  - **A cancelled generation is still running.** Cancelling the Kotlin flow
 *    does not stop the native call; it finishes in the background. Starting the
 *    next one before then throws "previous invocation still processing".
 *  - **Loading while generating** frees memory the running call is using.
 *
 * So a single permit guards the engine, and a generation releases it only when
 * the native side reports done — not when the collector stops listening.
 */
class MediaPipeLlm(context: Context) : LlmEngine {

    // Application context only: this object outlives every activity.
    private val app = context.applicationContext

    @Volatile private var engine: LlmInference? = null
    @Volatile private var loadedPath: String? = null
    @Volatile private var cancelled = false

    /** Total tokens the loaded engine accepts — input and output together. */
    @Volatile var maxTokens: Int = 0
        private set

    private val busy = Semaphore(1)

    override val isLoaded: Boolean
        get() = engine != null

    /** The folder a pushed model is expected to live in. */
    fun modelDir(): File? = app.getExternalFilesDir(null)

    /**
     * Any .task bundle in the app's files directory, largest first.
     *
     * Not a fixed filename: published models are called things like
     * Gemma3-1B-IT_multi-prefill-seq_q4_ekv2048.task, and making someone rename
     * a 555 MB file is a pointless step to get wrong.
     */
    fun findModel(): File? =
        modelDir()
            ?.listFiles { f -> f.isFile && f.extension.lowercase() == "task" && f.length() > 0 }
            ?.maxByOrNull { it.length() }

    /** Where to tell the user to put it. */
    fun expectedPath(): String = modelDir()?.absolutePath ?: "the app's files directory"

    /**
     * The model file that last failed to load, by path, size and date, and
     * when. Every attempt tries six configurations and can take many seconds,
     * so a file that cannot load is not retried on every message: only once
     * it is replaced, or after a while in case memory was the problem.
     */
    @Volatile private var failedKey: String? = null
    @Volatile private var failedAt = 0L

    private fun keyOf(file: File) = "${file.absolutePath}|${file.length()}|${file.lastModified()}"

    /** Load the newest model if nothing is loaded yet. Safe to call repeatedly. */
    suspend fun ensureLoaded(): Boolean {
        if (engine != null) return true
        val file = findModel() ?: return false
        val key = keyOf(file)
        if (key == failedKey && System.currentTimeMillis() - failedAt < RETRY_FAILED_MS) {
            return false
        }
        val ok = load(file.absolutePath)
        if (ok) {
            failedKey = null
        } else {
            failedKey = key
            failedAt = System.currentTimeMillis()
        }
        return ok
    }

    override suspend fun load(modelPath: String): Boolean = busy.withPermit {
        // A second caller that queued behind the first finds the work done.
        if (engine != null && loadedPath == modelPath) return@withPermit true

        closeEngine()
        val file = File(modelPath)
        if (!file.exists() || file.length() == 0L) return@withPermit false

        // Largest context first, falling back if the model or device refuses
        // it, and GPU before CPU at each size: GPU is several times faster on a
        // phone but not every device or model supports it.
        for (tokens in TOKEN_BUDGETS) {
            for (gpu in listOf(true, false)) {
                val created = try {
                    LlmInference.createFromOptions(app, options(file, tokens, gpu))
                } catch (e: Throwable) {
                    null
                }
                if (created != null) {
                    engine = created
                    loadedPath = modelPath
                    maxTokens = tokens
                    return@withPermit true
                }
            }
        }
        false
    }

    /**
     * Streams the reply.
     *
     * MediaPipe's progress listener delivers *deltas* — each callback is the
     * next fragment. An earlier version tried to "normalise" them by trimming a
     * prefix off each chunk, which ate characters and produced fluent nonsense.
     * Deltas are appended as they arrive, and nothing else.
     */
    override fun generate(prompt: String, system: String?, maxTokens: Int): Flow<String> =
        callbackFlow {
            // Wait for any earlier run to finish natively, cancelled ones
            // included. A wedged engine must not hang her forever.
            //
            // The flag is set inside the block rather than taken from its
            // return value: withTimeoutOrNull may discard the result even after
            // acquire() succeeded, which would leak the permit and wedge the
            // engine for good. A side effect with no suspension point between
            // it and acquire() records the truth either way.
            var acquired = false
            withTimeoutOrNull(WAIT_FOR_ENGINE_MS) {
                busy.acquire()
                acquired = true
            }
            if (!acquired) {
                close(IllegalStateException("The model is still busy with the last request."))
                return@callbackFlow
            }

            val llm = engine
            if (llm == null) {
                busy.release()
                close()
                return@callbackFlow
            }

            cancelled = false
            // Touched from the native callback thread and from the catch below.
            val released = AtomicBoolean(false)
            fun releaseOnce() {
                if (released.compareAndSet(false, true)) busy.release()
            }

            try {
                llm.generateResponseAsync(chatPrompt(prompt, system)) { partial: String, done: Boolean ->
                    if (!cancelled && partial.isNotEmpty()) trySend(partial)
                    if (done) {
                        releaseOnce()
                        close()
                    }
                }
            } catch (e: Throwable) {
                releaseOnce()
                close(e)
            }

            // The collector going away only stops us forwarding text; the
            // permit is released by the native done callback above.
            awaitClose { cancelled = true }
        }

    /** Stop forwarding text. The native call winds down on its own. */
    override fun cancel() {
        cancelled = true
    }

    override fun unload() {
        closeEngine()
    }

    private fun closeEngine() {
        try {
            engine?.close()
        } catch (e: Throwable) {
            // Closing an engine that already died is not worth reporting.
        }
        engine = null
        loadedPath = null
        maxTokens = 0
    }

    /**
     * Wrap the prompt in Gemma's turn markers, and keep it inside the budget.
     *
     * Instruction-tuned Gemma is trained on this exact shape; handed bare text
     * it continues rather than answers. And MediaPipe's token limit covers the
     * prompt *and* the reply, so an oversized prompt leaves nothing to answer
     * with. Overflow keeps the end, where the question and the most recent
     * turns are.
     */
    private fun chatPrompt(prompt: String, system: String?): String {
        val sys = system?.trim().orEmpty()
        val room = promptRoom(sys)
        val body = prompt.trim().let { if (it.length > room) "…" + it.takeLast(room) else it }
        return buildString {
            append("<start_of_turn>user\n")
            if (sys.isNotEmpty()) append(sys).append("\n\n")
            append(body)
            append("<end_of_turn>\n<start_of_turn>model\n")
        }
    }

    /**
     * Characters of prompt that fit beside [system] in the loaded engine's
     * budget. Anything longer is cut from the front, so a caller with a long
     * document sizes it with this first and keeps the part that matters.
     */
    fun promptRoom(system: String?): Int {
        val budgetChars = ((maxTokens.takeIf { it > 0 } ?: TOKEN_BUDGETS.last()) - REPLY_RESERVE)
            .coerceAtLeast(128) * CHARS_PER_TOKEN
        return (budgetChars - (system?.trim()?.length ?: 0)).coerceAtLeast(400)
    }

    private fun options(file: File, tokens: Int, gpu: Boolean) =
        LlmInference.LlmInferenceOptions.builder()
            .setModelPath(file.absolutePath)
            .setMaxTokens(tokens)
            .setPreferredBackend(if (gpu) LlmInference.Backend.GPU else LlmInference.Backend.CPU)
            .build()

    companion object {
        /**
         * Context sizes to try, largest first. Every common Gemma 3 1B bundle
         * supports at least 1280; 512 is the floor that anything will load at.
         */
        private val TOKEN_BUDGETS = listOf(2048, 1280, 512)

        /** Tokens held back for her answer when sizing the prompt. */
        private const val REPLY_RESERVE = 320

        /** Conservative English estimate; under-estimating would overflow. */
        private const val CHARS_PER_TOKEN = 3

        private const val WAIT_FOR_ENGINE_MS = 45_000L

        private const val RETRY_FAILED_MS = 10 * 60_000L
    }
}
