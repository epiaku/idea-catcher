import os
from pathlib import Path

from catcher.modules.backfill.scan import page_ids, scan_docs
from catcher.modules.youtube.urls import find_youtube_urls

A, B, C = "aaaaaaaaaaa", "bbbbbbbbbbb", "ccccccccccc"


def _write(root: Path, name: str, text: str) -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_the_url_forms_all_give_one_id(tmp_path):
    forms = [
        f"https://www.youtube.com/watch?v={A}&t=30s",
        f"https://youtu.be/{A}?si=x",
        f"https://www.youtube.com/shorts/{A}",
        f"https://m.youtube.com/watch?v={A}",
        f"https://www.youtube.com/embed/{A}",
        f"https://www.youtube.com/live/{A}",
    ]
    _write(tmp_path, "a.md", "\n".join(f"- {form}" for form in forms))
    result = scan_docs(tmp_path)
    assert result.found == {A: "a.md"}
    assert len(find_youtube_urls("\n".join(forms))) == 6


def test_a_playlist_or_channel_link_is_not_a_video(tmp_path):
    _write(
        tmp_path, "a.md", "https://www.youtube.com/playlist?list=PL123\nhttps://www.youtube.com/@someone\n"
    )
    assert scan_docs(tmp_path).found == {}


def test_the_same_video_in_many_pages_is_one_row_with_the_first_page(tmp_path):
    _write(tmp_path, "b/z.md", f"https://youtu.be/{A}")
    _write(tmp_path, "a/y.md", f"https://youtu.be/{A}")
    result = scan_docs(tmp_path)
    assert result.found == {A: "a/y.md"}
    assert result.files == 2


def test_frontmatter_source_and_body_links_are_both_found(tmp_path):
    _write(
        tmp_path,
        "a.md",
        f'---\nsource: "https://www.youtube.com/watch?v={A}"\n---\nsee [x](https://youtu.be/{B})\n',
    )
    assert set(scan_docs(tmp_path).found) == {A, B}


def test_git_and_symlinks_out_of_the_repo_are_not_read(tmp_path):
    repo = tmp_path / "repo"
    outside = tmp_path / "outside"
    _write(repo, ".git/x.md", f"https://youtu.be/{A}")
    _write(outside, "o.md", f"https://youtu.be/{B}")
    _write(repo, "inside/i.md", f"https://youtu.be/{C}")
    os.symlink(outside, repo / "link-dir")
    os.symlink(outside / "o.md", repo / "link.md")
    os.symlink(repo / "inside", repo / "link-in")
    assert set(scan_docs(repo).found) == {C}


def test_a_file_with_broken_frontmatter_or_bad_bytes_is_skipped_and_counted(tmp_path):
    _write(tmp_path, "broken.md", f"---\nsource: [unclosed\n---\nhttps://youtu.be/{A}\n")
    (tmp_path / "bytes.md").write_bytes(b"caf\xe9 https://youtu.be/" + B.encode() + b"\n")
    _write(tmp_path, "ok.md", f"https://youtu.be/{C}")
    result = scan_docs(tmp_path)
    assert result.unreadable == 1
    assert set(result.found) == {B, C}
    assert result.files == 3


def test_page_ids_reads_video_id_and_the_gemini_base_id(tmp_path):
    _write(tmp_path, "a.md", f"---\nvideo_id: {A}\n---\nbody")
    _write(tmp_path, "b.md", f"---\nid: {B}-gemini\n---\nbody")
    _write(tmp_path, "c.md", f"---\nid: some-note\n---\nhttps://youtu.be/{C}")
    _write(tmp_path, "d.md", "---\nbroken: [\n---\n")
    assert page_ids(tmp_path) == {A, B}
