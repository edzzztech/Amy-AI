package io.github.edzzztech.amy.ui

import androidx.compose.animation.core.*
import androidx.compose.foundation.Canvas
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import kotlin.math.cos
import kotlin.math.sin

/**
 * The drifting background from the desktop: a cool near-black base with two
 * slow accent blooms moving across it.
 *
 * Deliberately very low contrast. It should register as depth rather than as
 * decoration — if you notice it moving, it is too strong.
 */
@Composable
fun DriftingBackground(modifier: Modifier = Modifier, active: Boolean = true) {
    val clock = rememberInfiniteTransition(label = "bg")
    val t by clock.animateFloat(
        initialValue = 0f,
        targetValue = (2 * Math.PI).toFloat(),
        animationSpec = infiniteRepeatable(
            animation = tween(durationMillis = 48_000, easing = LinearEasing),
            repeatMode = RepeatMode.Restart,
        ),
        label = "bgT",
    )
    val drift = if (active) t else 0f

    Canvas(modifier = modifier) {
        // Base: near-black lifting slightly towards the bottom, like the desktop.
        drawRect(
            brush = Brush.verticalGradient(
                colors = listOf(Color(0xFF07080C), Color(0xFF141826)),
            ),
            size = Size(size.width, size.height),
        )

        bloom(
            cx = size.width * (0.5f + 0.38f * sin(drift * 0.31f)),
            cy = size.height * (0.32f + 0.26f * cos(drift * 0.23f)),
            radius = size.minDimension * 0.85f,
            colour = Color(0xFF5EEAD4),
            alpha = 0.10f,
        )
        bloom(
            cx = size.width * (0.5f + 0.34f * cos(drift * 0.19f + 2.1f)),
            cy = size.height * (0.66f + 0.24f * sin(drift * 0.27f + 1.3f)),
            radius = size.minDimension * 0.95f,
            colour = Color(0xFFA78BFA),
            alpha = 0.09f,
        )
    }
}

private fun androidx.compose.ui.graphics.drawscope.DrawScope.bloom(
    cx: Float,
    cy: Float,
    radius: Float,
    colour: Color,
    alpha: Float,
) {
    drawRect(
        brush = Brush.radialGradient(
            colors = listOf(colour.copy(alpha = alpha), colour.copy(alpha = 0f)),
            center = Offset(cx, cy),
            radius = radius,
        ),
    )
}
