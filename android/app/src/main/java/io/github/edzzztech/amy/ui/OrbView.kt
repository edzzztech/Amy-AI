package io.github.edzzztech.amy.ui

import android.content.Context
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.Matrix
import android.graphics.Paint
import android.graphics.RadialGradient
import android.graphics.Shader
import android.view.View
import io.github.edzzztech.amy.core.Listening
import kotlin.math.abs
import kotlin.math.cos
import kotlin.math.min
import kotlin.math.roundToInt
import kotlin.math.sin

/**
 * The orb as a plain View, for the floating overlay.
 *
 * Compose inside a WindowManager overlay needs lifecycle, saved-state and
 * recomposer owners wired by hand and fails subtly on some devices; a custom
 * View drawing the same gradients is far less to go wrong.
 *
 * ## Why it is built this way
 *
 * The first version created five gradients and ten arrays every frame — some
 * nine hundred allocations a second at 60fps, all garbage a moment later, which
 * is exactly what makes animation stutter when the collector runs.
 *
 * Instead, each gradient is built once at unit size around the origin, and
 * every frame only *moves* it with a reused matrix and sets how strong it is
 * with the paint's alpha. Nothing is allocated while drawing.
 *
 * The sphere's mid stop is the one thing a matrix cannot change, so a small set
 * of variants is built once, on first use, and picked from per frame.
 *
 * Each shader is used by exactly one draw per frame: on a hardware canvas a
 * shader mutated after being drawn with can leak its new matrix into the
 * earlier draw.
 */
class OrbView(context: Context) : View(context) {

    var state: Listening = Listening.Idle
        set(value) {
            if (field == value) return
            field = value
            invalidate()
        }

    var level: Float = 0f
        set(value) {
            field = value.coerceIn(0f, 1f)
        }

    private val paint = Paint(Paint.ANTI_ALIAS_FLAG)
    private val matrix = Matrix()
    private val start = System.nanoTime()

    private class Palette(val hi: Int, val body: Int, val deep: Int)

    private val awake = Palette(
        Color.rgb(94, 234, 212), Color.rgb(167, 139, 250), Color.rgb(109, 72, 206),
    )
    private val asleep = Palette(
        Color.rgb(140, 146, 166), Color.rgb(88, 94, 112), Color.rgb(56, 60, 74),
    )

    /** Unit-size shaders, one per draw, built lazily per palette. */
    private inner class Shaders(p: Palette) {
        val halo = unit(intArrayOf(p.hi, withAlpha(p.body, 0.55f), withAlpha(p.body, 0f)),
            floatArrayOf(0f, 0.45f, 1f))
        val currentA = unit(intArrayOf(p.hi, withAlpha(p.hi, 0f)), floatArrayOf(0f, 1f))
        val currentB = unit(intArrayOf(p.body, withAlpha(p.body, 0f)), floatArrayOf(0f, 1f))
        val sheen = unit(intArrayOf(Color.WHITE, withAlpha(Color.WHITE, 0f)), floatArrayOf(0f, 1f))
        val spheres = arrayOfNulls<RadialGradient>(SPHERE_STEPS)
        private val palette = p

        fun sphere(mid: Float): RadialGradient {
            val i = (((mid - MID_MIN) / (MID_MAX - MID_MIN)) * (SPHERE_STEPS - 1))
                .roundToInt().coerceIn(0, SPHERE_STEPS - 1)
            return spheres[i] ?: run {
                val m = MID_MIN + (MID_MAX - MID_MIN) * i / (SPHERE_STEPS - 1)
                unit(intArrayOf(palette.hi, palette.hi, palette.body, palette.deep),
                    floatArrayOf(0f, maxOf(0.12f, m - 0.38f), m, 1f))
                    .also { spheres[i] = it }
            }
        }
    }

    private var awakeShaders: Shaders? = null
    private var asleepShaders: Shaders? = null

    private fun shaders(muted: Boolean): Shaders =
        if (muted) asleepShaders ?: Shaders(asleep).also { asleepShaders = it }
        else awakeShaders ?: Shaders(awake).also { awakeShaders = it }

    init {
        // A floating control a screen reader can find and operate.
        contentDescription = "Amy. Double tap to open, long press to sleep or wake."
        isClickable = true
        isLongClickable = true
    }

    override fun performClick(): Boolean {
        super.performClick()
        return true
    }

    override fun onDraw(canvas: Canvas) {
        val t = ((System.nanoTime() - start) / 1_000_000_000.0).toFloat()
        val muted = state == Listening.Muted
        val synthetic = if (state == Listening.Speaking) {
            0.18f + 0.14f * abs(sin(t * 5.3f)) * abs(sin(t * 1.7f))
        } else 0f
        val lvl = if (muted) 0f else maxOf(level, synthetic)
        val sh = shaders(muted)

        val s = min(width, height).toFloat()
        val cx = width / 2f
        val cy = height / 2f
        val r = s * 0.33f * (1f + 0.07f * lvl)

        // Halo, kept inside the view so it fades rather than clipping square.
        val haloR = min(r * 1.8f, s * 0.495f)
        draw(canvas, sh.halo, cx, cy, haloR, 0.18f + 0.30f * lvl, cx, cy, haloR)

        // The sphere: highlight drifting, mid stop breathing.
        val drift = t * 0.38f
        val ox = cx + r * (-0.36f + 0.11f * cos(drift))
        val oy = cy + r * (-0.40f + 0.10f * sin(drift * 1.17f))
        val mid = (0.72f + 0.08f * sin(t * 0.52f)).coerceIn(MID_MIN, MID_MAX)
        draw(canvas, sh.sphere(mid), ox, oy, r * 1.62f, 1f, cx, cy, r)

        // Two currents running against each other, so the surface churns.
        draw(canvas, sh.currentA,
            cx + r * 0.34f * cos(-drift * 0.72f + 2.1f),
            cy + r * 0.34f * sin(-drift * 0.72f + 2.1f),
            r * (0.9f + 0.3f * lvl), 0.20f + 0.22f * lvl, cx, cy, r)
        draw(canvas, sh.currentB,
            cx + r * 0.40f * cos(drift * 1.31f + 4.2f),
            cy + r * 0.40f * sin(drift * 1.31f + 4.2f),
            r * (0.7f + 0.35f * lvl), 0.18f + 0.20f * lvl, cx, cy, r)
        draw(canvas, sh.sheen,
            cx + r * 0.42f * cos(t * 0.6f),
            cy + r * 0.42f * sin(t * 0.6f),
            r * 0.85f, 0.05f + 0.08f * lvl, cx, cy, r)

        paint.shader = null
        paint.alpha = 255

        // Full rate only while something is happening. At rest the motion is a
        // sixteen-second drift; half the frames are indistinguishable and the
        // overlay is on screen all day.
        if (lvl > 0.01f || state == Listening.Listening || state == Listening.Thinking) {
            postInvalidateOnAnimation()
        } else {
            postInvalidateDelayed(IDLE_FRAME_MS)
        }
    }

    /**
     * Position a unit gradient at (gx, gy) with radius [gr], set its strength,
     * and fill a circle of radius [cr] at (cx, cy) with it.
     */
    private fun draw(
        canvas: Canvas, shader: Shader,
        gx: Float, gy: Float, gr: Float, strength: Float,
        cx: Float, cy: Float, cr: Float,
    ) {
        val radius = gr.coerceAtLeast(1f)
        matrix.setScale(radius, radius)
        matrix.postTranslate(gx, gy)
        shader.setLocalMatrix(matrix)
        paint.shader = shader
        paint.alpha = (strength.coerceIn(0f, 1f) * 255).roundToInt()
        canvas.drawCircle(cx, cy, cr, paint)
    }

    private companion object {
        const val SPHERE_STEPS = 12
        const val MID_MIN = 0.62f
        const val MID_MAX = 0.82f
        const val IDLE_FRAME_MS = 33L

        /** A radial gradient centred on the origin with radius 1. */
        fun unit(colors: IntArray, stops: FloatArray) =
            RadialGradient(0f, 0f, 1f, colors, stops, Shader.TileMode.CLAMP)

        fun withAlpha(colour: Int, alpha: Float): Int =
            Color.argb(
                (alpha.coerceIn(0f, 1f) * 255).roundToInt(),
                Color.red(colour), Color.green(colour), Color.blue(colour),
            )
    }
}
