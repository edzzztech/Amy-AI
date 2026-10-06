package io.github.edzzztech.amy.core

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class WebSearchTest {

    // --- when she searches ------------------------------------------------------

    @Test
    fun explicitSearchesKeepJustTheQuery() {
        assertEquals("the population of Peru", WebSearch.asked("Google the population of Peru"))
        assertEquals("flights to Rome", WebSearch.asked("search for flights to Rome?"))
        assertEquals("Marie Curie", WebSearch.asked("could you look up Marie Curie"))
        assertEquals("best pizza near me", WebSearch.asked("search the web for best pizza near me"))
        assertEquals("tide times whitby", WebSearch.asked("look it up online: tide times whitby"))
        assertNull(WebSearch.asked("what's the time"))
        assertNull(WebSearch.asked("google"))
        // "Look up" in the middle of a sentence is not a request to search.
        assertNull(WebSearch.asked("I need to look up to the sky"))
    }

    @Test
    fun questionsAboutThingsThatChangeGoToTheWeb() {
        for (q in listOf(
            "What's the latest news?",
            "who won the match last night",
            "what's the weather like in Paris",
            "what is the price of bitcoin",
            "Is it going to rain tomorrow?",
            "when does the new Zelda come out",
            "who won the 2026 world cup",
        )) assertTrue(q, WebSearch.needsFreshAnswer(q))
    }

    @Test
    fun ordinaryChatStaysOnThePhone() {
        for (q in listOf(
            "write me a poem about the news",          // not a question
            "what is the capital of France",
            "how do I boil an egg",
            "tell me a joke",
            "open Spotify",
        )) assertFalse(q, WebSearch.needsFreshAnswer(q))
    }

    @Test
    fun weatherPlaces() {
        assertEquals("", WebSearch.weatherPlace("what's the weather"))
        assertEquals("", WebSearch.weatherPlace("what's the weather like today"))
        assertEquals("paris", WebSearch.weatherPlace("what's the weather like in Paris"))
        assertEquals("new york", WebSearch.weatherPlace("weather forecast for New York tomorrow"))
        assertNull(WebSearch.weatherPlace("who won the match"))
    }

    // --- reading results ------------------------------------------------------------

    @Test
    fun resultsPageGivesTitlesRealLinksAndSnippets() {
        val results = WebSearch.duckDuckGo(DDG_PAGE)
        assertEquals(2, results.size)
        assertEquals("2025 UEFA Champions League final - Wikipedia", results[0].title)
        assertEquals("https://en.wikipedia.org/wiki/2025_UEFA_Champions_League_Final", results[0].url)
        assertEquals("en.wikipedia.org", results[0].site)
        assertEquals("Paris Saint-Germain won 5–0 against Inter Milan & claimed the title.", results[0].snippet)
        assertEquals("uefa.com", results[1].site)
        assertEquals("", results[1].snippet)
    }

    @Test
    fun instantAnswerAndWikipedia() {
        val instant = WebSearch.instantAnswer(
            """{"Heading":"Eiffel Tower","AbstractText":"The Eiffel Tower is a lattice tower in Paris.","AbstractURL":"https://en.wikipedia.org/wiki/Eiffel_Tower"}"""
        )!!
        assertEquals("Eiffel Tower", instant.title)
        assertNull(WebSearch.instantAnswer("""{"Heading":"","AbstractText":""}"""))
        assertNull(WebSearch.instantAnswer("not json"))

        val wiki = WebSearch.wikipedia(
            """{"pages":[{"key":"Eiffel_Tower","title":"Eiffel Tower","excerpt":"The <span class=\"searchmatch\">Eiffel</span> Tower is a lattice tower","description":"Tower in Paris, France"}]}"""
        )
        assertEquals(1, wiki.size)
        assertEquals("Tower in Paris, France. The Eiffel Tower is a lattice tower", wiki[0].snippet)
        assertEquals("https://en.wikipedia.org/wiki/Eiffel_Tower", wiki[0].url)
    }

    @Test
    fun weatherIsSaidProperly() {
        assertEquals(
            "Leeds, England, GB: Patchy rain nearby, 21 degrees (feels like 20 degrees), " +
                "wind 9 kilometres an hour, humidity 51%",
            WebSearch.spokenWeather(
                "Leeds, England, GB: Patchy rain nearby, +21°C (feels like +20°C), wind →9km/h, humidity 51%"
            ),
        )
        assertEquals("Oslo: Clear, -3 degrees", WebSearch.spokenWeather("Oslo: Clear, -3°C"))
        // As wttr.in really sends it, with a space before the comma.
        assertEquals("Paris: Cloudy, 24 degrees", WebSearch.spokenWeather("Paris: Cloudy , +24°C"))
    }

    @Test
    fun promptFitsTheRoomAndKeepsTheQuestion() {
        val results = List(5) { WebSearch.Result("Title $it", "x".repeat(2000), "https://site$it.com/a") }
        val prompt = WebSearch.prompt("who won?", results, room = 1500)
        assertTrue(prompt.length <= 1500)
        assertTrue(prompt.endsWith("Question: who won?"))
        assertTrue("site4.com" in prompt)
    }

    private companion object {
        // Trimmed from a real results page: an advert, then two results, the
        // second without a summary.
        val DDG_PAGE = """
            <div class="result results_links results_links_deep result--ad ">
              <h2 class="result__title"><a rel="nofollow" class="result__a" href="https://duckduckgo.com/y.js?ad_domain=example.com&amp;u3=x">Buy tickets now</a></h2>
              <a class="result__snippet" href="https://duckduckgo.com/y.js?ad_domain=example.com">Cheap!</a>
            </div>
            <div class="result results_links results_links_deep web-result ">
              <h2 class="result__title">
                <a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fen.wikipedia.org%2Fwiki%2F2025_UEFA_Champions_League_Final&amp;rut=ef91">2025 UEFA Champions League final - Wikipedia</a>
              </h2>
              <a class="result__snippet" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fen.wikipedia.org%2Fwiki%2F2025_UEFA_Champions_League_Final&amp;rut=ef91">Paris Saint-Germain <b>won</b> 5–0 against Inter Milan &amp; claimed the title.</a>
            </div>
            <div class="result results_links results_links_deep web-result ">
              <h2 class="result__title">
                <a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.uefa.com%2Fuefachampionsleague%2Fhistory%2F&amp;rut=546b">Finals | History | UEFA.com</a>
              </h2>
            </div>
        """.trimIndent()
    }
}
