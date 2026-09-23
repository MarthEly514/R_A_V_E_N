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
