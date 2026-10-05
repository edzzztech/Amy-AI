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

    /** The folder a pushed model is expected to live in. */
    fun modelDir(): File? = context.getExternalFilesDir(null)

    /**
     * Any .task bundle in the app's files directory, largest first.
     *
     * Deliberately not a fixed filename: the published models are called things
     * like Gemma3-1B-IT_multi-prefill-seq_q4_ekv2048.task, and making someone
     * rename a 555 MB file to model.task is a pointless step to get wrong.
     */
    fun findModel(): File? =
        modelDir()
            ?.listFiles { f -> f.isFile && f.extension.lowercase() == "task" && f.length() > 0 }
            ?.maxByOrNull { it.length() }

    /** Where to tell the user to put it. */
    fun expectedPath(): String =
        modelDir()?.absolutePath ?: "the app's files directory"

    override suspend fun load(modelPath: String): Boolean {
        unload()
        val file = File(modelPath)
        if (!file.exists() || file.length() == 0L) return false
        return try {
            // GPU first: on a phone it is several times faster than CPU for
            // these models. Not every device or model supports it, so a failure
            // falls back rather than leaving her unable to answer at all.
            engine = try {
                LlmInference.createFromOptions(context, buildOptions(file, gpu = true))
            } catch (e: Throwable) {
                LlmInference.createFromOptions(context, buildOptions(file, gpu = false))
            }
            true
        } catch (e: Throwable) {
            // An unsupported model or an out-of-memory kill are both ordinary
            // outcomes on a phone; report them as failure rather than crashing.
            engine = null
            false
        }
    }

    /**
     * Streams the reply.
     *
     * MediaPipe's progress listener delivers *deltas* — each callback is the
     * next fragment, not the whole answer so far. An earlier version here tried
     * to "normalise" that by trimming a prefix off every chunk, which silently
     * ate the first characters of each one and produced fluent-looking
     * nonsense. Deltas are appended as they arrive, and nothing else.
     */
    override fun generate(prompt: String, system: String?, maxTokens: Int): Flow<String> =
        callbackFlow {
            val llm = engine
            if (llm == null) {
                close()
                return@callbackFlow
            }
            cancelled = false

            val full = chatPrompt(prompt, system)

            try {
                llm.generateResponseAsync(full) { partial: String, done: Boolean ->
                    if (cancelled) {
                        close()
                        return@generateResponseAsync
                    }
                    if (partial.isNotEmpty()) trySend(partial)
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

    /**
     * Wrap the prompt in Gemma's turn markers.
     *
     * Instruction-tuned Gemma models are trained on this exact shape. Handed
     * bare text they tend to continue it rather than answer it — rambling,
     * inventing a dialogue, or drifting off topic, which reads as the model
     * being stupid when it is really being mis-prompted.
     */
    private fun chatPrompt(prompt: String, system: String?): String = buildString {
        append("<start_of_turn>user\n")
        if (!system.isNullOrBlank()) {
            append(system.trim())
            append("\n\n")
        }
        append(prompt.trim())
        append("<end_of_turn>\n<start_of_turn>model\n")
    }

    private fun buildOptions(file: File, gpu: Boolean) =
        LlmInference.LlmInferenceOptions.builder()
            .setModelPath(file.absolutePath)
            // A smaller window is markedly faster to prefill and is plenty for
            // short spoken exchanges; long documents are truncated anyway.
            .setMaxTokens(MAX_TOKENS)
            .apply {
                if (gpu) setPreferredBackend(LlmInference.Backend.GPU)
                else setPreferredBackend(LlmInference.Backend.CPU)
            }
            .build()

    private companion object {
        const val MAX_TOKENS = 512
    }
}
