from fake_telegram import check_html

from claw_telegram.formatting import render


def test_fake_rejects_bad_html():
    assert check_html("<b>ok</b> <code>x</code>") is None
    assert check_html("<b>open") is not None
    assert check_html("<div>x</div>") is not None


def test_formatter_output_is_accepted_by_fake():
    md = "# H\n**b** *i* `c` <tag> & [l](https://x.y)\n```py\n<b>not a tag</b>\n```\n~~s~~ **unclosed"
    for msg in render(md * 50):
        assert check_html(msg) is None, msg[:200]
