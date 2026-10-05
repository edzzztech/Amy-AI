package io.github.edzzztech.amy

import android.app.Service
import android.content.Context
import android.content.Intent
import android.graphics.PixelFormat
import android.os.Build
import android.os.IBinder
import android.provider.Settings
import android.view.Gravity
import android.view.MotionEvent
import android.view.WindowManager
import io.github.edzzztech.amy.core.Amy
import io.github.edzzztech.amy.core.AmyState
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
    private val scope = CoroutineScope(Dispatchers.Main)

    override fun onCreate() {
        super.onCreate()
        if (!isAllowed(this)) {
            stopSelf()
            return
        }
        Amy.start(this)
        show()
    }

    private fun show() {
        val wm = getSystemService(WINDOW_SERVICE) as WindowManager
        val view = OrbView(this)
        val size = (84 * resources.displayMetrics.density).toInt()

        val type = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY
        } else {
            @Suppress("DEPRECATION")
            WindowManager.LayoutParams.TYPE_PHONE
        }

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

        view.setOnTouchListener(DragHandler(wm, lp, view))
        wm.addView(view, lp)

        windowManager = wm
        orb = view
        params = lp

        // Mirror her state onto the floating orb.
        watcher = scope.launch {
            AmyState.state.collect { state -> view.state = state }
        }
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
                        if (held > LONG_PRESS_MS) toggleListening() else openApp()
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

    private fun toggleListening() {
        val sleeping = AmyState.state.value == io.github.edzzztech.amy.core.Listening.Muted
        startForegroundService(
            Intent(this, AmyService::class.java)
                .setAction(if (sleeping) AmyService.ACTION_LISTEN else AmyService.ACTION_SLEEP)
        )
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int = START_STICKY

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onDestroy() {
        watcher?.cancel()
        orb?.let { view -> runCatching { windowManager?.removeView(view) } }
        orb = null
        windowManager = null
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
                android.net.Uri.parse("package:${context.packageName}"),
            )
    }
}
