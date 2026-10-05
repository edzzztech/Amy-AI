package io.github.edzzztech.amy.automation

import android.accessibilityservice.AccessibilityService
import android.accessibilityservice.GestureDescription
import android.graphics.Path
import android.os.Bundle
import android.view.accessibility.AccessibilityEvent
import android.view.accessibility.AccessibilityNodeInfo

/**
 * How Amy drives other apps without root: read the node tree, find things by
 * their visible text, tap and type.
 *
 * The desktop gates destructive automation behind an approval prompt; the same
 * rule applies here, and the gate belongs in the caller rather than this class,
 * which stays a dumb set of primitives.
 */
class AmyAccessibilityService : AccessibilityService() {

    companion object {
        @Volatile
        var instance: AmyAccessibilityService? = null
            private set

        val isConnected: Boolean get() = instance != null
    }

    override fun onServiceConnected() {
        super.onServiceConnected()
        instance = this
    }

    override fun onDestroy() {
        instance = null
        super.onDestroy()
    }

    override fun onAccessibilityEvent(event: AccessibilityEvent?) {
        // Nothing reactive yet. Amy pulls the tree when she needs it rather
        // than reacting to every event, which would burn battery for no gain.
    }

    override fun onInterrupt() {}

    /** Everything currently on screen, flattened, for the model to read. */
    fun describeScreen(): List<String> {
        val root = rootInActiveWindow ?: return emptyList()
        val out = mutableListOf<String>()
        walk(root) { node ->
            val text = node.text?.toString()?.trim()
            val desc = node.contentDescription?.toString()?.trim()
            val label = text?.takeIf { it.isNotEmpty() } ?: desc?.takeIf { it.isNotEmpty() }
            if (label != null) {
                val kind = node.className?.toString()?.substringAfterLast('.') ?: "View"
                out += if (node.isClickable) "[$kind] $label (tappable)" else "[$kind] $label"
            }
        }
        return out
    }

    /** Tap the first element whose text or description contains [query]. */
    fun tapByText(query: String): Boolean {
        val root = rootInActiveWindow ?: return false
        var target: AccessibilityNodeInfo? = null
        walk(root) { node ->
            if (target != null) return@walk
            val hay = buildString {
                append(node.text ?: "")
                append(' ')
                append(node.contentDescription ?: "")
            }.lowercase()
            if (hay.contains(query.lowercase())) {
                target = generateSequence(node) { it.parent }.firstOrNull { it.isClickable }
            }
        }
        return target?.performAction(AccessibilityNodeInfo.ACTION_CLICK) ?: false
    }

    /** Type into whatever currently has focus. */
    fun typeIntoFocused(text: String): Boolean {
        val node = findFocus(AccessibilityNodeInfo.FOCUS_INPUT) ?: return false
        val args = Bundle().apply {
            putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, text)
        }
        return node.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, args)
    }

    fun back(): Boolean = performGlobalAction(GLOBAL_ACTION_BACK)
    fun home(): Boolean = performGlobalAction(GLOBAL_ACTION_HOME)

    /** A tap at raw coordinates, for when nothing is addressable by text. */
    fun tapAt(x: Float, y: Float, durationMs: Long = 50L): Boolean {
        val path = Path().apply { moveTo(x, y) }
        val stroke = GestureDescription.StrokeDescription(path, 0L, durationMs)
        val gesture = GestureDescription.Builder().addStroke(stroke).build()
        return dispatchGesture(gesture, null, null)
    }

    private fun walk(node: AccessibilityNodeInfo, visit: (AccessibilityNodeInfo) -> Unit) {
        visit(node)
        for (i in 0 until node.childCount) {
            node.getChild(i)?.let { walk(it, visit) }
        }
    }
}
