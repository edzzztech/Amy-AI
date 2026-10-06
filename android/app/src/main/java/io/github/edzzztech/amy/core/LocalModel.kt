package io.github.edzzztech.amy.core

import android.annotation.SuppressLint
import android.content.Context
import android.net.Uri
import android.os.storage.StorageManager
import android.provider.OpenableColumns
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.emptyFlow
import java.io.File

/**
 * The model on the phone, whichever runtime it needs.
 *
 * A `.litertlm` file runs on [LiteRtLlm], and if it is Gemma 3n she can see.
 * A `.task` file runs on [MediaPipeLlm], exactly as before, so a phone that
 * already had a model keeps working untouched. With both present the
 * `.litertlm` wins, being the one that can do more. A 32-bit phone, which
 * LiteRT-LM does not support, stays on `.task`.
 */
class LocalModel(context: Context) {

    private val app = context.applicationContext
    private val liteRt = LiteRtLlm(app)
    private val mediaPipe = MediaPipeLlm(app)
    private val liteRtUsable = LiteRtLlm.deviceSupported()

    @Volatile private var active: LlmEngine? = null

    /** The model file last asked about by [canSee], and the answer. */
    @Volatile private var seeing: Pair<String, Boolean>? = null

    enum class Choice { LITERT, MEDIAPIPE, UNSUPPORTED, NONE }

    private fun choice(): Choice =
        choose(liteRt.findModel() != null, mediaPipe.findModel() != null, liteRtUsable)

    private fun engineFor(choice: Choice): LlmEngine? = when (choice) {
        Choice.LITERT -> liteRt
        Choice.MEDIAPIPE -> mediaPipe
        else -> null
    }

    /** The folder model files go in. */
    fun expectedPath(): String =
        app.getExternalFilesDir(null)?.absolutePath ?: "the app's files directory"

    /**
     * Load the model, switching runtime first if the files on the phone have
     * changed - the old one is freed once nothing is running on it.
     */
    suspend fun ensureLoaded(): Boolean {
        val want = engineFor(choice()) ?: return false
        val was = active
        if (was != null && was !== want) was.unloadWhenIdle()
        active = want
        return want.ensureLoaded()
    }

    /** Why there is nothing to talk with, in words she can say. */
    fun unavailableReason(): String = when (choice()) {
        Choice.NONE -> "I have no model yet. Put one in ${expectedPath()}."
        Choice.UNSUPPORTED ->
            "This phone can't run .litertlm models; it needs a 64-bit processor. A .task model will work."
        else -> "I found a model but couldn't load it. It may be the wrong format, " +
            "or too large for this phone's memory."
    }

    /** Whether the loaded model takes pictures. */
    val supportsVision: Boolean
        get() = active?.supportsVision == true

    /**
     * Whether she will be able to see, before anything is loaded - for the
     * camera's hint. Reads the model file's metadata, so not on the main thread.
     */
    fun canSee(): Boolean {
        if (choice() != Choice.LITERT) return false
        val file = liteRt.findModel() ?: return false
        val key = "${file.absolutePath}|${file.length()}|${file.lastModified()}"
        seeing?.let { (k, v) -> if (k == key) return v }
        return LiteRtLlm.visionIn(file).also { seeing = key to it }
    }

    /** Free the loaded model, so the next [ensureLoaded] picks up a new file. */
    suspend fun reload() {
        active?.unloadWhenIdle()
        active = null
        seeing = null
    }

    /**
     * Copy a model file the user picked into the model folder. Returns why it
     * failed, or null once it is in place. Blocks: call off the main thread.
     */
    fun install(uri: Uri, name: String, progress: (Int) -> Unit): String? {
        if (!isModelName(name)) return "That isn't a model file. I take .litertlm and .task files."
        val dir = app.getExternalFilesDir(null) ?: return "There's no storage for models on this phone."
        val resolver = app.contentResolver
        val size = runCatching {
            resolver.query(uri, arrayOf(OpenableColumns.SIZE), null, null, null)?.use { c ->
                if (c.moveToFirst() && !c.isNull(0)) c.getLong(0) else -1L
            }
        }.getOrNull() ?: -1L
        val free = freeSpace(dir)
        if (size > 0 && free < size + SPARE_BYTES) {
            return "There isn't room for that model: it needs ${gb(size)} GB free and the phone has " +
                "${gb(free)} GB."
        }
        val target = File(dir, File(name).name)
        val part = File(dir, target.name + ".part")
        return try {
            val opened = resolver.openInputStream(uri)?.use { inp ->
                part.outputStream().use { out ->
                    val buf = ByteArray(1 shl 20)
                    var copied = 0L
                    var shown = -1
                    while (true) {
                        val n = inp.read(buf)
                        if (n < 0) break
                        out.write(buf, 0, n)
                        copied += n
                        if (size > 0) {
                            val percent = (copied * 100 / size).toInt()
                            if (percent != shown) progress(percent).also { shown = percent }
                        }
                    }
                }
                true
            }
            if (opened == null) return "I couldn't open that file."
            if (part.length() < MIN_MODEL_BYTES) {
                part.delete()
                return "That file is too small to be a model. It may not have finished downloading."
            }
            if (target.exists()) target.delete()
            if (!part.renameTo(target)) {
                part.delete()
                return "I couldn't put the model in place."
            }
            null
        } catch (e: Exception) {
            part.delete()
            "Installing the model failed: ${e.message ?: "unknown error"}"
        }
    }

    /** Space a model can have, counting cached files the phone would clear for it. */
    @SuppressLint("UsableSpace")     // only the fallback, where the better call failed
    private fun freeSpace(dir: File): Long = try {
        val storage = app.getSystemService(StorageManager::class.java)
        storage.getAllocatableBytes(storage.getUuidForPath(dir))
    } catch (e: Exception) {
        dir.usableSpace
    }

    fun promptRoom(system: String?): Int = (active ?: mediaPipe).promptRoom(system)

    fun generate(prompt: String, system: String?): Flow<String> =
        active?.generate(prompt, system) ?: emptyFlow()

    fun describe(image: ByteArray, question: String, system: String?): Flow<String> =
        active?.describe(image, question, system) ?: emptyFlow()

    fun cancel() {
        active?.cancel()
    }

    companion object {
        /** Kept free beyond the model itself, so the phone is not left full. */
        private const val SPARE_BYTES = 300L * 1024 * 1024
        private const val MIN_MODEL_BYTES = 10L * 1024 * 1024

        private fun gb(bytes: Long) = String.format(java.util.Locale.UK, "%.1f", bytes / 1_073_741_824.0)

        /** Whether a picked file is a model to install rather than a document to read. */
        fun isModelName(name: String): Boolean =
            name.lowercase().let { it.endsWith(".litertlm") || it.endsWith(".task") }

        /** Which runtime to use, given what is on the phone. Pure, for testing. */
        internal fun choose(hasLiteRtModel: Boolean, hasTaskModel: Boolean, liteRtUsable: Boolean): Choice =
            when {
                hasLiteRtModel && liteRtUsable -> Choice.LITERT
                hasTaskModel -> Choice.MEDIAPIPE
                hasLiteRtModel -> Choice.UNSUPPORTED      // a .litertlm on a 32-bit phone
                else -> Choice.NONE
            }
    }
}
