package io.github.edzzztech.amy

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.darkColorScheme
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.core.content.ContextCompat
import androidx.lifecycle.lifecycleScope
import io.github.edzzztech.amy.core.ActionLog
import io.github.edzzztech.amy.core.AmyState
import io.github.edzzztech.amy.core.Listening
import io.github.edzzztech.amy.core.Stt
import io.github.edzzztech.amy.core.Tts
import kotlinx.coroutines.launch

class MainActivity : ComponentActivity() {

    private lateinit var stt: Stt
    private var tts: Tts? = null
    private lateinit var actions: ActionLog

    private val requestMic = registerForActivityResult(
        ActivityResultContracts.RequestPermission()
    ) { granted ->
        if (granted) startAmy() else AmyState.setProblem("Amy needs the microphone to listen.")
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        actions = ActionLog(this)
        tts = Tts(this).also { engine ->
            awaitVoice(engine)
            engine.onSpeakStart = { AmyState.setState(Listening.Speaking) }
            engine.onSpeakDone = { AmyState.setState(Listening.Idle) }
        }

        stt = Stt(this).apply {
            onReadyForSpeech = {
                AmyState.setProblem(null)
                AmyState.setState(Listening.Listening)
            }
            onPartial = { AmyState.setHeard(it) }
            onEndOfSpeech = { AmyState.setState(Listening.Thinking) }
            onFinal = { text ->
                AmyState.setHeard(text)
                actions.record("message", "Heard: $text")
                respondTo(text)
            }
            onError = { message ->
                AmyState.setProblem(message)
                AmyState.setState(Listening.Idle)
            }
        }

        setContent { AmyTheme { Home(onTalk = ::onTalkPressed) } }

        if (ContextCompat.checkSelfPermission(this, Manifest.permission.RECORD_AUDIO)
            == PackageManager.PERMISSION_GRANTED
        ) {
            startAmy()
        } else {
            requestMic.launch(Manifest.permission.RECORD_AUDIO)
        }
    }

    /** TTS initialises asynchronously; wait for it before setting the voice. */
    private fun awaitVoice(engine: Tts) {
        lifecycleScope.launch {
            if (engine.awaitReady()) engine.setVoice()
            else AmyState.setProblem("Text to speech is unavailable on this device.")
        }
    }

    /**
     * Placeholder until the local model lands: she repeats what she heard so the
     * microphone, recogniser and voice can all be verified end to end. Replacing
     * this with [io.github.edzzztech.amy.core.LlmEngine] is the next step.
     */
    private fun respondTo(heard: String) {
        val reply = "You said: $heard"
        AmyState.setReply(reply)
        tts?.speak(reply)
    }

    private fun onTalkPressed() {
        when (AmyState.state.value) {
            Listening.Speaking -> {
                tts?.stop()                       // barge-in
                AmyState.setState(Listening.Idle)
            }
            Listening.Listening -> {
                stt.stop()
                AmyState.setState(Listening.Idle)
            }
            else -> {
                AmyState.clear()
                stt.start()
            }
        }
    }

    private fun startAmy() {
        startForegroundService(Intent(this, AmyService::class.java))
    }

    override fun onDestroy() {
        stt.stop()
        tts?.shutdown()
        tts = null
        super.onDestroy()
    }
}

// Same palette as the desktop and the site.
private val AmyColors = darkColorScheme(
    primary = Color(0xFF5EEAD4),
    secondary = Color(0xFFA78BFA),
    background = Color(0xFF07080C),
    surface = Color(0xFF0E1017),
    onBackground = Color(0xFFE8EAF2),
    onSurface = Color(0xFFE8EAF2),
)

@Composable
fun AmyTheme(content: @Composable () -> Unit) =
    MaterialTheme(colorScheme = AmyColors, content = content)

@Composable
fun Home(onTalk: () -> Unit) {
    val state by AmyState.state.collectAsState()
    val heard by AmyState.heard.collectAsState()
    val reply by AmyState.reply.collectAsState()
    val problem by AmyState.problem.collectAsState()

    Surface(modifier = Modifier.fillMaxSize(), color = MaterialTheme.colorScheme.background) {
        Column(
            modifier = Modifier
                .fillMaxSize()
                .padding(horizontal = 24.dp, vertical = 32.dp),
            horizontalAlignment = Alignment.CenterHorizontally,
        ) {
            Text(
                "AMY",
                fontSize = 15.sp,
                letterSpacing = 4.sp,
                color = MaterialTheme.colorScheme.onBackground.copy(alpha = 0.55f),
            )

            Spacer(Modifier.weight(1f))

            // The orb: the same teal-into-violet mark as the desktop.
            Box(
                modifier = Modifier
                    .size(if (state == Listening.Idle) 180.dp else 200.dp)
                    .clip(CircleShape)
                    .background(
                        Brush.radialGradient(
                            colors = listOf(Color(0xFF5EEAD4), Color(0xFFA78BFA), Color(0xFF6D48CE)),
                            radius = 420f,
                        )
                    )
            )

            Spacer(Modifier.height(26.dp))

            Text(
                when (state) {
                    Listening.Idle -> "Tap to talk"
                    Listening.Listening -> "Listening"
                    Listening.Thinking -> "Thinking"
                    Listening.Speaking -> "Speaking — tap to interrupt"
                    Listening.Muted -> "Muted"
                },
                fontSize = 13.sp,
                letterSpacing = 2.sp,
                color = MaterialTheme.colorScheme.primary,
            )

            Spacer(Modifier.height(22.dp))

            if (heard.isNotEmpty()) {
                Text(
                    heard,
                    fontSize = 17.sp,
                    textAlign = TextAlign.Center,
                    color = MaterialTheme.colorScheme.onBackground,
                )
            }
            if (reply.isNotEmpty()) {
                Spacer(Modifier.height(12.dp))
                Text(
                    reply,
                    fontSize = 15.sp,
                    textAlign = TextAlign.Center,
                    color = MaterialTheme.colorScheme.onBackground.copy(alpha = 0.65f),
                )
            }
            problem?.let {
                Spacer(Modifier.height(12.dp))
                Text(it, fontSize = 13.sp, textAlign = TextAlign.Center, color = Color(0xFFE57373))
            }

            Spacer(Modifier.weight(1f))

            Button(
                onClick = onTalk,
                modifier = Modifier.fillMaxWidth().height(52.dp),
                colors = ButtonDefaults.buttonColors(
                    containerColor = MaterialTheme.colorScheme.primary,
                    contentColor = Color(0xFF04221D),
                ),
            ) {
                Text(
                    if (state == Listening.Listening || state == Listening.Speaking) "Stop"
                    else "Talk to Amy",
                    fontSize = 15.sp,
                )
            }
        }
    }
}
