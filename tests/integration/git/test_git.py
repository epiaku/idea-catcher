from catcher.core.git import commit_paths, pull, push

AUTHOR = ("idea-catcher", "bot@example.com")


def test_commit_paths_leaves_unrelated_changes_alone(make_repo, sh):
    _, work = make_repo(
        "docs", {"hugo/content/en/docs/idea-bucket/clipping/old.md": "old\n", "other.md": "x\n"}
    )
    (work / "hugo/content/en/docs/idea-bucket/clipping/old.md").unlink()
    (work / "other.md").write_text("changed\n")
    (work / "staged-by-user.md").write_text("u\n")
    sh(work, "add", "staged-by-user.md")
    new_page = work / "hugo/content/en/docs/idea-bucket/notes/20260927_a7b2c9_x.md"
    new_page.parent.mkdir(parents=True)
    new_page.write_text("page\n")

    assert commit_paths(work, [new_page], "idea-catcher: publish 1 page(s)", author=AUTHOR)

    assert sh(work, "show", "--name-only", "--format=", "HEAD").splitlines() == [
        "hugo/content/en/docs/idea-bucket/notes/20260927_a7b2c9_x.md"
    ]
    status = sh(work, "status", "--porcelain").splitlines()
    assert " D hugo/content/en/docs/idea-bucket/clipping/old.md" in status
    assert " M other.md" in status
    assert "A  staged-by-user.md" in status
    assert sh(work, "log", "-1", "--format=%an <%ae>").strip() == "idea-catcher <bot@example.com>"


def test_commit_paths_records_deletions_and_skips_unknown_paths(make_repo, sh):
    _, work = make_repo("ideas", {"inbox/notes/a note.md": "hi\n"})
    (work / "inbox/notes/a note.md").unlink()
    staged = work / "staging/a7b2c9.md"
    staged.parent.mkdir()
    staged.write_text("staged\n")
    ghost = work / "inbox/notes/never committed.md"
    assert commit_paths(work, [work / "inbox/notes/a note.md", staged, ghost], "stage", author=AUTHOR)
    assert sh(work, "show", "--name-status", "--format=", "HEAD").splitlines() == [
        "D\tinbox/notes/a note.md",
        "A\tstaging/a7b2c9.md",
    ]


def test_nothing_to_commit_returns_false(make_repo):
    _, work = make_repo("ideas", {})
    assert commit_paths(work, [work / "README.md"], "noop", author=AUTHOR) is False
    assert commit_paths(work, [], "noop", author=AUTHOR) is False


def test_pull_and_push_do_nothing_without_a_remote(tmp_path, sh):
    sh(tmp_path, "init", "-b", "main", "local")
    pull(tmp_path / "local")
    push(tmp_path / "local")


def test_push_rebases_over_a_phone_push(make_repo, sh, tmp_path):
    bare, work = make_repo("ideas", {})
    phone = tmp_path / "phone"
    sh(tmp_path, "clone", str(bare), str(phone))
    (phone / "inbox").mkdir()
    (phone / "inbox/new.md").write_text("from the phone\n")
    sh(phone, "add", "-A")
    sh(phone, "commit", "-m", "phone")
    sh(phone, "push")

    (work / "staging").mkdir()
    (work / "staging/x.md").write_text("x\n")
    commit_paths(work, [work / "staging/x.md"], "stage", author=AUTHOR)
    push(work)

    assert sh(bare, "log", "--format=%s", "main").splitlines() == ["stage", "phone", "seed"]


def test_pull_brings_in_remote_changes(make_repo, sh, tmp_path):
    bare, work = make_repo("docs", {})
    other = tmp_path / "other"
    sh(tmp_path, "clone", str(bare), str(other))
    (other / "new.md").write_text("n\n")
    sh(other, "add", "-A")
    sh(other, "commit", "-m", "other")
    sh(other, "push")
    (work / "README.md").write_text("dirty local change\n")
    pull(work)
    assert (work / "new.md").exists()
    assert (work / "README.md").read_text() == "dirty local change\n"
