package io.github.edzzztech.amy.core

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import java.net.HttpURLConnection
import java.net.URL
import java.net.URLDecoder
import java.net.URLEncoder

/**
 * Looking things up on the web, for questions a model on the phone cannot
 * know the answer to: today's news, prices, scores, the weather.
 *
 * DuckDuckGo rather than Google: Google's results cannot be read by an app
 * without a paid API key, and DuckDuckGo neither needs one nor keeps a
 * profile of who asked. Wikipedia fills in when DuckDuckGo has nothing, and
 * the weather comes from wttr.in. Only the words being searched for are sent:
 * nothing about the phone, the conversation or who you are.
 *
 * Every call blocks on the network: call off the main thread.
 */
class WebSearch {

    data class Result(val title: String, val snippet: String, val url: String) {
        /** "bbc.co.uk", for saying where an answer came from. */
        val site: String
            get() = url.substringAfter("://").substringBefore('/').removePrefix("www.")
    }

    /** What a search found, ready to hand to the model, or a finished answer. */
    sealed interface Found {
        data class Results(val results: List<Result>) : Found
        /** Complete in itself, such as the weather: said as it is. */
        data class Answer(val text: String, val source: String) : Found
        data object Nothing : Found
        data object Offline : Found
    }

    fun lookUp(query: String): Found {
        // The weather as it is now; if that service is down, an ordinary search.
        weatherPlace(query)?.let { place -> weather(place)?.let { return Found.Answer(it, "wttr.in") } }
        var reached = false
        val results = mutableListOf<Result>()
        get(INSTANT + enc(query))?.let { body ->
            reached = true
            instantAnswer(body)?.let { results += it }
        }
        get(HTML + enc(query))?.let { body ->
            reached = true
            results += duckDuckGo(body)
        }
        if (results.size < 2) {
            get(WIKIPEDIA + enc(query))?.let { body ->
                reached = true
                results += wikipedia(body)
            }
        }
        val unique = results.distinctBy { it.url }.take(MAX_RESULTS)
        return when {
            unique.isNotEmpty() -> Found.Results(unique)
            reached -> Found.Nothing
            else -> Found.Offline
        }
    }

    private fun weather(place: String): String? {
        val where = if (place.isBlank()) "" else enc(place).replace("+", "%20")
        // As a command-line tool: to a browser, wttr.in sends a web page.
        return get("https://wttr.in/$where?format=$WEATHER_FORMAT&m", agent = "curl/8.5")
            ?.trim()
            ?.takeIf { it.isNotEmpty() && !it.startsWith("Unknown location", ignoreCase = true) && '<' !in it }
            ?.let(::spokenWeather)
    }

    private fun get(url: String, agent: String = USER_AGENT): String? = try {
        val conn = URL(url).openConnection() as HttpURLConnection
        conn.connectTimeout = TIMEOUT_MS
        conn.readTimeout = TIMEOUT_MS
        conn.setRequestProperty("User-Agent", agent)
        conn.setRequestProperty("Accept-Language", "en-GB,en;q=0.8")
        try {
            if (conn.responseCode == 200) {
                conn.inputStream.bufferedReader().use { r ->
                    val out = StringBuilder()
                    val buf = CharArray(8192)
                    while (out.length < MAX_PAGE_CHARS) {
                        val n = r.read(buf)
                        if (n < 0) break
                        out.append(buf, 0, n)
                    }
                    out.toString()
                }
            } else null
        } finally {
            conn.disconnect()
        }
    } catch (e: Exception) {
        null
    }

    companion object {
        private const val TIMEOUT_MS = 8_000
        private const val MAX_PAGE_CHARS = 400_000
        private const val MAX_RESULTS = 5
        private const val USER_AGENT =
            "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 (KHTML, like Gecko) " +
                "Chrome/130.0 Mobile Safari/537.36"

        private const val INSTANT = "https://api.duckduckgo.com/?format=json&no_html=1&skip_disambig=1&q="
        private const val HTML = "https://html.duckduckgo.com/html/?q="
        private const val WIKIPEDIA = "https://en.wikipedia.org/w/rest.php/v1/search/page?limit=3&q="
        private const val WEATHER_FORMAT = "%l:+%C,+%t+(feels+like+%f),+wind+%w,+humidity+%h"

        private val json = Json { ignoreUnknownKeys = true }

        private fun enc(s: String) = URLEncoder.encode(s, "UTF-8")

        // --- when to search ------------------------------------------------------

        /**
         * What to search for, when she is asked outright: "google the
         * population of Peru", "search for flights to Rome", "look up
         * Marie Curie".
         */
        fun asked(text: String): String? {
            val m = ASKED.find(text.trim()) ?: return null
            return m.groupValues[1].trim().trimEnd('.', '?', '!').trim().ifEmpty { null }
        }

        /**
         * Whether a question is about something that changes - news, prices,
         * scores, the weather - which a model on the phone cannot know, since
         * it only knows what it was trained on.
         */
        fun needsFreshAnswer(text: String): Boolean {
            val lower = text.trim().lowercase()
            if (!QUESTION.containsMatchIn(lower)) return false
            return FRESH.containsMatchIn(lower)
        }

        /**
         * The place a weather question is about: "" for here, a name for
         * "weather in Paris", or null if it is not about the weather.
         */
        internal fun weatherPlace(query: String): String? {
            val lower = query.lowercase()
            if (!WEATHER.containsMatchIn(lower)) return null
            val place = WEATHER_PLACE.find(lower)?.groupValues?.get(1)?.trim().orEmpty()
            return place.replace(WEATHER_FILLER, "").trim(' ', '?', '.', '!')
        }

        // --- reading what came back ----------------------------------------------

        /** DuckDuckGo's own short answer, when it has one (mostly Wikipedia). */
        internal fun instantAnswer(body: String): Result? = try {
            val o = json.parseToJsonElement(body).jsonObject
            val text = o.str("AbstractText").ifEmpty { o.str("Answer") }
            val url = o.str("AbstractURL")
            if (text.isBlank()) null
            else Result(o.str("Heading").ifEmpty { "Answer" }, text, url.ifEmpty { "https://duckduckgo.com" })
        } catch (e: Exception) {
            null
        }

        /** The results page: each result's title, link and summary. */
        internal fun duckDuckGo(html: String): List<Result> {
            val titles = DDG_TITLE.findAll(html).toList()
            val snippets = DDG_SNIPPET.findAll(html).map { it.groupValues[1] to it.groupValues[2] }.toList()
            return titles.mapNotNull { m ->
                val url = realUrl(m.groupValues[1]) ?: return@mapNotNull null
                // Adverts link through DuckDuckGo's ad redirect; they are not results.
                if ("duckduckgo.com/y.js" in url || "ad_domain" in m.groupValues[1]) return@mapNotNull null
                val snippet = snippets.firstOrNull { realUrl(it.first) == url }?.second.orEmpty()
                Result(plain(m.groupValues[2]), plain(snippet), url)
            }.filter { it.title.isNotBlank() }
        }

        internal fun wikipedia(body: String): List<Result> = try {
            json.parseToJsonElement(body).jsonObject["pages"]!!.jsonArray.map { p ->
                val page = p.jsonObject
                val title = page.str("title")
                val text = listOf(page.str("description"), plain(page.str("excerpt")))
                    .filter { it.isNotBlank() }.joinToString(". ")
                Result(title, text, "https://en.wikipedia.org/wiki/" + page.str("key"))
            }
        } catch (e: Exception) {
            emptyList()
        }

        /** DuckDuckGo wraps each link in a redirect; the real address is in uddg. */
        private fun realUrl(href: String): String? {
            val h = href.replace("&amp;", "&")
            val wrapped = Regex("[?&]uddg=([^&]+)").find(h)?.groupValues?.get(1)
            val url = wrapped?.let { URLDecoder.decode(it, "UTF-8") }
                ?: if (h.startsWith("//")) "https:$h" else h
            return url.takeIf { it.startsWith("http") }
        }

        /** Markup out, entities decoded, spaces tidied. */
        internal fun plain(html: String): String =
            html.replace(TAGS, "")
                .replace("&amp;", "&").replace("&quot;", "\"").replace("&#x27;", "'")
                .replace("&#39;", "'").replace("&lt;", "<").replace("&gt;", ">")
                .replace("&nbsp;", " ")
                .replace(SPACES, " ")
                .trim()

        /** "+21°C" and "→9km/h" read badly aloud; this says them properly. */
        internal fun spokenWeather(raw: String): String =
            raw.replace(Regex("[^\\p{L}\\p{N}\\s,.:%()°+\\-/]"), "")
                .replace(Regex("\\+(\\d)"), "$1")
                .replace("°C", " degrees")
                .replace("km/h", " kilometres an hour")
                .replace(Regex(" {2,}"), " ")
                .replace(" ,", ",")
                .trim()

        // --- the prompt ------------------------------------------------------------

        /**
         * The question with what the search found, sized to fit [room]
         * characters. Tells the model to stay with the results: a small model
         * will otherwise mix them with what it half-remembers.
         */
        fun prompt(question: String, results: List<Result>, room: Int): String {
            val head = "Web search results:\n"
            val tail = "\nAnswer using only these results, in one or two sentences. " +
                "If they do not answer it, say so.\nQuestion: $question"
            val budget = (room - head.length - tail.length).coerceAtLeast(200)
            val per = budget / results.size.coerceAtLeast(1)
            val body = results.mapIndexed { i, r ->
                "${i + 1}. ${r.title} (${r.site}): ${r.snippet}".take(per.coerceAtLeast(80))
            }.joinToString("\n")
            return head + body.take(budget) + tail
        }

        private fun JsonObject.str(key: String): String =
            runCatching { this[key]?.jsonPrimitive?.content }.getOrNull().orEmpty()

        // --- patterns ----------------------------------------------------------------

        private val ASKED = Regex(
            "^(?:(?:can|could|would) you |please )?(?:" +
                "google|bing|search up|search (?:the web|online|the internet|google|the net)(?: for)?|" +
                "search for|look up|look online for|look (?:it|that) up(?: online)?:?|" +
                "find (?:me )?(?:online|on the web)|check online(?: for)?|web search(?: for)?" +
                ")\\s+(.+)",
            RegexOption.IGNORE_CASE,
        )

        private val QUESTION = Regex(
            "\\?|^(?:what|what's|whats|who|who's|when|where|which|how|is|are|was|were|did|does|do|will|has|have|tell me|give me|any)\\b",
        )

        private val FRESH = Regex(
            "\\b(?:latest|news|headlines?|breaking|today'?s|tonight'?s?|this (?:week|weekend|month|year)|" +
                "right now|currently|current|at the moment|score|scores|fixtures?|results? of|who won|winning|" +
                "price of|cost of|stock|share price|exchange rate|bitcoin|weather|forecast|temperature outside|" +
                "rain(?:ing)? (?:today|tomorrow)|release date|come out|coming out|open (?:today|now)|" +
                "opening hours|election|20(?:2[5-9]|3\\d))\\b",
        )

        private val WEATHER = Regex("\\b(?:weather|forecast|temperature outside|raining|going to rain|is it (?:cold|hot|warm|sunny))\\b")
        private val WEATHER_PLACE = Regex("\\b(?:in|for|at)\\s+([a-z][a-z .'-]{1,40})")
        private val WEATHER_FILLER = Regex("\\b(?:today|tonight|tomorrow|right now|now|(?:this|the) (?:morning|afternoon|evening|week|weekend|moment)|like)\\b")

        private val DDG_TITLE = Regex("""class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>""", RegexOption.DOT_MATCHES_ALL)
        private val DDG_SNIPPET = Regex("""class="result__snippet"[^>]*href="([^"]+)"[^>]*>(.*?)</a>""", RegexOption.DOT_MATCHES_ALL)
        private val TAGS = Regex("<[^>]+>")
        private val SPACES = Regex("\\s+")
    }
}
