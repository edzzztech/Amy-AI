package io.github.edzzztech.amy.ui

import android.content.Context
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.Paint
import android.graphics.RadialGradient
import android.graphics.Shader
import android.view.View
import io.github.edzzztech.amy.core.Listening
import kotlin.math.cos
import kotlin.math.min
import kotlin.math.sin

/**
 * The orb as a plain View, for the floating overlay.
 *
 * Compose inside a WindowManager overlay needs lifecycle, saved-state and
 * recomposer owners wired up by hand, and gets subtly wrong in ways that only
 * show on some devices. A custom View drawing the same gradient is far less to
 * go wrong, and this is the same maths as the desktop painter.
 */
class OrbView(context: Context) : View(context) {

    var state: Listening = Listening.Idle
        set(value) {
            field = value
            invalidate()
        }

    var level: Float = 0f
        set(value) {
            field = value.coerceIn(0f, 1f)
        }

    private val paint = Paint(Paint.ANTI_ALIAS_FLAG)
    private val start = System.nanoTime()

    private val teal = Color.rgb(94, 234, 212)
    private val violet = Color.rgb(167, 139, 250)
    private val deepViolet = Color.rgb(109, 72, 206)
    private val muted = Color.rgb(140, 146, 166)
    private val mutedDeep = Color.rgb(88, 94, 112)
    private val mutedShade = Color.rgb(56, 60, 74)

    override fun onDraw(canvas: Canvas) {
        val t = ((System.nanoTime() - start) / 1_000_000_000.0).toFloat()
        val isMuted = state == Listening.Muted
        val synthetic = if (state == Listening.Speaking) {
            0.18f + 0.14f * kotlin.math.abs(sin(t * 5.3f)) * kotlin.math.abs(sin(t * 1.7f))
        } else 0f
        val lvl = if (isMuted) 0f else maxOf(level, synthetic)

        val hi = if (isMuted) muted else teal
        val body = if (isMuted) mutedDeep else violet
        val deep = if (isMuted) mutedShade else deepViolet

        val s = min(width, height).toFloat()
        val cx = width / 2f
        val cy = height / 2f
        val r = s * 0.33f * (1f + 0.07f * lvl)

        // Halo, kept inside the view so it fades rather than clipping square.
        val haloR = min(r * 1.8f, s * 0.495f)
        paint.shader = RadialGradient(
            cx, cy, haloR,
            intArrayOf(
                withAlpha(hi, 0.18f + 0.30f * lvl),
                withAlpha(body, 0.10f + 0.18f * lvl),
                withAlpha(body, 0f),
            ),
            floatArrayOf(0f, 0.45f, 1f),
            Shader.TileMode.CLAMP,
        )
        canvas.drawCircle(cx, cy, haloR, paint)

        // The sphere, with the highlight drifting and the mid stop breathing.
        val drift = t * 0.38f
        val ox = cx + r * (-0.36f + 0.11f * cos(drift))
        val oy = cy + r * (-0.40f + 0.10f * sin(drift * 1.17f))
        val mid = (0.72f + 0.08f * sin(t * 0.52f)).coerceIn(0.45f, 0.92f)
        paint.shader = RadialGradient(
            ox, oy, r * 1.62f,
            intArrayOf(hi, hi, body, deep),
            floatArrayOf(0f, maxOf(0.12f, mid - 0.38f), mid, 1f),
            Shader.TileMode.CLAMP,
        )
        canvas.drawCircle(cx, cy, r, paint)

        // Two currents running against each other, so the surface churns.
        current(canvas, cx, cy, r,
            cx + r * 0.34f * cos(-drift * 0.72f + 2.1f),
            cy + r * 0.34f * sin(-drift * 0.72f + 2.1f),
            r * (0.9f + 0.3f * lvl), withAlpha(hi, 0.20f + 0.22f * lvl))
        current(canvas, cx, cy, r,
            cx + r * 0.40f * cos(drift * 1.31f + 4.2f),
            cy + r * 0.40f * sin(drift * 1.31f + 4.2f),
            r * (0.7f + 0.35f * lvl), withAlpha(body, 0.18f + 0.20f * lvl))
        current(canvas, cx, cy, r,
            cx + r * 0.42f * cos(t * 0.6f),
            cy + r * 0.42f * sin(t * 0.6f),
            r * 0.85f, withAlpha(Color.WHITE, 0.05f + 0.08f * lvl))

        paint.shader = null
        postInvalidateOnAnimation()
    }

    private fun current(
        canvas: Canvas, cx: Float, cy: Float, sphereR: Float,
        x: Float, y: Float, radius: Float, colour: Int,
    ) {
        paint.shader = RadialGradient(
            x, y, radius.coerceAtLeast(1f),
            intArrayOf(colour, colour and 0x00FFFFFF),
            floatArrayOf(0f, 1f),
            Shader.TileMode.CLAMP,
        )
        canvas.drawCircle(cx, cy, sphereR, paint)
    }

    private fun withAlpha(colour: Int, alpha: Float): Int =
        Color.argb(
            (alpha.coerceIn(0f, 1f) * 255).toInt(),
            Color.red(colour), Color.green(colour), Color.blue(colour),
        )
}
