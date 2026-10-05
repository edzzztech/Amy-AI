package io.github.edzzztech.amy

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.core.content.ContextCompat
import androidx.lifecycle.lifecycleScope
import io.github.edzzztech.amy.core.*
import kotlinx.coroutines.launch

class MainActivity : ComponentActivity() {

    private lateinit var stt: Stt
    private var tts: Tts? = null
    private lateinit var actions: ActionLog
    private lateinit var conversations: Conversations

    private val requestMic = registerForActivityResult(
        ActivityResultContracts.RequestPermission()
    ) { granted ->
        if (granted) startAmy() else AmyState.setProblem("Amy needs the microphone to listen.")
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        actions = ActionLog(this)
        conversations = Conversations(this)
        refreshHistory()

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
                AmyState.setHeard("")
                submit(text, spoken = true)
            }
            onError = { message ->
                AmyState.setProblem(message)
                AmyState.setState(Listening.Idle)
            }
        }

        setContent {
            AmyTheme {
                Screen(
                    onTalk = ::onTalkPressed,
                    onSend = { submit(it, spoken = false) },
                    onNewConversation = ::newConversation,
                    onOpenConversation = ::openConversation,
                )
            }
        }

        if (ContextCompat.checkSelfPermission(this, Manifest.permission.RECORD_AUDIO)
            == PackageManager.PERMISSION_GRANTED
        ) {
            startAmy()
        } else {
            requestMic.launch(Manifest.permission.RECORD_AUDIO)
        }
    }

    /** One path for typed and spoken input, so both are recorded identically. */
    private fun submit(text: String, spoken: Boolean) {
        val clean = text.trim()
        if (clean.isEmpty()) return
        conversations.append("you", clean)
        AmyState.setTurns(conversations.turns())
        actions.record("message", "${if (spoken) "Said" else "Typed"}: $clean")

        // Placeholder until the local model lands — see respondTo().
        val reply = respondTo(clean)
        conversations.append("amy", reply)
        AmyState.setTurns(conversations.turns())
        refreshHistory()
        if (spoken) tts?.speak(reply) else AmyState.setState(Listening.Idle)
    }

    /**
     * Stands in for [LlmEngine] so the input, storage, history and voice can be
     * exercised end to end before inference lands. Replacing this is next.
     */
    private fun respondTo(heard: String): String = "You said: $heard"

    private fun newConversation() {
        conversations.start()
        AmyState.setTurns(emptyList())
        AmyState.clear()
    }

    private fun openConversation(id: String) {
        conversations.load(id)?.let { AmyState.setTurns(it.turns) }
    }

    private fun refreshHistory() = AmyState.setHistory(conversations.list())

    private fun awaitVoice(engine: Tts) {
        lifecycleScope.launch {
            if (engine.awaitReady()) engine.setVoice()
            else AmyState.setProblem("Text to speech is unavailable on this device.")
        }
    }

    private fun onTalkPressed() {
        when (AmyState.state.value) {
            Listening.Speaking -> {
                tts?.stop()
                AmyState.setState(Listening.Idle)
            }
            Listening.Listening -> {
                stt.stop()
                AmyState.setState(Listening.Idle)
            }
            else -> {
                AmyState.setProblem(null)
                stt.start()
            }
        }
    }

    private fun startAmy() = startForegroundService(Intent(this, AmyService::class.java))

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

private val Line = Color(0x1AFFFFFF)
private val Muted = Color(0xFF8C92A6)

@Composable
fun AmyTheme(content: @Composable () -> Unit) =
    MaterialTheme(colorScheme = AmyColors, content = content)

@Composable
fun Screen(
    onTalk: () -> Unit,
    onSend: (String) -> Unit,
    onNewConversation: () -> Unit,
    onOpenConversation: (String) -> Unit,
) {
    val drawerState = rememberDrawerState(DrawerValue.Closed)
    val scope = rememberCoroutineScope()
    val history by AmyState.history.collectAsState()

    ModalNavigationDrawer(
        drawerState = drawerState,
        drawerContent = {
            HistoryDrawer(
                history = history,
                onNew = {
                    onNewConversation()
                    scope.launch { drawerState.close() }
                },
                onOpen = { id ->
                    onOpenConversation(id)
                    scope.launch { drawerState.close() }
                },
            )
        },
    ) {
        Home(
            onTalk = onTalk,
            onSend = onSend,
            onOpenDrawer = { scope.launch { drawerState.open() } },
        )
    }
}

@Composable
private fun HistoryDrawer(
    history: List<Conversation>,
    onNew: () -> Unit,
    onOpen: (String) -> Unit,
) {
    ModalDrawerSheet(drawerContainerColor = MaterialTheme.colorScheme.surface) {
        Column(Modifier.padding(horizontal = 18.dp, vertical = 22.dp)) {
            Text(
                "CONVERSATIONS",
                fontSize = 11.sp,
                letterSpacing = 2.sp,
                color = Muted,
            )
            Spacer(Modifier.height(16.dp))
            Text(
                "+  New conversation",
                fontSize = 14.sp,
                color = MaterialTheme.colorScheme.primary,
                modifier = Modifier
                    .fillMaxWidth()
                    .clickable(onClick = onNew)
                    .padding(vertical = 10.dp),
            )
        }
        HorizontalDivider(color = Line)
        if (history.isEmpty()) {
            Text(
                "Nothing saved yet.",
                fontSize = 13.sp,
                color = Muted,
                modifier = Modifier.padding(18.dp),
            )
        } else {
            LazyColumn {
                items(history, key = { it.id }) { conversation ->
                    Column(
                        Modifier
                            .fillMaxWidth()
                            .clickable { onOpen(conversation.id) }
                            .padding(horizontal = 18.dp, vertical = 12.dp)
                    ) {
                        Text(
                            conversation.title,
                            fontSize = 14.sp,
                            color = MaterialTheme.colorScheme.onSurface,
                            maxLines = 1,
                        )
                        Spacer(Modifier.height(3.dp))
                        Text(
                            "${conversation.turns.size} messages",
                            fontSize = 11.sp,
                            color = Muted,
                        )
                    }
                }
            }
        }
    }
}

@Composable
private fun Home(
    onTalk: () -> Unit,
    onSend: (String) -> Unit,
    onOpenDrawer: () -> Unit,
) {
    val state by AmyState.state.collectAsState()
    val heard by AmyState.heard.collectAsState()
    val turns by AmyState.turns.collectAsState()
    val problem by AmyState.problem.collectAsState()
    var draft by remember { mutableStateOf("") }

    Surface(Modifier.fillMaxSize(), color = MaterialTheme.colorScheme.background) {
        Column(Modifier.fillMaxSize()) {

            // Top bar: the side tab lives here.
            Row(
                Modifier
                    .fillMaxWidth()
                    .padding(horizontal = 16.dp, vertical = 14.dp),
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Text(
                    "☰",
                    fontSize = 20.sp,
                    color = Muted,
                    modifier = Modifier
                        .clickable(onClick = onOpenDrawer)
                        .padding(horizontal = 6.dp, vertical = 2.dp),
                )
                Spacer(Modifier.width(12.dp))
                Text("AMY", fontSize = 13.sp, letterSpacing = 4.sp, color = Muted)
            }

            // Middle: the orb, centred, with the conversation under it.
            Column(
                modifier = Modifier
                    .weight(1f)
                    .fillMaxWidth()
                    .verticalScroll(rememberScrollState())
                    .padding(horizontal = 22.dp),
                horizontalAlignment = Alignment.CenterHorizontally,
            ) {
                Spacer(Modifier.height(24.dp))
                Orb(active = state != Listening.Idle)
                Spacer(Modifier.height(18.dp))
                Text(
                    when (state) {
                        Listening.Idle -> "Tap the mic, or type below"
                        Listening.Listening -> "Listening"
                        Listening.Thinking -> "Thinking"
                        Listening.Speaking -> "Speaking — tap to interrupt"
                        Listening.Muted -> "Muted"
                    },
                    fontSize = 12.sp,
                    letterSpacing = 1.5.sp,
                    color = MaterialTheme.colorScheme.primary,
                )

                if (heard.isNotEmpty()) {
                    Spacer(Modifier.height(14.dp))
                    Text(heard, fontSize = 16.sp, textAlign = TextAlign.Center, color = Muted)
                }
                problem?.let {
                    Spacer(Modifier.height(12.dp))
                    Text(it, fontSize = 13.sp, textAlign = TextAlign.Center, color = Color(0xFFE57373))
                }

                Spacer(Modifier.height(26.dp))
                turns.forEach { turn ->
                    Column(Modifier.fillMaxWidth().padding(bottom = 14.dp)) {
                        Text(
                            if (turn.role == "you") "you" else "amy",
                            fontSize = 10.sp,
                            letterSpacing = 1.5.sp,
                            color = if (turn.role == "you") Muted else MaterialTheme.colorScheme.primary,
                        )
                        Spacer(Modifier.height(4.dp))
                        Text(
                            turn.text,
                            fontSize = 15.sp,
                            color = MaterialTheme.colorScheme.onBackground,
                        )
                    }
                }
                Spacer(Modifier.height(16.dp))
            }

            HorizontalDivider(color = Line)

            // Bottom: type or talk.
            Row(
                Modifier
                    .fillMaxWidth()
                    .padding(horizontal = 12.dp, vertical = 10.dp),
                verticalAlignment = Alignment.CenterVertically,
            ) {
                OutlinedTextField(
                    value = draft,
                    onValueChange = { draft = it },
                    placeholder = { Text("Ask Amy anything…", color = Muted, fontSize = 14.sp) },
                    singleLine = true,
                    shape = RoundedCornerShape(12.dp),
                    modifier = Modifier.weight(1f),
                    keyboardOptions = KeyboardOptions(imeAction = ImeAction.Send),
                    keyboardActions = KeyboardActions(onSend = {
                        onSend(draft)
                        draft = ""
                    }),
                    colors = OutlinedTextFieldDefaults.colors(
                        focusedBorderColor = MaterialTheme.colorScheme.primary,
                        unfocusedBorderColor = Line,
                        focusedTextColor = MaterialTheme.colorScheme.onBackground,
                        unfocusedTextColor = MaterialTheme.colorScheme.onBackground,
                    ),
                )
                Spacer(Modifier.width(8.dp))
                Box(
                    Modifier
                        .size(48.dp)
                        .clip(RoundedCornerShape(12.dp))
                        .background(MaterialTheme.colorScheme.primary)
                        .clickable {
                            if (draft.isNotBlank()) {
                                onSend(draft)
                                draft = ""
                            } else {
                                onTalk()
                            }
                        },
                    contentAlignment = Alignment.Center,
                ) {
                    Text(
                        if (draft.isNotBlank()) "↑" else "●",
                        fontSize = 18.sp,
                        color = Color(0xFF04221D),
                    )
                }
            }
        }
    }
}

@Composable
private fun Orb(active: Boolean) {
    Box(
        Modifier
            .size(if (active) 172.dp else 156.dp)
            .clip(CircleShape)
            .background(
                Brush.radialGradient(
                    colors = listOf(
                        Color(0xFF5EEAD4),
                        Color(0xFFA78BFA),
                        Color(0xFF6D48CE),
                    ),
                    radius = 380f,
                )
            )
    )
}
