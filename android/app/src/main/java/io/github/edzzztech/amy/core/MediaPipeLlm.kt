package io.github.edzzztech.amy.core

import android.content.Context
import com.google.mediapipe.tasks.genai.llminference.LlmInference
import kotlinx.coroutines.channels.awaitClose
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.callbackFlow
import java.io.File

/**
 * On-device inference through MediaPipe's LLM Inference task.
 *
 * Chosen over hand-rolling llama.cpp through JNI: that means an NDK toolchain,
 * a C++ build per ABI, and maintaining the bindings ourselves. MediaPipe is a
 * plain Maven dependency that already does GPU delegation and streaming, which
 * gets a working assistant far sooner. If we later need GGUF support or a
 * model MediaPipe will not take, the [LlmEngine] interface is the seam that
 * lets llama.cpp drop in without touching anything above it.
 *
 * Models are .task bundles — Gemma 2B/3 1B, Phi-2, Falcon 1B and similar.
 * They are far too large to ship inside the APK, so the file is pushed to the
 * device and loaded from storage.
 */
class MediaPipeLlm(private val context: Context) : LlmEngine {

    private var engine: LlmInference? = null
    private var cancelled = false

    override val isLoaded: Boolean
        get() = engine != null

    /** Where a pushed model is expected to live. */
    fun defaultModelFile(): File = File(context.getExternalFilesDir(null), "model.task")

    override suspend fun load(modelPath: String): Boolean {
        unload()
        val file = File(modelPath)
        if (!file.exists() || file.length() == 0L) return false
        return try {
            val options = LlmInference.LlmInferenceOptions.builder()
                .setModelPath(file.absolutePath)
                .setMaxTokens(MAX_TOKENS)
                .build()
            engine = LlmInference.createFromOptions(context, options)
            true
        } catch (e: Throwable) {
            // An unsupported model or an out-of-memory kill are both ordinary
            // outcomes on a phone; report them as failure rather than crashing.
            engine = null
            false
        }
    }

    /**
     * Streams partial results. MediaPipe hands back cumulative text in some
     * versions and deltas in others, so this normalises to deltas — emitting
     * only what is new since the last callback.
     */
    override fun generate(prompt: String, system: String?, maxTokens: Int): Flow<String> =
        callbackFlow {
            val llm = engine
            if (llm == null) {
                close()
                return@callbackFlow
            }
            cancelled = false
            var emitted = 0

            val full = if (system.isNullOrBlank()) prompt else "$system\n\n$prompt"

            try {
                llm.generateResponseAsync(full) { partial: String, done: Boolean ->
                    if (cancelled) {
                        close()
                        return@generateResponseAsync
                    }
                    val delta = if (partial.length >= emitted && partial.startsWith(
                            partial.take(emitted)
                        )
                    ) {
                        partial.substring(emitted)
                    } else {
                        partial                       // already a delta
                    }
                    emitted = maxOf(emitted, partial.length)
                    if (delta.isNotEmpty()) trySend(delta)
                    if (done) close()
                }
            } catch (e: Throwable) {
                close(e)
            }

            awaitClose { cancelled = true }
        }

    override fun cancel() {
        cancelled = true
    }

    override fun unload() {
        try {
            engine?.close()
        } catch (e: Throwable) {
            // Closing an engine that already died is not worth reporting.
        }
        engine = null
    }

    private companion object {
        const val MAX_TOKENS = 1024
    }
}
