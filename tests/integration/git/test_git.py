from catcher.core.git import commit_managed, pull, push

AUTHOR = ("idea-catcher", "bot@example.com")


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

    (work / "output/notes").mkdir(parents=True)
    (work / "output/notes/x.md").write_text("x\n")
    commit_managed(work, ["output"], "stage", author=AUTHOR)
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
