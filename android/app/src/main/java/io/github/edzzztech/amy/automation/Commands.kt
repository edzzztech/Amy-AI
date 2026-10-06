package io.github.edzzztech.amy.automation

import android.content.Context
import android.content.Intent
import android.hardware.camera2.CameraCharacteristics
import android.hardware.camera2.CameraManager
import android.media.AudioManager
import android.os.BatteryManager
import android.provider.AlarmClock
import io.github.edzzztech.amy.CameraActivity
import io.github.edzzztech.amy.core.ActionLog
import io.github.edzzztech.amy.core.Amy
import io.github.edzzztech.amy.core.RootShell
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import java.time.LocalDate
import java.time.LocalTime
import java.time.format.DateTimeFormatter
import java.time.format.FormatStyle
import java.util.Locale
import kotlin.math.roundToInt

/**
 * Deterministic commands, matched before anything reaches the model.
 *
 * Same principle as the desktop: "open Spotify" should open Spotify every
 * time, not depend on a 1B model deciding to call a tool correctly. The same
 * goes for anything the phone knows better than the model — the time, the
 * date, the battery — which a small model will otherwise guess, confidently.
 * Anything not matched here falls through to conversation.
 *
 * The parsing lives in [Phrases], where it is tested; this class only acts.
 */
class Commands(
    private val context: Context,
    private val actions: ActionLog,
) {

    private val apps = AppControl(context)

    /**
     * The reply to speak, or null to mean "not a command, ask the model".
     * Called on the main thread; anything slow inside switches away from it.
     */
    suspend fun handle(input: String): String? {
        val text = Phrases.bare(input)
        val lower = text.lowercase()
        if (lower.isEmpty()) return null
        return answer(lower)
            ?: device(lower)
            ?: clock(lower)
            ?: look(lower)
            ?: launch(lower)
            ?: onScreen(lower, Phrases.politeStartRemoved(input))
    }

    /** Open the camera to be shown something - if the model can see. */
    private suspend fun look(lower: String): String? {
        if (!Phrases.asksToLook(lower)) return null
        // Reads the model file's metadata: not on the main thread.
        if (!withContext(Dispatchers.IO) { Amy.llm.canSee() }) {
            return "I can't see yet. With a Gemma 3n model on the phone, I could."
        }
        return when (apps.start(Intent(context, CameraActivity::class.java))) {
            AppControl.Start.Started -> "Show me, and tap the button."
            AppControl.Start.Blocked -> BLOCKED
            AppControl.Start.Failed -> "I couldn't open the camera."
        }
    }

    // --- things the phone knows ----------------------------------------------

    private suspend fun answer(lower: String): String? {
        if (Phrases.asksTime(lower)) {
            return "It's " + LocalTime.now().format(spokenTime()) + "."
        }
        if (Phrases.asksDate(lower)) {
            return "It's " + LocalDate.now().format(spokenDate()) + "."
        }
        if (Phrases.asksBattery(lower)) return battery()
        Phrases.actionLogHours(lower)?.let { hours -> return actionLog(hours) }
        if (Phrases.asksAppCount(lower)) {
            return "I can see ${apps.refresh()} apps on this phone."
        }
        return null
    }

    private fun battery(): String {
        val bm = context.getSystemService(BatteryManager::class.java)
            ?: return "I can't read the battery."
        val level = bm.getIntProperty(BatteryManager.BATTERY_PROPERTY_CAPACITY)
        if (level !in 0..100) return "I can't read the battery."
        return "The battery is at $level percent" + if (bm.isCharging) ", and charging." else "."
    }

    /**
     * Read back what she has done. The log records every message too, which
     * would drown the answer, so only actions are spoken.
     */
    private suspend fun actionLog(hours: Int): String {
        val done = withContext(Dispatchers.IO) { actions.since(hours) }
            .filter { it.kind != "message" && it.kind != "system" }
        val span = if (hours == 24) "in the last day" else "in the last $hours hours"
        if (done.isEmpty()) return "Nothing $span, apart from talking."
        val latest = done.takeLast(SPOKEN_ACTIONS).reversed().joinToString("; ") { e ->
            e.summary + if (e.outcome != "done") " (${e.outcome})" else ""
        }
        val more = if (done.size > SPOKEN_ACTIONS) {
            " Those are the latest $SPOKEN_ACTIONS of ${done.size}."
        } else ""
        return "Most recent first: $latest.$more"
    }

    // --- the phone's own hardware -------------------------------------------

    private fun device(lower: String): String? {
        Phrases.torch(lower)?.let { on -> return torch(on) }
        Phrases.volume(lower)?.let { change -> return volume(change) }
        return null
    }

    private fun torch(on: Boolean): String {
        val cm = context.getSystemService(CameraManager::class.java)
            ?: return NO_TORCH
        val id = try {
            cm.cameraIdList.firstOrNull { id ->
                cm.getCameraCharacteristics(id)
                    .get(CameraCharacteristics.FLASH_INFO_AVAILABLE) == true
            }
        } catch (e: Exception) {
            null
        } ?: return NO_TORCH
        return try {
            cm.setTorchMode(id, on)
            actions.record("device", if (on) "Torch on" else "Torch off")
            if (on) "Torch on." else "Torch off."
        } catch (e: Exception) {
            // Thrown while the camera is open, in her camera view or another app.
            "The camera is in use, so I can't work the torch right now."
        }
    }

    /**
     * Media volume only. The ringer is the user's to set: an assistant that
     * silences the phone to stop a sound is solving the wrong problem.
     */
    private fun volume(change: Phrases.Volume): String {
        val am = context.getSystemService(AudioManager::class.java)
            ?: return "I can't change the volume."
        val stream = AudioManager.STREAM_MUSIC
        val max = am.getStreamMaxVolume(stream).coerceAtLeast(1)
        val target = when (change) {
            // A tenth of the range per step. The system's own step is a
            // fifteenth or less, which is too small to hear the difference.
            is Phrases.Volume.Step -> {
                val step = (max / 10).coerceAtLeast(1)
                am.getStreamVolume(stream) + if (change.up) step else -step
            }
            is Phrases.Volume.Percent -> (max * change.value / 100.0).roundToInt()
        }.coerceIn(0, max)
        return try {
            am.setStreamVolume(stream, target, AudioManager.FLAG_SHOW_UI)
            val percent = am.getStreamVolume(stream) * 100 / max
            actions.record("device", "Media volume $percent%")
            "Volume $percent percent."
        } catch (e: Exception) {
            "Android wouldn't let me change the volume."
        }
    }

    // --- timers and alarms ----------------------------------------------------

    private fun clock(lower: String): String? {
        Phrases.timer(lower)?.let { seconds ->
            val intent = Intent(AlarmClock.ACTION_SET_TIMER)
                .putExtra(AlarmClock.EXTRA_LENGTH, seconds)
                .putExtra(AlarmClock.EXTRA_SKIP_UI, true)
                .putExtra(AlarmClock.EXTRA_MESSAGE, "Amy")
            val said = Phrases.describe(seconds)
            return started(apps.start(intent), "Timer for $said") {
                "Timer set for $said."
            }
        }
        val now = LocalTime.now()
        Phrases.alarm(lower, now.hour, now.minute)?.let { (hour, minute) ->
            val intent = Intent(AlarmClock.ACTION_SET_ALARM)
                .putExtra(AlarmClock.EXTRA_HOUR, hour)
                .putExtra(AlarmClock.EXTRA_MINUTES, minute)
                .putExtra(AlarmClock.EXTRA_SKIP_UI, true)
                .putExtra(AlarmClock.EXTRA_MESSAGE, "Amy")
            val said = LocalTime.of(hour, minute).format(spokenTime())
            return started(apps.start(intent), "Alarm for $said") {
                "Alarm set for $said."
            }
        }
        return null
    }

    private fun started(result: AppControl.Start, what: String, ok: () -> String): String =
        when (result) {
            AppControl.Start.Started -> {
                actions.record("automation", what)
                ok()
            }
            AppControl.Start.Blocked -> {
                actions.record("automation", what, outcome = "blocked")
                BLOCKED
            }
            AppControl.Start.Failed -> {
                actions.record("automation", what, outcome = "failed")
                "I couldn't find a clock app to do that."
            }
        }

    // --- launching apps -------------------------------------------------------

    private suspend fun launch(lower: String): String? {
        val open = Phrases.open(lower) ?: return null
        return when (val result = apps.launch(open.name, loose = open.definite)) {
            is AppControl.Launch.Opened -> {
                actions.record("automation", "Opened ${result.label}")
                "Opening ${result.label}."
            }
            is AppControl.Launch.Blocked -> {
                // Root is not bound by the background rules; try it before
                // telling the user what to switch on.
                if (result.component != null && RootShell.startActivity(result.component)) {
                    actions.record("automation", "Opened ${result.label}", detail = "as root")
                    "Opening ${result.label}."
                } else {
                    actions.record("automation", "Open ${result.label}", outcome = "blocked")
                    BLOCKED
                }
            }
            is AppControl.Launch.Failed -> {
                actions.record("automation", "Open ${result.label}", outcome = "failed")
                "I found ${result.label}, but it wouldn't open."
            }
            AppControl.Launch.NotFound ->
                if (open.definite) {
                    actions.record("automation", "Could not find app: ${open.name}",
                        outcome = "failed")
                    "I can't find an app called ${open.name}."
                } else {
                    null          // "start a conversation about..." is for the model
                }
        }
    }

    // --- acting on what is on screen --------------------------------------------

    private fun onScreen(lower: String, typedText: String): String? {
        if (Phrases.isBack(lower)) {
            val svc = requireService() ?: return NO_SERVICE
            svc.back()
            actions.record("automation", "Back")
            return "Done."
        }
        if (Phrases.isHome(lower)) {
            val svc = requireService() ?: return NO_SERVICE
            svc.home()
            actions.record("automation", "Home")
            return "Done."
        }
        Phrases.tapTarget(lower)?.let { target ->
            val svc = requireService() ?: return NO_SERVICE
            val ok = svc.tapByText(target)
            actions.record("automation", "Tap '$target'",
                outcome = if (ok) "done" else "not found")
            return if (ok) "Tapped $target." else "I can't see anything called $target."
        }
        Phrases.typed(typedText)?.let { what ->
            val svc = requireService() ?: return NO_SERVICE
            val ok = svc.typeIntoFocused(what)
            actions.record("automation", "Typed into focused field",
                outcome = if (ok) "done" else "no field")
            return if (ok) "Typed it." else "Nothing is focused to type into."
        }
        if (Phrases.asksScreen(lower)) {
            val svc = requireService() ?: return NO_SERVICE
            val seen = svc.describeScreen()
            actions.record("automation", "Read the screen")
            return if (seen.isEmpty()) "I can't read anything on screen."
            else "I can see: " + seen.take(12).joinToString("; ")
        }
        return null
    }

    private fun requireService(): AmyAccessibilityService? =
        AmyAccessibilityService.instance

    // Built when used, not once: the phone's language can change while she
    // runs, and a formatter keeps the locale it was made with.
    private fun spokenTime() = DateTimeFormatter.ofLocalizedTime(FormatStyle.SHORT)
        .withLocale(Locale.getDefault())

    private fun spokenDate() = DateTimeFormatter.ofPattern("EEEE d MMMM", Locale.getDefault())

    private companion object {
        const val SPOKEN_ACTIONS = 5

        const val NO_SERVICE =
            "I need accessibility access for that. Turn Amy on under Settings, Accessibility."
        const val NO_TORCH = "This phone doesn't have a torch I can use."
        const val BLOCKED =
            "Android won't let me do that while I'm in the background. Let me display " +
                "over other apps, or turn on my accessibility service, and I can."
    }
}
