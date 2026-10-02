from catcher.modules.pipeline.context import CONTEXT_FILE, load_context


def test_the_text_without_its_comments_is_the_context(tmp_path):
    path = tmp_path / "ctx.md"
    path.write_text("<!--\nnotes for me,\nnot sent\n-->\n\n- We make demos.\n<!-- inline --> - Small team.\n")
    assert load_context(path) == "- We make demos.\n - Small team."


def test_a_missing_or_empty_file_is_no_context(tmp_path):
    assert load_context(tmp_path / "nope.md") == ""
    (tmp_path / "only-comment.md").write_text("<!-- nothing yet -->\n")
    assert load_context(tmp_path / "only-comment.md") == ""


def test_the_committed_context_has_text_and_no_comment_left():
    text = load_context(CONTEXT_FILE)
    assert text and "<!--" not in text and "-->" not in text
