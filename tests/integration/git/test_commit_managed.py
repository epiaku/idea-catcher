"""`commit_managed`: commit everything under the managed folders (new, changed, deleted), nothing else."""

from catcher.core.git import commit_managed

AUTHOR = ("idea-catcher", "bot@example.com")
MANAGED = ["inbox", "archive", "output", "failed", "duplicates", "facts", "llm"]


def test_commit_managed_commits_only_the_given_folders(make_repo, sh):
    _, work = make_repo("ideas", {"inbox/notes/a.md": "a\n", "notes.txt": "mine\n"})
    (work / "inbox/notes/a.md").write_text("a, edited\n")
    (work / "output/notes").mkdir(parents=True)
    (work / "output/notes/b.md").write_text("b\n")
    (work / "notes.txt").write_text("mine, edited\n")
    (work / "staged-by-user.md").write_text("u\n")
    sh(work, "add", "staged-by-user.md")

    assert commit_managed(work, MANAGED, "idea-catcher: publish", author=AUTHOR) is True

    assert sorted(sh(work, "show", "--name-status", "--format=", "HEAD").splitlines()) == [
        "A\toutput/notes/b.md",
        "M\tinbox/notes/a.md",
    ]
    status = sh(work, "status", "--porcelain").splitlines()
    assert " M notes.txt" in status
    assert "A  staged-by-user.md" in status
    assert len(status) == 2
    assert sh(work, "log", "-1", "--format=%an <%ae>|%s").strip() == (
        "idea-catcher <bot@example.com>|idea-catcher: publish"
    )


def test_commit_managed_stages_deletions_and_moves(make_repo, sh):
    _, work = make_repo("ideas", {"inbox/notes/a note.md": "a note\n", "inbox/clippings/c.md": "clip\n"})
    (work / "archive/notes").mkdir(parents=True)
    (work / "inbox/notes/a note.md").rename(work / "archive/notes/a note.md")
    (work / "inbox/clippings/c.md").unlink()
    (work / "inbox/clippings").rmdir()

    assert commit_managed(work, MANAGED, "move", author=AUTHOR) is True

    assert sorted(sh(work, "show", "--name-status", "--no-renames", "--format=", "HEAD").splitlines()) == [
        "A\tarchive/notes/a note.md",
        "D\tinbox/clippings/c.md",
        "D\tinbox/notes/a note.md",
    ]
    assert sh(work, "status", "--porcelain").strip() == ""


def test_commit_managed_stages_a_managed_folder_that_is_gone_from_disk(make_repo, sh):
    _, work = make_repo("ideas", {"failed/x.md": "x\n"})
    (work / "failed/x.md").unlink()
    (work / "failed").rmdir()

    assert commit_managed(work, MANAGED, "gone", author=AUTHOR) is True
    assert sh(work, "show", "--name-status", "--format=", "HEAD").splitlines() == ["D\tfailed/x.md"]


def test_commit_managed_returns_false_when_nothing_changed(make_repo, sh):
    _, work = make_repo("ideas", {"inbox/notes/a.md": "a\n"})
    (work / "elsewhere.md").write_text("not managed\n")
    head = sh(work, "rev-parse", "HEAD")

    assert commit_managed(work, MANAGED, "noop", author=AUTHOR) is False
    assert commit_managed(work, [], "noop", author=AUTHOR) is False
    assert commit_managed(work, ["no-such-folder"], "noop", author=AUTHOR) is False
    assert sh(work, "rev-parse", "HEAD") == head
    assert sh(work, "status", "--porcelain").splitlines() == ["?? elsewhere.md"]


def test_an_emptied_inbox_does_not_break_the_next_commit(make_repo, sh):
    _, work = make_repo("ideas", {"inbox/notes/n.md": "n\n"})
    (work / "archive/notes").mkdir(parents=True)
    (work / "inbox/notes/n.md").rename(work / "archive/notes/n.md")
    assert commit_managed(work, MANAGED, "process", author=AUTHOR) is True
    assert (work / "inbox/notes").is_dir()  # Stage A never removes the emptied folder

    (work / "llm").mkdir()
    (work / "llm/t.json").write_text("{}\n")
    assert commit_managed(work, MANAGED, "trace", author=AUTHOR) is True
    assert sh(work, "show", "--name-only", "--format=", "HEAD").splitlines() == ["llm/t.json"]
    assert sh(work, "status", "--porcelain") == ""


def test_an_empty_managed_sub_folder_is_skipped(make_repo, sh):
    _, work = make_repo("ideas", {})
    (work / "failed/notes").mkdir(parents=True)
    (work / "output/notes").mkdir(parents=True)
    (work / "output/notes/p.md").write_text("p\n")
    assert commit_managed(work, MANAGED, "output", author=AUTHOR) is True
    assert sh(work, "show", "--name-only", "--format=", "HEAD").splitlines() == ["output/notes/p.md"]


def test_a_folder_with_only_ignored_files_is_skipped(make_repo, sh):
    _, work = make_repo("ideas", {".gitignore": "*.tmp\n"})
    (work / "duplicates").mkdir()
    (work / "duplicates/half.tmp").write_text("x\n")
    (work / "facts").mkdir()
    (work / "facts/v.json").write_text("{}\n")
    assert commit_managed(work, MANAGED, "facts", author=AUTHOR) is True
    assert sh(work, "show", "--name-only", "--format=", "HEAD").splitlines() == ["facts/v.json"]
