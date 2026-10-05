package io.github.edzzztech.amy

import android.app.Service
import android.content.Context
import android.content.Intent
import android.graphics.PixelFormat
import android.os.IBinder
import android.provider.Settings
import android.view.Gravity
import android.view.MotionEvent
import android.view.WindowManager
import androidx.core.net.toUri
import io.github.edzzztech.amy.core.Amy
import io.github.edzzztech.amy.core.AmyState
import io.github.edzzztech.amy.core.Listening
import io.github.edzzztech.amy.ui.OrbView
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.launch
import kotlin.math.abs

/**
 * The orb floating above everything else — the phone's answer to the desktop
 * overlay. Drag it anywhere, tap it to open Amy, long-press to put her to sleep
 * or wake her.
 *
 * Needs SYSTEM_ALERT_WINDOW, which the user must grant in Settings; there is no
 * way to request it inline. [isAllowed] checks before we try, because starting
 * without it throws.
 */
class OverlayService : Service() {

    private var windowManager: WindowManager? = null
    private var orb: OrbView? = null
    private var params: WindowManager.LayoutParams? = null
    private var watcher: Job? = null
    private var levelWatcher: Job? = null
    private val scope = CoroutineScope(Dispatchers.Main)

    override fun onCreate() {
        super.onCreate()
        if (!isAllowed(this)) {
            stopSelf()
            return
        }
        Amy.start(this)
        if (show()) AmyState.setOverlay(true)
    }

    /** False if the window could not be added, in which case the service stops. */
    private fun show(): Boolean {
        val wm = getSystemService(WINDOW_SERVICE) as WindowManager
        val view = OrbView(this)
        val size = (84 * resources.displayMetrics.density).toInt()

        // minSdk is 29, so the modern overlay type is always available.
        val type = WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY

        val lp = WindowManager.LayoutParams(
            size, size, type,
            // Not focusable, so it never steals the keyboard from the app
            // underneath; still touchable so it can be dragged and tapped.
            WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE or
                WindowManager.LayoutParams.FLAG_LAYOUT_NO_LIMITS,
            PixelFormat.TRANSLUCENT,
        ).apply {
            gravity = Gravity.TOP or Gravity.START
            x = resources.displayMetrics.widthPixels - size - 24
            y = resources.displayMetrics.heightPixels / 3
        }

        // Real click listeners, not just touch handling: a screen reader's
        // double tap calls performClick, and without these it did nothing.
        view.setOnClickListener { openApp() }
        view.setOnLongClickListener { toggleListening(); true }
        view.setOnTouchListener(DragHandler(wm, lp, view))
        try {
            wm.addView(view, lp)
        } catch (e: Exception) {
            // The permission can be revoked between the check and here.
            stopSelf()
            return false
        }

        windowManager = wm
        orb = view
        params = lp

        // Mirror her state onto the floating orb.
        watcher = scope.launch {
            AmyState.state.collect { state -> view.state = state }
        }
        levelWatcher = scope.launch {
            AmyState.level.collect { lvl -> view.level = lvl }
        }
        return true
    }

    /** Drag to move, tap to open, long-press to sleep or wake. */
    private inner class DragHandler(
        private val wm: WindowManager,
        private val lp: WindowManager.LayoutParams,
        private val view: OrbView,
    ) : android.view.View.OnTouchListener {

        private var startX = 0
        private var startY = 0
        private var touchX = 0f
        private var touchY = 0f
        private var downAt = 0L
        private var moved = false

        /**
         * The window may be dragged past the edges, but not left there: a
         * floating control that is fully off screen is lost until restart.
         */
        private fun keepOnScreen() {
            val dm = resources.displayMetrics
            val margin = (lp.width * 0.4f).toInt()
            lp.x = lp.x.coerceIn(-margin, dm.widthPixels - lp.width + margin)
            lp.y = lp.y.coerceIn(0, dm.heightPixels - lp.height)
            runCatching { wm.updateViewLayout(view, lp) }
        }

        override fun onTouch(v: android.view.View, event: MotionEvent): Boolean {
            when (event.action) {
                MotionEvent.ACTION_DOWN -> {
                    startX = lp.x; startY = lp.y
                    touchX = event.rawX; touchY = event.rawY
                    downAt = System.currentTimeMillis()
                    moved = false
                    return true
                }
                MotionEvent.ACTION_MOVE -> {
                    val dx = (event.rawX - touchX).toInt()
                    val dy = (event.rawY - touchY).toInt()
                    if (abs(dx) > TOUCH_SLOP || abs(dy) > TOUCH_SLOP) moved = true
                    lp.x = startX + dx
                    lp.y = startY + dy
                    runCatching { wm.updateViewLayout(view, lp) }
                    return true
                }
                MotionEvent.ACTION_UP -> {
                    val held = System.currentTimeMillis() - downAt
                    if (!moved) {
                        if (held > LONG_PRESS_MS) v.performLongClick() else v.performClick()
                    } else {
                        keepOnScreen()
                    }
                    return true
                }
            }
            return false
        }
    }

    private fun openApp() {
        startActivity(
            Intent(this, MainActivity::class.java)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        )
    }

    /**
     * Same rules as the mic button in the app. Talking: interrupt. Asleep:
     * wake. Otherwise: sleep. Asleep is read from its own flag; the status
     * reads Speaking while she says "I'll stop listening", and taking that as
     * awake sent a second sleep instead of a wake.
     */
    private fun toggleListening() {
        val state = AmyState.state.value
        if (state == Listening.Speaking || state == Listening.Thinking) {
            Amy.stopSpeaking()
            return
        }
        val action = if (AmyState.asleep.value) AmyService.ACTION_LISTEN else AmyService.ACTION_SLEEP
        try {
            startForegroundService(Intent(this, AmyService::class.java).setAction(action))
        } catch (e: Exception) {
            // Refused from the background on some versions; the app can do it.
            openApp()
        }
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int = START_STICKY

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onDestroy() {
        watcher?.cancel()
        levelWatcher?.cancel()
        orb?.let { view -> runCatching { windowManager?.removeView(view) } }
        orb = null
        windowManager = null
        AmyState.setOverlay(false)
        super.onDestroy()
    }

    companion object {
        private const val TOUCH_SLOP = 12
        private const val LONG_PRESS_MS = 500L

        fun isAllowed(context: Context): Boolean = Settings.canDrawOverlays(context)

        /** Send the user to the one Settings screen that can grant this. */
        fun permissionIntent(context: Context): Intent =
            Intent(
                Settings.ACTION_MANAGE_OVERLAY_PERMISSION,
                "package:${context.packageName}".toUri(),
            )
    }
}
