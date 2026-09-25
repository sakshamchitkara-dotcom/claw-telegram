from claw_telegram.formatting import TG_LIMIT, render, split_markdown, to_html


def test_escapes_html_and_formats_subset():
    out = to_html("# Title\n**bold** <script> & *it* `a<b` [x](https://e.com)")
    assert out == ('<b>Title</b>\n<b>bold</b> &lt;script&gt; &amp; <i>it</i> <code>a&lt;b</code> '
                   '<a href="https://e.com">x</a>')


def test_code_fence_not_formatted():
    out = to_html("before\n```python\nx = 2 * 3 * 4 **y**\n```\nafter")
    assert '<pre><code class="language-python">x = 2 * 3 * 4 **y**</code></pre>' in out
    assert out.startswith("before") and out.endswith("after")


def test_unterminated_fence_and_snake_case():
    assert to_html("```\nopen") == "<pre>open</pre>"
    assert to_html("my_var_name and 2*3*4") == "my_var_name and 2*3*4"


def test_javascript_links_are_not_linked():
    assert "<a" not in to_html("[x](javascript:alert(1))")


def test_split_respects_limit_and_reopens_fences():
    md = "intro\n```\n" + "\n".join(f"line {i}" for i in range(2000)) + "\n```\nend"
    chunks = split_markdown(md, 1000)
    assert len(chunks) > 5
    assert all(len(c) <= 1000 for c in chunks)
    assert all(c.count("```") % 2 == 0 for c in chunks)


def test_render_long_text_under_limit():
    md = ("<>&" * 3000) + "\n" + ("word " * 3000)
    msgs = render(md)
    assert all(0 < len(m) <= TG_LIMIT for m in msgs)
    assert len(msgs) >= 3
