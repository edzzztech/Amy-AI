package io.github.edzzztech.amy.automation

import android.content.Context
import io.github.edzzztech.amy.core.ActionLog

/**
 * Deterministic commands, matched before anything reaches the model.
 *
 * Same principle as the desktop: "open Spotify" should open Spotify every
 * time, not depend on a 1B model deciding to call a tool correctly. Anything
 * not matched here falls through to conversation.
 *
 * Returns the reply to speak, or null to mean "not a command, ask the model".
 */
class Commands(
    context: Context,
    private val actions: ActionLog,
) {

    private val apps = AppControl(context)

    fun handle(input: String): String? {
        val text = input.trim()
        val lower = text.lowercase()

        // --- launching -------------------------------------------------------
        OPEN.find(lower)?.let { m ->
            val name = m.groupValues[1].trim().removeSuffix(" app").trim()
            if (name.isEmpty()) return null
            val launched = apps.launch(name)
            return if (launched != null) {
                actions.record("automation", "Opened $launched")
                "Opening $launched."
            } else {
                actions.record("automation", "Could not find app: $name", outcome = "failed")
                "I can't find an app called $name."
            }
        }

        // --- navigation ------------------------------------------------------
        if (lower matches "(go )?back".toRegex()) {
            return requireService()?.let { svc ->
                svc.back(); actions.record("automation", "Back"); "Done."
            } ?: NO_SERVICE
        }
        if (lower matches "(go )?home".toRegex()) {
            return requireService()?.let { svc ->
                svc.home(); actions.record("automation", "Home"); "Done."
            } ?: NO_SERVICE
        }

        // --- acting on what is on screen -------------------------------------
        TAP.find(lower)?.let { m ->
            val target = m.groupValues[1].trim()
            if (target.isEmpty()) return null
            val svc = requireService() ?: return NO_SERVICE
            val ok = svc.tapByText(target)
            actions.record(
                "automation", "Tap '$target'",
                outcome = if (ok) "done" else "not found",
            )
            return if (ok) "Tapped $target." else "I can't see anything called $target."
        }

        TYPE.find(text)?.let { m ->
            val what = m.groupValues[1].trim()
            if (what.isEmpty()) return null
            val svc = requireService() ?: return NO_SERVICE
            val ok = svc.typeIntoFocused(what)
            actions.record("automation", "Typed into focused field",
                outcome = if (ok) "done" else "no field")
            return if (ok) "Typed it." else "Nothing is focused to type into."
        }

        if (SCREEN.containsMatchIn(lower)) {
            val svc = requireService() ?: return NO_SERVICE
            val seen = svc.describeScreen()
            actions.record("automation", "Read the screen")
            return if (seen.isEmpty()) "I can't read anything on screen."
            else "I can see: " + seen.take(12).joinToString("; ")
        }

        // --- the app list ----------------------------------------------------
        if (APP_COUNT.containsMatchIn(lower)) {
            return "I can see ${apps.refresh()} apps on this phone."
        }

        return null
    }

    private fun requireService(): AmyAccessibilityService? =
        AmyAccessibilityService.instance

    private companion object {
        val OPEN = Regex("^(?:open|launch|start|run) (?:the )?(.+)$")
        val TAP = Regex("^(?:tap|press|click|select) (?:on )?(?:the )?(.+)$")
        val TYPE = Regex("^(?:type|enter|write) (.+)$", RegexOption.IGNORE_CASE)
        val SCREEN = Regex("what(?:'s| is| can you see)? on (?:the |my )?screen|read the screen")
        val APP_COUNT = Regex("how many apps|what apps")
        const val NO_SERVICE =
            "I need accessibility access for that. Turn Amy on under Settings, Accessibility."
    }
}
