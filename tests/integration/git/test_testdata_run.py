from catcher.core.frontmatter import load
from catcher.core.testdata import reset_test_repos
from catcher.modules.pipeline.run import RunOptions, run_pipeline


def test_the_whole_pipeline_on_the_committed_test_data(tmp_path, make_services, sh):
    """A regression test on real captures: notes, chats clipped several times, YouTube clips, an artifact."""
    repos = reset_test_repos(tmp_path / "ic")
    ideas, docs = repos["idea-bucket"], repos["epiaku-docs"]
    report = run_pipeline(ideas, docs, RunOptions(profile="fake"), make_services())

    counts = report.counts()
    assert counts.get("failed", 0) == 0 and not report.unreadable and not report.not_found
    assert counts["artifact"] == 1
    assert counts["duplicate"] > 0  # the chat that was clipped several times as it grew
    assert counts["published"] > 0
    assert counts.get("deferred", 0) > 0  # YouTube facts cannot be fetched in tests, so those stall

    assert not [p for p in (ideas / "inbox").rglob("*") if p.is_file()]  # everything was worked on
    [archived_pdf] = list((ideas / "archive/artifacts").iterdir())
    assert archived_pdf.name.endswith("-sample-report.pdf")
    assert (docs / "idea-bucket/artifacts" / archived_pdf.name).exists()
    assert list((ideas / "duplicates/clippings").glob("*.md"))

    [web_clip_page] = list(
        (docs / "hugo/content/en/docs/idea-bucket/web-clips").glob("*hugo-shortcodes-explained.md")
    )
    assert (
        load(web_clip_page).fm["id"]
        and load(web_clip_page).fm["source"] == "https://gohugo.io/content-management/shortcodes/"
    )

    stalled = [p for p in (ideas / "output").rglob("*.md") if load(p).fm.get("stage") == "deferred"]
    assert len(stalled) == counts["deferred"]
    assert sh(ideas, "status", "--porcelain") == "" and sh(docs, "status", "--porcelain") == ""
