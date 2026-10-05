package io.github.edzzztech.amy.ui

import androidx.compose.animation.core.*
import androidx.compose.foundation.Canvas
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.drawscope.DrawScope
import io.github.edzzztech.amy.core.Listening
import kotlin.math.cos
import kotlin.math.min
import kotlin.math.sin

/**
 * The same mark as the desktop, drawn rather than declared.
 *
 * The desktop orb is a circle whose gradient moves: the highlight drifts, the
 * mid stop breathes, and two currents run against each other inside so the
 * surface churns instead of sliding. A static Brush cannot do that, so this
 * redraws every frame on a Canvas, exactly as amy.py does.
 *
 * The silhouette stays circular. An earlier desktop version deformed the
 * outline and it read as an amoeba; all the motion belongs inside the edge.
 */

private val Teal = Color(0xFF5EEAD4)
private val Violet = Color(0xFFA78BFA)
private val DeepViolet = Color(0xFF6D48CE)
private val Muted = Color(0xFF8C92A6)
private val MutedDeep = Color(0xFF585E70)
private val MutedShade = Color(0xFF383C4A)

@Composable
fun Orb(
    state: Listening,
    level: Float = 0f,
    modifier: Modifier = Modifier,
) {
    val clock = rememberInfiniteTransition(label = "orb")

    // One slow clock; everything else is derived from it at different rates,
    // so nothing lines up into a visible loop.
    val t by clock.animateFloat(
        initialValue = 0f,
        targetValue = (2 * Math.PI).toFloat(),
        animationSpec = infiniteRepeatable(
            animation = tween(durationMillis = 16_000, easing = LinearEasing),
            repeatMode = RepeatMode.Restart,
        ),
        label = "t",
    )

    // Speaking has no microphone level to read, so a gentle synthetic pulse
    // stands in — the same compromise the desktop makes.
    val synthetic = if (state == Listening.Speaking) {
        0.18f + 0.14f * kotlin.math.abs(sin(t * 5.3f)) * kotlin.math.abs(sin(t * 1.7f))
    } else 0f
    val muted = state == Listening.Muted
    val lvl = if (muted) 0f else maxOf(level, synthetic)

    val breathe by animateFloatAsState(
        targetValue = if (state == Listening.Idle) 0.84f else 0.92f,
        animationSpec = tween(400),
        label = "breathe",
    )

    Canvas(modifier = modifier) {
        drawOrb(
            t = t,
            level = lvl,
            scale = breathe,
            highlight = if (muted) Muted else Teal,
            body = if (muted) MutedDeep else Violet,
            deep = if (muted) MutedShade else DeepViolet,
        )
    }
}

private fun DrawScope.drawOrb(
    t: Float,
    level: Float,
    scale: Float,
    highlight: Color,
    body: Color,
    deep: Color,
) {
    val s = min(size.width, size.height)
    val cx = size.width / 2f
    val cy = size.height / 2f
    val r = s * 0.36f * (scale + 0.07f * level)

    // Halo, clamped inside the canvas so it fades out instead of being cut
    // square at the edges — a bug the desktop hit and had to fix.
    val haloR = min(r * 1.8f, s * 0.495f)
    drawCircle(
        brush = Brush.radialGradient(
            0.0f to highlight.copy(alpha = 0.18f + 0.30f * level),
            0.45f to body.copy(alpha = 0.10f + 0.18f * level),
            1.0f to body.copy(alpha = 0f),
            center = Offset(cx, cy),
            radius = haloR,
        ),
        radius = haloR,
        center = Offset(cx, cy),
    )

    // The sphere. The highlight drifts and the mid stop breathes, which is what
    // makes the gradient itself look alive rather than merely tinted.
    val drift = t * 0.38f
    val ox = cx + r * (-0.36f + 0.11f * cos(drift))
    val oy = cy + r * (-0.40f + 0.10f * sin(drift * 1.17f))
    val mid = (0.72f + 0.08f * sin(t * 0.52f)).coerceIn(0.45f, 0.92f)

    drawCircle(
        brush = Brush.radialGradient(
            0.0f to highlight,
            (mid - 0.38f).coerceAtLeast(0.12f) to highlight,
            mid to body,
            1.0f to deep,
            center = Offset(ox, oy),
            radius = r * 1.62f,
        ),
        radius = r,
        center = Offset(cx, cy),
    )

    // Two currents running against each other, so the inside churns.
    current(
        cx + r * 0.34f * cos(-drift * 0.72f + 2.1f),
        cy + r * 0.34f * sin(-drift * 0.72f + 2.1f),
        r * (0.9f + 0.3f * level), r, cx, cy,
        highlight.copy(alpha = 0.20f + 0.22f * level),
    )
    current(
        cx + r * 0.40f * cos(drift * 1.31f + 4.2f),
        cy + r * 0.40f * sin(drift * 1.31f + 4.2f),
        r * (0.7f + 0.35f * level), r, cx, cy,
        body.copy(alpha = 0.18f + 0.20f * level),
    )
    // A slow sheen, so it reads as a liquid surface rather than a flat disc.
    current(
        cx + r * 0.42f * cos(t * 0.6f),
        cy + r * 0.42f * sin(t * 0.6f),
        r * 0.85f, r, cx, cy,
        Color.White.copy(alpha = 0.05f + 0.08f * level),
    )
}

/** One soft blob of colour, clipped to the sphere by drawing at its radius. */
private fun DrawScope.current(
    x: Float, y: Float, radius: Float,
    sphereR: Float, cx: Float, cy: Float,
    colour: Color,
) {
    drawCircle(
        brush = Brush.radialGradient(
            0.0f to colour,
            1.0f to colour.copy(alpha = 0f),
            center = Offset(x, y),
            radius = radius.coerceAtLeast(1f),
        ),
        radius = sphereR,
        center = Offset(cx, cy),
    )
}
