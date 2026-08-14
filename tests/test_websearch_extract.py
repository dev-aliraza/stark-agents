"""HTML to readable text: the part that decides whether "summarize it" can work at all.

Pure functions over HTML strings, so all of this runs with no network and no browser.
"""

from __future__ import annotations

import pytest

from stark.tools.websearch.extraction import DEFAULT_MAX_CHARS, extract

PAGE = """
<!doctype html>
<html>
  <head>
    <title>Top 10 Destinations in the UAE</title>
    <meta name="description" content="A guide to the best places to visit.">
    <style>body { color: red }</style>
    <script>window.analytics = 1;</script>
  </head>
  <body>
    <nav><a href="/home">Home</a><a href="/about">About</a></nav>
    <header><h1 class="site">TravelSite</h1></header>
    <main>
      <h1>Top 10 Destinations in the UAE</h1>
      <p>The UAE draws visitors for its cities and its desert.</p>
      <h2>1. Burj Khalifa</h2>
      <p>The tallest building in the world, at 828 metres.</p>
      <ul><li>Open daily</li><li>Book ahead</li></ul>
      <p>See the <a href="/guides/dubai">Dubai guide</a> for more.</p>
    </main>
    <footer><a href="/privacy">Privacy</a></footer>
  </body>
</html>
"""


@pytest.fixture()
def document():
    return extract(PAGE, url="https://example.com/uae")


# --- the readable text ----------------------------------------------------------------


def test_title_comes_from_the_title_tag(document):
    assert document.title == "Top 10 Destinations in the UAE"


def test_meta_description_is_kept(document):
    assert document.description == "A guide to the best places to visit."


def test_body_text_survives(document):
    assert "The tallest building in the world, at 828 metres." in document.text
    assert "The UAE draws visitors" in document.text


def test_scripts_and_styles_are_dropped(document):
    assert "window.analytics" not in document.text
    assert "color: red" not in document.text


def test_page_furniture_is_dropped(document):
    """Nav, header and footer are what separate an article from a page."""
    assert "Privacy" not in document.text
    assert "About" not in document.text
    assert "TravelSite" not in document.text


def test_headings_keep_their_level(document):
    assert "# Top 10 Destinations in the UAE" in document.text
    assert "## 1. Burj Khalifa" in document.text


def test_list_items_become_bullets(document):
    assert "- Open daily" in document.text
    assert "- Book ahead" in document.text


def test_blocks_do_not_run_together(document):
    """Without block breaks the whole page arrives as one unreadable line."""
    assert "desert.The tallest" not in document.text
    assert "\n" in document.text


def test_whitespace_is_collapsed(document):
    assert "  " not in document.text
    assert "\n\n\n" not in document.text


# --- links ----------------------------------------------------------------------------


def test_links_are_collected_and_made_absolute(document):
    urls = {link.url for link in document.links}
    assert "https://example.com/guides/dubai" in urls


def test_link_text_is_kept_for_choosing(document):
    dubai = next(link for link in document.links if link.url.endswith("/guides/dubai"))
    assert dubai.text == "Dubai guide"


def test_links_inside_dropped_furniture_are_not_collected(document):
    assert all("privacy" not in link.url for link in document.links)


def test_dead_links_are_skipped():
    html = """
    <a href="#top">Top</a>
    <a href="javascript:void(0)">Menu</a>
    <a href="mailto:x@example.com">Mail</a>
    <a href="https://example.org/real">Real</a>
    """
    links = extract(html, url="https://example.com").links
    assert [link.url for link in links] == ["https://example.org/real"]


def test_duplicate_links_are_collapsed():
    html = '<a href="/a">One</a><a href="/a">One again</a><a href="/b">Two</a>'
    links = extract(html, url="https://example.com").links
    assert [link.url for link in links] == ["https://example.com/a", "https://example.com/b"]


def test_relative_links_need_a_base_url():
    """With no base, a relative href cannot be resolved, so it is dropped rather than guessed."""
    assert extract('<a href="/a">One</a>').links == []


# --- limits and robustness -------------------------------------------------------------


def test_text_is_truncated_with_a_marker():
    html = f"<p>{'word ' * 5000}</p>"
    document = extract(html, max_chars=200)

    assert document.truncated is True
    assert len(document.text) < 400
    assert "truncated at 200 characters" in document.text


def test_untruncated_text_is_not_flagged(document):
    assert document.truncated is False
    assert "truncated" not in document.text


def test_malformed_markup_does_not_raise():
    """A hostile or half-downloaded page must degrade, not fail the tool call."""
    document = extract("<p>unclosed <div><span>tags <a href='/x'>and a link", url="https://e.com")
    assert "unclosed" in document.text


def test_empty_html_gives_an_empty_document():
    document = extract("", url="https://example.com")
    assert document.text == ""
    assert document.title == ""
    assert document.links == []


def test_a_javascript_shell_yields_no_text():
    """The signal that tells the model to retry with a real browser."""
    html = '<html><body><div id="root"></div><script>render()</script></body></html>'
    document = extract(html, url="https://example.com")
    assert document.text == ""
    # Both passes found nothing, so there was nothing to recover.
    assert document.recovered is False


# --- recovering from over-aggressive stripping ------------------------------------------

ARTICLE = "The UAE has seven emirates and a great many places worth visiting. " * 8


def test_a_page_wrapped_entirely_in_a_form_keeps_its_content():
    """ASP.NET and several CMS templates do exactly this.

    Measured against a real travel site: dropping <form> as furniture discarded the whole
    20,000-character article.
    """
    html = f"<html><body><form id='aspnetForm'><h1>UAE</h1><p>{ARTICLE}</p></form></body></html>"
    document = extract(html, url="https://example.com/uae")

    assert "seven emirates" in document.text
    assert document.title == "UAE"


def test_an_article_hidden_in_an_aside_is_recovered():
    """Stripping is a heuristic; when it removes everything, it was the wrong heuristic."""
    html = f"<html><body><aside><h1>Guide</h1><p>{ARTICLE}</p></aside></body></html>"
    document = extract(html, url="https://example.com")

    assert "seven emirates" in document.text
    assert document.recovered is True


def test_an_unclosed_footer_does_not_swallow_the_page():
    html = f"<html><body><footer>Legal<p>{ARTICLE}</p></body></html>"
    assert "seven emirates" in extract(html, url="https://example.com").text


def test_stripping_still_applies_when_the_page_has_real_content(document):
    """Recovery is a fallback, not a replacement — furniture still goes when text remains."""
    assert document.recovered is False
    assert "Privacy" not in document.text


def test_recovery_keeps_whichever_pass_found_more():
    """A page that is genuinely almost empty must not gain text it never had."""
    document = extract("<body><p>Short.</p></body>", url="https://example.com")
    assert document.text == "Short."
    assert document.recovered is False


def test_title_falls_back_to_the_first_heading():
    document = extract("<body><h1>Just A Heading</h1><p>Body.</p></body>")
    assert document.title == "Just A Heading"


def test_entities_are_decoded():
    assert "Dubai & Abu Dhabi" in extract("<p>Dubai &amp; Abu Dhabi</p>").text


# --- the payload a tool returns --------------------------------------------------------


def test_payload_has_what_the_model_needs(document):
    payload = document.as_payload()
    assert payload["url"] == "https://example.com/uae"
    assert payload["title"] == "Top 10 Destinations in the UAE"
    assert "828 metres" in payload["text"]
    assert payload["links"][0]["url"].startswith("https://example.com/")


def test_payload_caps_the_link_list():
    html = "".join(f'<a href="/p{index}">Page {index}</a>' for index in range(100))
    payload = extract(html, url="https://example.com").as_payload(max_links=5)

    assert len(payload["links"]) == 5
    assert payload["links_omitted"] == 95


def test_payload_omits_absent_fields():
    payload = extract("<p>Text.</p>").as_payload()
    assert "description" not in payload
    assert "truncated" not in payload
    assert "links_omitted" not in payload


def test_the_default_cap_matches_the_file_tools():
    """One number for "how much text a model should get in one call"."""
    from stark.tools.file import MAX_READ_CHARS

    assert DEFAULT_MAX_CHARS == MAX_READ_CHARS
