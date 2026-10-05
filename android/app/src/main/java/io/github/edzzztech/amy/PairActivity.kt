package io.github.edzzztech.amy

import android.content.Context
import android.content.Intent
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import io.github.edzzztech.amy.core.Amy
import io.github.edzzztech.amy.ui.Icon
import io.github.edzzztech.amy.ui.Sym
import kotlinx.coroutines.launch

/**
 * Pairing with the desktop: an address and a code, both shown by Amy on the
 * computer when you say "pair my phone".
 *
 * No account and no server — the phone talks straight to the PC over your own
 * network, and the desktop refuses anything that is not a local address.
 */
class PairActivity : ComponentActivity() {

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()
        Amy.start(this)
        setContent { AmyTheme { PairScreen(onClose = { finish() }) } }
    }

    companion object {
        fun open(context: Context) {
            context.startActivity(Intent(context, PairActivity::class.java))
        }
    }
}

private val Muted = Color(0xFF8C92A6)
private val Line = Color(0x1AFFFFFF)

@Composable
private fun PairScreen(onClose: () -> Unit) {
    val scope = rememberCoroutineScope()
    var address by remember { mutableStateOf(Amy.desktop.address) }
    var code by remember { mutableStateOf(Amy.desktop.code) }
    var result by remember { mutableStateOf<String?>(null) }
    var checking by remember { mutableStateOf(false) }

    Surface(Modifier.fillMaxSize(), color = MaterialTheme.colorScheme.background) {
        Column(
            Modifier
                .fillMaxSize()
                .statusBarsPadding()
                .verticalScroll(rememberScrollState())
                .padding(horizontal = 22.dp),
        ) {
            Row(
                Modifier.fillMaxWidth().padding(vertical = 10.dp),
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Box(
                    Modifier
                        .clip(CircleShape)
                        .clickable(role = Role.Button, onClick = onClose)
                        .padding(10.dp),
                ) { Icon(Sym.Close, Muted, size = 18.dp, label = "Close") }
                Spacer(Modifier.width(6.dp))
                Text("Pair with your computer", fontSize = 17.sp,
                    color = MaterialTheme.colorScheme.onBackground)
            }

            Spacer(Modifier.height(10.dp))
            Text(
                "On the computer, say \"Amy, pair my phone\". She will show an " +
                    "address and a code. Type both here.",
                fontSize = 14.sp,
                lineHeight = 21.sp,
                color = Muted,
            )

            Spacer(Modifier.height(26.dp))
            Field("Address", address, "192.168.1.24:8765") { address = it }
            Spacer(Modifier.height(14.dp))
            Field("Pairing code", code, "the code she shows you") { code = it }

            Spacer(Modifier.height(22.dp))
            Button(
                onClick = {
                    Amy.desktop.address = address
                    Amy.desktop.code = code
                    checking = true
                    result = null
                    scope.launch {
                        val status = Amy.desktop.status()
                        result = if (status.reachable) {
                            "Connected. Try saying \"on my PC, open Chrome\"."
                        } else {
                            status.detail
                        }
                        checking = false
                    }
                },
                enabled = !checking && address.isNotBlank() && code.isNotBlank(),
                modifier = Modifier.fillMaxWidth().height(50.dp),
                shape = RoundedCornerShape(26.dp),
                colors = ButtonDefaults.buttonColors(
                    containerColor = MaterialTheme.colorScheme.primary,
                    contentColor = Color(0xFF04221D),
                ),
            ) {
                Text(if (checking) "Checking..." else "Save and test", fontSize = 15.sp)
            }

            result?.let {
                Spacer(Modifier.height(16.dp))
                Text(it, fontSize = 14.sp, lineHeight = 21.sp,
                    color = MaterialTheme.colorScheme.onBackground)
            }

            if (Amy.desktop.isPaired) {
                Spacer(Modifier.height(10.dp))
                Text(
                    "Forget this computer",
                    fontSize = 13.sp,
                    color = Color(0xFFE57373),
                    modifier = Modifier
                        .clip(RoundedCornerShape(10.dp))
                        .clickable {
                            Amy.desktop.forget()
                            address = ""; code = ""; result = "Forgotten."
                        }
                        .padding(vertical = 10.dp, horizontal = 4.dp),
                )
            }

            Spacer(Modifier.height(30.dp))
            HorizontalDivider(color = Line)
            Spacer(Modifier.height(18.dp))
            Text("Worth knowing", fontSize = 12.sp, letterSpacing = 1.5.sp,
                color = MaterialTheme.colorScheme.primary)
            Spacer(Modifier.height(10.dp))
            Text(
                "Both devices must be on the same network. Anyone on that network " +
                    "who has the code can send commands to your computer, so treat it " +
                    "like a password. Every command the computer accepts is written to " +
                    "its action log. If the PC is off, Amy answers on the phone instead.",
                fontSize = 13.sp,
                lineHeight = 20.sp,
                color = Muted,
            )
            Spacer(Modifier.height(40.dp))
        }
    }
}

@Composable
private fun Field(label: String, value: String, hint: String, onChange: (String) -> Unit) {
    Column {
        Text(label, fontSize = 12.sp, color = Muted)
        Spacer(Modifier.height(6.dp))
        OutlinedTextField(
            value = value,
            onValueChange = onChange,
            placeholder = { Text(hint, color = Muted, fontSize = 14.sp) },
            singleLine = true,
            shape = RoundedCornerShape(14.dp),
            modifier = Modifier.fillMaxWidth(),
            colors = OutlinedTextFieldDefaults.colors(
                focusedBorderColor = MaterialTheme.colorScheme.primary,
                unfocusedBorderColor = Line,
                focusedTextColor = MaterialTheme.colorScheme.onBackground,
                unfocusedTextColor = MaterialTheme.colorScheme.onBackground,
            ),
        )
    }
}
