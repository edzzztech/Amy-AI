package io.github.edzzztech.amy.core

import android.content.Context
import android.os.Build
import com.google.ai.edge.litertlm.Backend
import com.google.ai.edge.litertlm.Capabilities
import com.google.ai.edge.litertlm.Content
import com.google.ai.edge.litertlm.Contents
import com.google.ai.edge.litertlm.Conversation
import com.google.ai.edge.litertlm.ConversationConfig
import com.google.ai.edge.litertlm.Engine
import com.google.ai.edge.litertlm.EngineConfig
import com.google.ai.edge.litertlm.Message
import com.google.ai.edge.litertlm.MessageCallback
import com.google.ai.edge.litertlm.SamplerConfig
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.channels.awaitClose
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.callbackFlow
import kotlinx.coroutines.sync.Semaphore
import kotlinx.coroutines.sync.withPermit
import kotlinx.coroutines.withContext
import kotlinx.coroutines.withTimeoutOrNull
import java.io.File
import java.util.concurrent.CancellationException
import java.util.concurrent.Executors
import java.util.concurrent.atomic.AtomicBoolean

/**
 * On-device inference through Google's LiteRT-LM, the successor to MediaPipe's
 * LLM Inference API, and the way she sees: Gemma 3n takes a picture along with
 * the question. Runs `.litertlm` files; text-only ones (Gemma 3 1B) work too,
 * and [supportsVision] says which kind is loaded.
 *
 * The library was checked before it came in: its only dependencies are Gson,
 * Kotlin reflection and coroutines, and the native code carries no logging
 * or telemetry endpoints - unlike MediaPipe's image support, which needs a
 * package that brings Google's data-transport libraries with it.
 *
 * ## Concurrency
 *
 * The same rule as [MediaPipeLlm]: one request at a time, and the permit is
 * held until the native side reports it has finished, not merely until the
 * collector stops listening, so a cancelled reply can never overlap the next.
 * Each request gets a fresh conversation (history is in the prompt, as with
 * MediaPipe), closed on a thread of its own once the engine's callback thread
 * is done with it - closing from inside that callback invites a deadlock.
 */
class LiteRtLlm(context: Context) : LlmEngine {

    private val app = context.applicationContext

    @Volatile private var engine: Engine? = null
    @Volatile private var loadedKey: String? = null
    @Volatile private var cancelled = false
    @Volatile private var tokens = 0

    @Volatile
    override var supportsVision: Boolean = false
        private set

    /** As in [MediaPipeLlm]: a file that failed to load is not retried every message. */
    @Volatile private var failedKey: String? = null
    @Volatile private var failedAt = 0L

    private val busy = Semaphore(1)

    /** The request in flight, for [cancel]. Guarded by [lock]. */
    private val lock = Any()
    private var current: Turn? = null

    private val closer = Executors.newSingleThreadExecutor { r ->
        Thread(r, "litertlm-close").apply { isDaemon = true }
    }

    private class Turn(val conversation: Conversation) {
        /** Set once the engine has called back done or error; after that it is never touched. */
        @Volatile var nativeDone = false
        val finished = AtomicBoolean(false)
    }

    override val fileExtension = "litertlm"

    override val isLoaded: Boolean
        get() = engine != null

    override fun findModel(): File? =
        app.getExternalFilesDir(null)
            ?.listFiles { f -> f.isFile && f.extension.lowercase() == fileExtension && f.length() > 0 }
            ?.maxByOrNull { it.length() }

    private fun keyOf(file: File) = "${file.absolutePath}|${file.length()}|${file.lastModified()}"

    override suspend fun ensureLoaded(): Boolean {
        if (engine != null) return true
        val file = findModel() ?: return false
        val key = keyOf(file)
        if (key == failedKey && System.currentTimeMillis() - failedAt < RETRY_FAILED_MS) return false
        val ok = load(file)
        if (ok) {
            failedKey = null
        } else {
            failedKey = key
            failedAt = System.currentTimeMillis()
        }
        return ok
    }

    /**
     * Load, trying the fastest setup first and falling back: GPU before CPU,
     * with the vision encoder before without (it needs the GPU, and a phone
     * without one should still get text), largest context first.
     */
    private suspend fun load(file: File): Boolean = busy.withPermit {
        withContext(Dispatchers.IO) {
            val key = keyOf(file)
            if (engine != null && loadedKey == key) return@withContext true
            closeEngine()
            val vision = visionIn(file)
            val setups = buildList {
                if (vision) {
                    add(Setup(gpu = true, vision = true))
                    add(Setup(gpu = false, vision = true))     // text on CPU, pictures on GPU
                }
                add(Setup(gpu = true, vision = false))
                add(Setup(gpu = false, vision = false))
            }
            for (budget in TOKEN_BUDGETS) {
                for (setup in setups) {
                    val created = start(file, setup, budget) ?: continue
                    engine = created
                    loadedKey = key
                    tokens = budget
                    supportsVision = setup.vision
                    return@withContext true
                }
            }
            false
        }
    }

    private data class Setup(val gpu: Boolean, val vision: Boolean)

    private fun start(file: File, setup: Setup, budget: Int): Engine? {
        val config = EngineConfig(
            modelPath = file.absolutePath,
            backend = if (setup.gpu) Backend.GPU() else Backend.CPU(),
            // Gemma 3n's vision encoder runs on the GPU only.
            visionBackend = if (setup.vision) Backend.GPU() else null,
            audioBackend = null,
            maxNumTokens = budget,
            maxNumImages = if (setup.vision) 1 else null,
            // Compiled GPU programs are cached here, so the second start is quicker.
            cacheDir = app.cacheDir.absolutePath,
        )
        val created = try {
            Engine(config)
        } catch (e: Throwable) {
            return null          // includes the native library failing to load
        }
        return try {
            created.initialize()
            // The GPU is only really started by the first conversation: an
            // engine whose vision encoder cannot get one still initialises,
            // then fails every request. Try one now, so the next setup in
            // the list is used instead.
            created.createConversation(ConversationConfig()).close()
            created
        } catch (e: Throwable) {
            runCatching { created.close() }
            null
        }
    }

    override fun generate(prompt: String, system: String?): Flow<String> =
        respond(Contents.of(Content.Text(prompt)), system)

    override fun describe(image: ByteArray, question: String, system: String?): Flow<String> =
        // The picture before the words, the order Gemma was trained on.
        respond(Contents.of(Content.ImageBytes(image), Content.Text(question)), system)

    private fun respond(contents: Contents, system: String?): Flow<String> = callbackFlow {
        // As in MediaPipeLlm: the flag is set inside the block, so a permit
        // acquired just as the timeout fires is still recorded and released.
        var acquired = false
        withTimeoutOrNull(WAIT_FOR_ENGINE_MS) {
            busy.acquire()
            acquired = true
        }
        if (!acquired) {
            close(IllegalStateException("The model is still busy with the last request."))
            return@callbackFlow
        }
        val running = engine
        if (running == null) {
            busy.release()
            close()
            return@callbackFlow
        }
        cancelled = false

        val conversation = try {
            running.createConversation(
                ConversationConfig(
                    systemInstruction = system?.takeIf { it.isNotBlank() }?.let { Contents.of(it) },
                    samplerConfig = SamplerConfig(TOP_K, TOP_P, TEMPERATURE, SEED),
                )
            )
        } catch (e: Throwable) {
            busy.release()
            close(e)
            return@callbackFlow
        }
        val turn = Turn(conversation)
        synchronized(lock) { current = turn }

        // Runs once, on the engine's thread, when it has finished either way.
        fun finished(error: Throwable?) {
            if (!turn.finished.compareAndSet(false, true)) return
            synchronized(lock) {
                turn.nativeDone = true
                if (current === turn) current = null
            }
            closer.execute {
                runCatching { conversation.close() }
                busy.release()
            }
            // A cancelled reply ends quietly; anything else is reported.
            if (error == null || error is CancellationException) close() else close(error)
        }

        try {
            conversation.sendMessageAsync(contents, object : MessageCallback {
                override fun onMessage(message: Message) {
                    if (cancelled) return
                    // Each message is the next piece, not the whole reply so far.
                    val text = message.contents.contents
                        .filterIsInstance<Content.Text>()
                        .joinToString("") { it.text }
                    if (text.isNotEmpty()) trySend(text)
                }

                override fun onDone() = finished(null)

                override fun onError(throwable: Throwable) = finished(throwable)
            })
        } catch (e: Throwable) {
            finished(e)
        }

        // The collector leaving early (interrupted) stops the native work; the
        // permit comes back through finished() once it has actually stopped.
        awaitClose {
            cancelled = true
            synchronized(lock) {
                if (!turn.nativeDone) runCatching { conversation.cancelProcess() }
            }
        }
    }

    override fun cancel() {
        cancelled = true
        synchronized(lock) {
            current?.let { t -> if (!t.nativeDone) runCatching { t.conversation.cancelProcess() } }
        }
    }

    override fun promptRoom(system: String?): Int {
        val budget = (tokens.takeIf { it > 0 } ?: TOKEN_BUDGETS.last()) - REPLY_RESERVE
        return (budget.coerceAtLeast(256) * CHARS_PER_TOKEN - (system?.trim()?.length ?: 0))
            .coerceAtLeast(400)
    }

    override suspend fun unloadWhenIdle() {
        withTimeoutOrNull(WAIT_FOR_ENGINE_MS) { busy.withPermit { closeEngine() } }
    }

    private fun closeEngine() {
        runCatching { engine?.close() }
        engine = null
        loadedKey = null
        tokens = 0
        supportsVision = false
    }

    companion object {
        /** Context sizes to try, largest first: Gemma 3n is built for 4096. */
        private val TOKEN_BUDGETS = listOf(4096, 2048, 1024)

        /** Held back for the answer when sizing a prompt. */
        private const val REPLY_RESERVE = 400

        /** Conservative English estimate; under-estimating would overflow. */
        private const val CHARS_PER_TOKEN = 3

        // Gemma 3n's reference settings, cooler on temperature: she should be
        // concise and steady, not inventive.
        private const val TOP_K = 64
        private const val TOP_P = 0.95
        private const val TEMPERATURE = 0.7
        private const val SEED = 0

        private const val WAIT_FOR_ENGINE_MS = 45_000L
        private const val RETRY_FAILED_MS = 10 * 60_000L

        /**
         * LiteRT-LM ships for 64-bit phones (and x86_64 emulators) only.
         * Older 32-bit phones stay on MediaPipe.
         */
        fun deviceSupported(abis: Array<String> = Build.SUPPORTED_ABIS): Boolean =
            abis.any { it == "arm64-v8a" || it == "x86_64" }

        /** Whether a model file takes pictures, from its own metadata. */
        fun visionIn(file: File): Boolean = try {
            Capabilities(file.absolutePath).use { it.inputModalities().vision }
        } catch (e: Throwable) {
            false
        }
    }
}
