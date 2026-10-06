package io.github.edzzztech.amy.core

import android.content.Context
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.emptyFlow

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

    fun promptRoom(system: String?): Int = (active ?: mediaPipe).promptRoom(system)

    fun generate(prompt: String, system: String?): Flow<String> =
        active?.generate(prompt, system) ?: emptyFlow()

    fun describe(image: ByteArray, question: String, system: String?): Flow<String> =
        active?.describe(image, question, system) ?: emptyFlow()

    fun cancel() {
        active?.cancel()
    }

    companion object {
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
