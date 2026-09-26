"""raven/browser.py — against the local fixture page (tests/fixtures/test_form.html).

No network: file:// only. Uses the real (headless) Chromium, session-scoped
via the `browser_page`/`fresh_page` fixtures so it's paid once per run, not
once per test. This is the one file in the suite that's inherently slower —
still no `live` marker, since it never touches the network or a real model.
"""
from raven import browser


def test_navigate_and_read(fresh_page):
    result = fresh_page.read()
    assert "Test Form Page" in result
    assert "This is a test page" in result


def test_click_an_ordinary_button_changes_the_page(fresh_page):
    assert "Hidden content revealed" not in fresh_page.read()
    result = fresh_page.click("Show More")
    assert result == "Clicked 'Show More'."
    assert "Hidden content revealed!" in fresh_page.read()


def test_click_refuses_a_real_submit_control(fresh_page):
    """The enforced (not just instructed) safety backstop: a literal
    <button type=submit> must be refused by browser_click and redirected to
    browser_submit — and the form must genuinely NOT have been submitted."""
    result = fresh_page.click("Submit Form")
    assert "use browser_submit" in result
    assert "Form was submitted" not in fresh_page.read()


def test_type_into_a_field(fresh_page):
    result = fresh_page.type_text("Your Name", "Ely")
    assert result == "Typed into 'Your Name'."


def test_type_into_an_unknown_field(fresh_page):
    result = fresh_page.type_text("Not A Real Field", "x")
    assert "No input field matches" in result


def test_click_an_unknown_element(fresh_page):
    result = fresh_page.click("Not A Real Button")
    assert "No visible element matches" in result


def test_submit_actually_submits(fresh_page):
    """browser_submit is the confirmed path — verify it genuinely completes
    the action (a real DOM change), not just that it returns without error."""
    fresh_page.type_text("Your Name", "Ely")
    result = fresh_page.submit("Submit Form")
    assert "Submitted" in result
    assert "Form was submitted with: Ely" in fresh_page.read()


def test_submit_unknown_element(fresh_page):
    result = fresh_page.submit("Not A Real Button")
    assert "No visible element matches" in result


# ---------------------------------------------------------------------------
# screenshot (V2)
#
# Same deliberate gap already stated for _read()'s "No page loaded yet"
# branch (Step 3.24): the shared session fixture always navigates before
# first use, so that branch is never actually hit here, and closing the
# shared browser mid-suite just to hit one string would break the "paid
# once per run" fixture this whole file relies on. Not worth it for one
# string, consistent with the earlier call on the same tradeoff.
# ---------------------------------------------------------------------------

def test_screenshot_saves_a_real_png_file(fresh_page):
    import os
    path = fresh_page.screenshot()
    assert os.path.isfile(path)
    assert os.path.getsize(path) > 0
    with open(path, "rb") as f:
        assert f.read(8) == b"\x89PNG\r\n\x1a\n"  # real PNG magic bytes -- not just a placeholder
    os.remove(path)
