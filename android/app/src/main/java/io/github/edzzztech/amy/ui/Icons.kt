package io.github.edzzztech.amy.ui

import androidx.compose.foundation.Canvas
import androidx.compose.foundation.layout.size
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.Path
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.graphics.StrokeJoin
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp

/**
 * Drawn icons on a 24-unit grid, same as the desktop's camera bar.
 *
 * Not emoji — those ignore the theme and render differently on every device —
 * and not a Material icon dependency, which would pull in thousands of vectors
 * to use six. Each one is a few strokes that inherit the colour they are given.
 */
enum class Sym { Attach, Camera, Orb, Mic, Send, Stop, Menu, Sleep }

@Composable
fun Icon(sym: Sym, tint: Color, size: Dp = 20.dp, modifier: Modifier = Modifier) {
    Canvas(modifier = modifier.size(size)) {
        val u = this.size.minDimension / 24f          // one grid unit
        val stroke = Stroke(
            width = 1.7f * u,
            cap = StrokeCap.Round,
            join = StrokeJoin.Round,
        )
        when (sym) {
            Sym.Attach -> {
                // Paperclip: the hook, drawn as a rounded open path.
                val p = Path().apply {
                    moveTo(17f * u, 9f * u)
                    lineTo(9.5f * u, 16.5f * u)
                    cubicTo(8.2f * u, 17.8f * u, 6.2f * u, 17.8f * u, 5f * u, 16.5f * u)
                    cubicTo(3.7f * u, 15.3f * u, 3.7f * u, 13.3f * u, 5f * u, 12f * u)
                    lineTo(13.5f * u, 3.5f * u)
                    cubicTo(14.3f * u, 2.7f * u, 15.7f * u, 2.7f * u, 16.5f * u, 3.5f * u)
                    cubicTo(17.3f * u, 4.3f * u, 17.3f * u, 5.7f * u, 16.5f * u, 6.5f * u)
                    lineTo(8f * u, 15f * u)
                    cubicTo(7.6f * u, 15.4f * u, 7f * u, 15.4f * u, 6.6f * u, 15f * u)
                    cubicTo(6.2f * u, 14.6f * u, 6.2f * u, 14f * u, 6.6f * u, 13.6f * u)
                    lineTo(14f * u, 6.2f * u)
                }
                drawPath(p, tint, style = stroke)
            }

            Sym.Camera -> {
                val body = Path().apply {
                    moveTo(3.5f * u, 8.5f * u)
                    lineTo(7.4f * u, 8.5f * u)
                    lineTo(8.5f * u, 6.5f * u)
                    lineTo(15.5f * u, 6.5f * u)
                    lineTo(16.6f * u, 8.5f * u)
                    lineTo(20.5f * u, 8.5f * u)
                    lineTo(20.5f * u, 18.5f * u)
                    lineTo(3.5f * u, 18.5f * u)
                    close()
                }
                drawPath(body, tint, style = stroke)
                drawCircle(tint, radius = 3.1f * u, center = Offset(12f * u, 13f * u), style = stroke)
            }

            Sym.Orb -> {
                drawCircle(tint, radius = 7f * u, center = Offset(12f * u, 12f * u), style = stroke)
                drawCircle(tint, radius = 2.6f * u, center = Offset(10f * u, 10f * u))
            }

            Sym.Mic -> {
                val cap = Path().apply {
                    moveTo(12f * u, 3.2f * u)
                    cubicTo(13.6f * u, 3.2f * u, 15f * u, 4.6f * u, 15f * u, 6.2f * u)
                    lineTo(15f * u, 12f * u)
                    cubicTo(15f * u, 13.6f * u, 13.6f * u, 15f * u, 12f * u, 15f * u)
                    cubicTo(10.4f * u, 15f * u, 9f * u, 13.6f * u, 9f * u, 12f * u)
                    lineTo(9f * u, 6.2f * u)
                    cubicTo(9f * u, 4.6f * u, 10.4f * u, 3.2f * u, 12f * u, 3.2f * u)
                    close()
                }
                drawPath(cap, tint, style = stroke)
                val arc = Path().apply {
                    moveTo(5.5f * u, 11.5f * u)
                    cubicTo(5.5f * u, 17f * u, 18.5f * u, 17f * u, 18.5f * u, 11.5f * u)
                }
                drawPath(arc, tint, style = stroke)
                drawLine(tint, Offset(12f * u, 18f * u), Offset(12f * u, 21f * u),
                    strokeWidth = stroke.width, cap = StrokeCap.Round)
            }

            Sym.Send -> {
                drawLine(tint, Offset(12f * u, 19f * u), Offset(12f * u, 5f * u),
                    strokeWidth = stroke.width, cap = StrokeCap.Round)
                val head = Path().apply {
                    moveTo(6f * u, 11f * u)
                    lineTo(12f * u, 5f * u)
                    lineTo(18f * u, 11f * u)
                }
                drawPath(head, tint, style = stroke)
            }

            Sym.Stop -> {
                val sq = Path().apply {
                    moveTo(7.5f * u, 7.5f * u)
                    lineTo(16.5f * u, 7.5f * u)
                    lineTo(16.5f * u, 16.5f * u)
                    lineTo(7.5f * u, 16.5f * u)
                    close()
                }
                drawPath(sq, tint, style = stroke)
            }

            Sym.Menu -> {
                listOf(7f, 12f, 17f).forEach { y ->
                    drawLine(tint, Offset(4f * u, y * u), Offset(20f * u, y * u),
                        strokeWidth = stroke.width, cap = StrokeCap.Round)
                }
            }

            Sym.Sleep -> {
                // Crescent: her asleep state.
                val moon = Path().apply {
                    moveTo(19f * u, 14.5f * u)
                    cubicTo(13.5f * u, 16.5f * u, 7.5f * u, 12.5f * u, 9f * u, 6.5f * u)
                    cubicTo(4.5f * u, 9f * u, 5.5f * u, 17f * u, 11.5f * u, 18.5f * u)
                    cubicTo(15f * u, 19.3f * u, 18f * u, 17.5f * u, 19f * u, 14.5f * u)
                    close()
                }
                drawPath(moon, tint, style = stroke)
            }
        }
    }
}
