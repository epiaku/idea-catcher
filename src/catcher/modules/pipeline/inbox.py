import json
import logging
import re
import secrets
import shutil
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from catcher.core.files import write_atomic
from catcher.core.frontmatter import Doc, FrontmatterError, dump, load
from catcher.modules.pipeline.doctypes import DocType, derive_id, detect

log = logging.getLogger("catcher.inbox")

MAX_NAME = 128  # the longest file name we derive from a document name, sidecars and .error.txt included
_LONGEST_SUFFIX = ".youtube.json"
MAX_STEM = MAX_NAME - len(_LONGEST_SUFFIX)
ARTIFACTS_DIR = "artifacts"  # archive/artifacts/ and <epiaku-docs>/idea-bucket/artifacts/
_NAMED = re.compile(r"^\d{8}-[0-9a-f]{6}-")  # a name that already has its date and guid
_CALCULATED_NAME = re.compile(r"^\d{8}-[0-9a-f]{6}-[a-z0-9-]+\.md$")  # exactly what `calculated_stem` makes
_JUNK = frozenset({"thumbs.db", "desktop.ini"})
_ILLEGAL = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
STAGE_FOLDERS = ("inbox", "output", "archive", "failed", "duplicates")
ORIGINAL_KEY = "original_filename"
CALCULATED_KEY = "calculated_filename"

STAGE_ANALYZED = "analyzed"  # the working copy in output/ while the document is being worked on
STAGE_DEFERRED = "deferred"  # an error stalled it (LLM down, budget used up): overwritten by the next run

_TURN = re.compile(r"^\*\*You\*\*[ \t]*$", re.MULTILINE)


@dataclass
class Note:
    """One document in `inbox/`, read and analysed in memory. Nothing is written for it until it is done."""

    doc_id: str
    doctype: DocType
    doc: Doc
    path: Path  # the inbox file
    name: str | None = None  # the calculated file name (with .md) once it has one
    original: str | None = None  # the file name it had when it was captured
    inbox_rel: Path | None = None  # the path under `inbox/`, fixed when the inbox was scanned

    @property
    def original_name(self) -> str:
        return self.original or self.path.name

    @property
    def target_rel(self) -> Path:
        """The path used in archive/, output/, failed/ and duplicates/: subfolder plus calculated name."""
        return self.rel.parent / self.name if self.name else self.rel

    def output_path(self, ideas_repo: Path) -> Path:
        return ideas_repo / "output" / self.target_rel

    @property
    def rel(self) -> Path:
        """The path under `inbox/`, for example `clippings/New chat.md`. Every folder uses the same path."""
        if self.inbox_rel is not None:
            return self.inbox_rel
        root = inbox_root(self.path)  # a document read from anywhere (`reason`, `render`)
        return self.path.resolve().relative_to(root) if root else Path(self.path.name)


def inbox_root(path: Path) -> Path | None:
    """The `inbox/` folder a document is in. A path can hold the word `inbox` more than once (a repo in
    `~/inbox/idea-bucket/`): the one next to a `.git` wins, else the nearest to the file."""
    candidates = [p for p in path.resolve().parents if p.name == "inbox"]
    if not candidates:
        return None
    return next((p for p in candidates if (p.parent / ".git").exists()), candidates[0])


@dataclass
class Artifact:
    """A file in `inbox/` that is not markdown (a PDF, an image...). It is only renamed and copied."""

    path: Path  # the inbox file
    name: str | None = None  # `YYYYMMDD-<guid>-<original name>`, once it has one

    @property
    def original_name(self) -> str:
        return _NAMED.sub("", self.path.name, count=1) if _NAMED.match(self.path.name) else self.path.name

    @property
    def size(self) -> int:
        return self.path.stat().st_size


@dataclass
class ScanResult:
    notes: list[Note] = field(default_factory=list)
    artifacts: list[Artifact] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)  # unreadable files: inbox-relative path -> reason
    matched: set[str] = field(default_factory=set)  # the `only` names that found an inbox file


def name_matches(query: str, rel: Path) -> bool:
    """True when `query` names the document at `rel` (its path under `inbox/` or `output/`).

    It may be the file name (`New chat.md`), the name without `.md` (`New chat`) or the path with the
    subfolder (`clippings/New chat.md`). Case is ignored.
    """
    wanted = query.strip().replace("\\", "/").lower()
    return wanted in {
        rel.as_posix().lower(),
        rel.with_suffix("").as_posix().lower(),
        rel.name.lower(),
        rel.stem.lower(),
    }


def note_label(note: Note) -> str:
    """'<class> <id> \"<original file>\"' for log lines."""
    return f'{note.doctype.name} {note.doc_id} "{note.doc.fm.get("source_file", note.path.name)}"'


def is_snapshot_of(shorter: str, longer: str) -> bool:
    """True when `shorter` is an earlier snapshot of the same conversation as `longer`.

    A chat is compared message by message (a message starts at a `**You**` line). Every message of the
    shorter clip must be identical to the longer clip's, except the shorter clip's last one, which is often
    cut off because the conversation was still being written. Text without messages must be an exact prefix.
    """
    if len(shorter) >= len(longer):
        return False
    short_turns, long_turns = _TURN.split(shorter), _TURN.split(longer)
    if len(short_turns) < 2 or len(long_turns) < 2:
        return longer.startswith(shorter)
    complete = short_turns[:-1]
    return long_turns[: len(complete)] == complete and len(short_turns) <= len(long_turns)


def new_id() -> str:
    return secrets.token_hex(3)


def captured_date(fm: dict[str, Any], now: datetime) -> str:
    raw = str(fm.get("captured") or fm.get("created") or "")[:10]
    try:
        return datetime.strptime(raw, "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError:
        return now.strftime("%Y-%m-%d")


_MESSAGE_END = re.compile(r"^---[ \t]*$|^\*\*(?:Gemini|Claude)\*\*[ \t]*$", re.MULTILINE)
_GENERIC_TITLES = frozenset({"", "new chat", "untitled", "chat"})


def first_words(body: str, count: int = 8) -> str:
    """The first words of the first `**You**` message, without links."""
    turns = _TURN.split(body)
    if len(turns) < 2:
        return ""
    message = _MESSAGE_END.split(turns[1])[0]  # your message only, not the answer that follows it
    words = [w for w in re.sub(r"https?://\S+", " ", message).split() if re.search(r"[A-Za-z0-9]", w)]
    return " ".join(words[:count])


def name_title(note: Note) -> str:
    """The title part of the calculated name: the clip's title, else for an AI chat the first words of your
    first message, else the file name it was captured under."""
    title = note.doc.fm.get("title")
    if isinstance(title, str) and title.strip().lower() not in _GENERIC_TITLES:
        return title
    if note.doctype.name == "ai-chat":
        words = first_words(note.doc.body)
        if words:
            return words
    return Path(note.original_name).stem


def slugify_title(text: str) -> str:
    """Plain lower-case letters, digits and hyphens, so the name is safe in every file system."""
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-") or "untitled"


def calculated_stem(captured: str, guid: str, title: str) -> str:
    """`YYYYMMDD-<short guid>-<title>`, short enough for every name derived from it."""
    prefix = f"{captured[0:4]}{captured[5:7]}{captured[8:10]}-{guid}-"
    slug = slugify_title(title)[: MAX_STEM - len(prefix)].rstrip("-")
    return prefix + (slug or "untitled")


def _name_is_taken(ideas_repo: Path, rel_dir: Path, stem: str) -> bool:
    return any(
        (ideas_repo / folder / rel_dir / f"{stem}.md").exists()
        for folder in ("archive", "output", "failed", "duplicates")
    )


def _unique_stem(ideas_repo: Path, rel_dir: Path, captured: str, title: str) -> str:
    for _ in range(50):
        stem = calculated_stem(captured, secrets.token_hex(3), title)
        if not _name_is_taken(ideas_repo, rel_dir, stem):
            return stem
    raise RuntimeError("could not find an unused file name")


def assign_name(ideas_repo: Path, note: Note) -> str:
    """Give the document its calculated file name, unless it already has one (a requeued document)."""
    if note.name is None:
        title = name_title(note)
        stem = _unique_stem(ideas_repo, note.rel.parent, str(note.doc.fm["captured"]), title)
        note.name = f"{stem}.md"
        note.original = note.original_name
        log.info("named %s -> %s", note.original_name, note.name)
    return note.name


def with_filename_fields(raw: str, original: str, calculated: str) -> str:
    """Add the two file name fields to a file's frontmatter, as text. The rest stays byte for byte.

    Fields that are already there are kept, so a requeued file comes back unchanged.
    """
    newline = "\r\n" if "\r\n" in raw else "\n"
    bom = "\ufeff" if raw.startswith("\ufeff") else ""
    text = raw[len(bom) :]
    wanted = {ORIGINAL_KEY: original, CALCULATED_KEY: calculated}

    def lines(skip: set[str]) -> str:
        return "".join(
            f"{k}: {json.dumps(v, ensure_ascii=False)}{newline}" for k, v in wanted.items() if k not in skip
        )

    opening = re.match(r"---[ \t]*\r?\n", text)
    if opening:
        closing = re.compile(r"^---[ \t]*\r?$", re.MULTILINE).search(text, opening.end())
        if closing:
            block = text[opening.end() : closing.start()]
            present = {k for k in wanted if re.search(rf"^{k}[ \t]*:", block, re.MULTILINE)}
            return bom + text[: closing.start()] + lines(present) + text[closing.start() :]
    return f"{bom}---{newline}{lines(set())}---{newline}{text}"


def artifact_name(original: str, now: datetime, taken: Callable[[str], bool]) -> str:
    """`YYYYMMDD-<short guid>-<original name>`. A name that already starts like that is kept.

    The original name and extension stay as they are (we know nothing about the content), apart from
    characters that are illegal in file names, and the whole name is cut to 128 characters.
    """
    if _NAMED.match(original):
        return original
    cleaned = _ILLEGAL.sub("_", original).strip() or "file"
    suffix = Path(cleaned).suffix
    stem = cleaned[: len(cleaned) - len(suffix)]
    for _ in range(50):
        prefix = f"{now.strftime('%Y%m%d')}-{secrets.token_hex(3)}-"
        room = MAX_NAME - len(prefix) - len(suffix)
        name = f"{prefix}{stem[:room].rstrip()}{suffix}"
        if not taken(name):
            return name
    raise RuntimeError("could not find an unused file name")


def copy_artifact(
    ideas_repo: Path, docs_repo: Path, artifact: Artifact, now: datetime | None = None
) -> tuple[list[Path], list[Path]]:
    """Archive the file and copy it to epiaku-docs, both under `YYYYMMDD-<guid>-<original name>`, and take it
    out of `inbox/`. Returns the touched paths of the idea-bucket repo and of the epiaku-docs repo.
    """
    now = now or datetime.now().astimezone()
    archive_dir = ideas_repo / "archive" / ARTIFACTS_DIR
    docs_dir = docs_repo / "idea-bucket" / ARTIFACTS_DIR
    artifact.name = artifact.name or artifact_name(
        artifact.path.name, now, lambda n: (archive_dir / n).exists() or (docs_dir / n).exists()
    )  # a name that already has its date and guid (a requeued file) is kept
    archived, published = archive_dir / artifact.name, docs_dir / artifact.name
    for dest in (archived, published):
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(artifact.path, dest)
    artifact.path.unlink()
    log.info(
        "artifact %s -> archive/%s/%s and idea-bucket/%s/%s",
        artifact.original_name,
        ARTIFACTS_DIR,
        artifact.name,
        ARTIFACTS_DIR,
        artifact.name,
    )
    return [archived, artifact.path], [published]


def rel_in_inbox(ideas_repo: Path, src: Path) -> Path:
    return src.resolve().relative_to((ideas_repo / "inbox").resolve())


def _rel_in_folder(ideas_repo: Path, src: Path) -> tuple[str, Path]:
    """The top folder (`inbox` or `output`) a file is in, and its path below it."""
    parts = src.resolve().relative_to(ideas_repo.resolve()).parts
    return parts[0], Path(*parts[1:])


def archive_copy(ideas_repo: Path, note: Note) -> Path:
    """Archive the original under its calculated name. The file is unchanged except for the two file name
    fields added to its frontmatter."""
    name = assign_name(ideas_repo, note)
    dest = ideas_repo / "archive" / note.rel.parent / name
    dest.parent.mkdir(parents=True, exist_ok=True)
    raw = note.path.read_bytes().decode("utf-8")
    write_atomic(dest, with_filename_fields(raw, note.original_name, name).encode("utf-8"))
    log.info("archived %s", dest.relative_to(ideas_repo).as_posix())
    return dest


def start_work(ideas_repo: Path, note: Note, now: datetime | None = None) -> list[Path]:
    """A document is about to be worked on: archive the original, write the working copy to `output/`
    (with `stage: analyzed`), and take the document out of `inbox/`. So it can never be started twice."""
    now = now or datetime.now().astimezone()
    touched = [archive_copy(ideas_repo, note)]
    out = note.output_path(ideas_repo)
    out.parent.mkdir(parents=True, exist_ok=True)
    fm = {
        **note.doc.fm,
        ORIGINAL_KEY: note.original_name,
        CALCULATED_KEY: note.name,
        "analyzed_at": now.isoformat(timespec="seconds"),
        "stage": STAGE_ANALYZED,
    }
    write_atomic(out, dump(Doc(fm, note.doc.body)))
    note.path.unlink()  # last: until now a crash leaves the document in inbox/ as well, never nowhere
    log.info("started work: %s -> output/%s", note.path.name, note.target_rel.as_posix())
    return [*touched, out, note.path]


def return_to_inbox(ideas_repo: Path, note: Note) -> list[Path]:
    """Undo `start_work`: the archived original goes back to `inbox/` where it was (with the file name fields,
    like a requeued document), and the working copy in `output/` is deleted. The document is then in one
    place only, so the next run starts it again under the same name. For a run that was interrupted, or a
    clip that has to wait for YouTube after work had begun."""
    if note.name is None:
        return []
    archived = ideas_repo / "archive" / note.rel.parent / note.name
    out = note.output_path(ideas_repo)
    if not archived.is_file():
        if not note.path.exists():  # never started is fine: it is still in inbox/
            log.error("cannot return %s to inbox/: its archive copy %s is missing", note.name, archived)
        return []
    write_atomic(note.path, archived.read_bytes())
    archived.unlink()
    out.unlink(missing_ok=True)
    log.info("returned %s to inbox/%s", note.name, note.rel.as_posix())
    return [archived, note.path, out]


def deferred_in_output(ideas_repo: Path) -> list[str]:
    """The documents a temporary error stalled: their paths under `output/` (the same as under `archive/`)."""
    found: list[str] = []
    output = ideas_repo / "output"
    for path in sorted(output.rglob("*.md")) if output.is_dir() else []:
        rel = path.relative_to(output)
        if any(part.startswith(".") for part in rel.parts):
            continue
        try:
            stage = load(path).fm.get("stage")
        except (FrontmatterError, UnicodeDecodeError):
            continue
        if stage == STAGE_DEFERRED:
            found.append(rel.as_posix())
    return found


def mark_deferred(ideas_repo: Path, note: Note, reason: str, now: datetime | None = None) -> list[Path]:
    """An error stalled the document: keep the working copy in `output/` and say why in its frontmatter."""
    now = now or datetime.now().astimezone()
    out = note.output_path(ideas_repo)
    doc = load(out)
    fm = {
        **doc.fm,
        "stage": STAGE_DEFERRED,
        "deferred_at": now.isoformat(timespec="seconds"),
        "deferred_reason": reason,
    }
    write_atomic(out, dump(Doc(fm, doc.body)))
    return [out]


def move_to_failed(
    ideas_repo: Path,
    src: Path,
    reason: str,
    *,
    doc_id: str | None = None,
    doc_class: str | None = None,
    now: datetime | None = None,
) -> list[Path]:
    """Move a document to `failed/` with the reason next to it.

    From `output/` (the working copy): it already has its calculated name, and the original was archived
    when the work started. From `inbox/` (an unreadable file, whose frontmatter cannot be edited): give it
    a calculated name, and archive and move the bytes unchanged.
    """
    now = now or datetime.now().astimezone()
    top, rel = _rel_in_folder(ideas_repo, src)
    touched: list[Path] = []
    original = src.name
    if top == "inbox":
        stem = _unique_stem(ideas_repo, rel.parent, now.strftime("%Y-%m-%d"), Path(src.name).stem)
        rel = rel.parent / f"{stem}.md"
        archived = ideas_repo / "archive" / rel
        archived.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, archived)
        touched.append(archived)
        log.info("archived %s", archived.relative_to(ideas_repo).as_posix())
    dest = ideas_repo / "failed" / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.unlink(missing_ok=True)
    shutil.move(src, dest)
    error_file = dest.with_suffix(".error.txt")
    write_atomic(
        error_file,
        f"time: {now.isoformat(timespec='seconds')}\n"
        f"original file: {original}\n"
        f"calculated file: {rel.as_posix()}\n"
        f"id: {doc_id or '-'}\n"
        f"class: {doc_class or '-'}\n"
        f"reason: {reason}\n",
    )
    log.info("moved to failed/%s (reason in %s)", rel.as_posix(), error_file.name)
    return [*touched, src, dest, error_file]


def move_to_duplicates(ideas_repo: Path, note: Note, winner: Note) -> list[Path]:
    """Archive an earlier snapshot, then move it out of `inbox/` to `duplicates/`. Nothing is deleted."""
    touched = [archive_copy(ideas_repo, note)]
    dest = ideas_repo / "duplicates" / note.target_rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    fm = {
        **note.doc.fm,
        ORIGINAL_KEY: note.original_name,
        CALCULATED_KEY: note.name,
        "duplicate_of": winner.rel.as_posix(),
    }
    write_atomic(dest, dump(Doc(fm, note.doc.body)))
    note.path.unlink()
    log.info("moved to duplicates/%s (duplicate of %s)", note.target_rel.as_posix(), winner.rel.as_posix())
    return [*touched, note.path, dest]


def _plain_file_name(name: str) -> bool:
    return (
        bool(name)
        and name == Path(name).name
        and "\\" not in name
        and "\x00" not in name
        and len(name) <= 255
    )


def _analyse(path: Path, doc: Doc, source_file: str, now: datetime) -> Note:
    doctype = detect(doc.fm, doc.body)
    doc_id = derive_id(doctype, doc.fm, doc.body) or new_id()
    fm = {
        **doc.fm,
        "id": doc_id,
        "class": doctype.name,
        "captured": captured_date(doc.fm, now),
        "source_file": source_file,
    }
    # Both are set when a document comes back from archive/. They are text from a file, so they are only
    # trusted when they look like what we write: a name with a path in it must never reach the file system.
    name, original = doc.fm.get(CALCULATED_KEY), doc.fm.get(ORIGINAL_KEY)
    if name is not None and not (
        isinstance(name, str) and _CALCULATED_NAME.match(name) and len(name) <= MAX_NAME
    ):
        log.warning("%s: ignoring a %s that is not one of our names: %r", path.name, CALCULATED_KEY, name)
        name = None
    if original is not None and not (isinstance(original, str) and _plain_file_name(original)):
        log.warning("%s: ignoring an %s that is not a plain file name: %r", path.name, ORIGINAL_KEY, original)
        original = None
    return Note(doc_id, doctype, Doc(fm, doc.body), path, name or None, original or None)


def scan_inbox(ideas_repo: Path, *, now: datetime | None = None, only: list[str] | None = None) -> ScanResult:
    """Read the inbox: markdown documents (class and id in memory) and other files (artifacts).

    Writes nothing and moves nothing.
    """
    now = now or datetime.now().astimezone()
    inbox = ideas_repo / "inbox"
    result = ScanResult()
    for path in sorted(inbox.rglob("*")) if inbox.is_dir() else []:
        rel = path.relative_to(inbox)
        if not path.is_file() or any(part.startswith(".") for part in rel.parts):
            continue
        if path.name.lower() in _JUNK:
            continue
        is_note = path.suffix.lower() == ".md"
        doc: Doc | None = None
        error: str | None = None
        if is_note:
            try:
                doc = load(path)
            except (FrontmatterError, UnicodeDecodeError) as e:
                error = str(e)
        if only is not None:
            names = [
                rel
            ]  # a requeued file is found by its calculated name or by the name it was captured under
            original = doc.fm.get(ORIGINAL_KEY) if doc else None
            if isinstance(original, str) and original:
                names.append(rel.parent / original)
            if not is_note and _NAMED.match(rel.name):
                names.append(rel.parent / _NAMED.sub("", rel.name, count=1))
            hits = {query for query in only if any(name_matches(query, name) for name in names)}
            if not hits:
                continue
            result.matched |= hits
        if not is_note:
            result.artifacts.append(Artifact(path))
            continue
        if doc is None:
            result.errors[f"inbox/{rel.as_posix()}"] = error or "unreadable"
            log.error("cannot read inbox/%s: %s", rel.as_posix(), error)
            continue
        note = _analyse(path, doc, f"inbox/{rel.as_posix()}", now)
        note.inbox_rel = rel
        result.notes.append(note)
    return result


@dataclass
class Requeued:
    """A document from `archive/` that `--requeue` put (or would put) back into `inbox/`."""

    rel: Path  # the path under archive/ and inbox/: subfolder plus calculated name
    doc_id: str
    doc_class: str
    copied: bool  # False in a dry run, or when inbox/ already had a file with that name
    touched: list[Path] = field(default_factory=list)  # every path it was moved from or to, for the commit


def requeue_from_archive(
    ideas_repo: Path, queries: list[str], *, dry_run: bool = False
) -> tuple[list[Requeued], list[str]]:
    """Move the archived original of each named document back into `inbox/`, so the pipeline starts on it
    from scratch. Returns what was found and the queries that matched nothing in `archive/`.

    The move keeps the subfolder and the calculated name, and everything an earlier run left behind is
    deleted: the working copy in `output/` and, for a failed document, the copy in
    `failed/` with its `.error.txt`. So the document is in one place only, `inbox/`, until the run starts.
    The run then writes `archive/` and `output/` again under the same name, and overwrites the page in
    epiaku-docs. A name that `inbox/` already holds is left alone.
    """
    archive = ideas_repo / "archive"
    found: list[Requeued] = []
    matched: set[str] = set()
    for path in sorted(archive.rglob("*.md")) if archive.is_dir() else []:
        rel = path.relative_to(archive)
        if rel.parts[0] == ARTIFACTS_DIR or any(part.startswith(".") for part in rel.parts):
            continue
        try:
            doc = load(path)
        except (FrontmatterError, UnicodeDecodeError):
            continue
        names = [rel]  # found by its calculated name or by the name it was captured under
        original = doc.fm.get(ORIGINAL_KEY)
        if isinstance(original, str) and original:
            names.append(rel.parent / original)
        hits = {q for q in queries if any(name_matches(q, name) for name in names)}
        if not hits:
            continue
        matched |= hits
        dest = ideas_repo / "inbox" / rel
        output = ideas_repo / "output" / rel
        failed = ideas_repo / "failed" / rel
        moved = not dry_run and not dest.exists()
        touched: list[Path] = []
        if moved:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(path, dest)
            touched = [path, dest]
            for stale in (output, failed, failed.with_suffix(".error.txt")):
                if stale.exists():
                    stale.unlink()
                    touched.append(stale)
            log.info("requeued archive/%s -> inbox/%s", rel.as_posix(), rel.as_posix())
        elif dest.exists():
            log.warning("inbox/%s already exists: not overwritten", rel.as_posix())
        found.append(Requeued(rel, str(doc.fm.get("id", "")), str(doc.fm.get("class", "")), moved, touched))
    return found, [q for q in queries if q not in matched]


def read_note(path: Path, now: datetime | None = None) -> Note:
    """Read one document from anywhere, for `reason` and `render`."""
    return _analyse(path, load(path), path.as_posix(), now or datetime.now().astimezone())


def load_staged_note(ideas_repo: Path, path: Path, now: datetime | None = None) -> Note:
    """Read a document from `inbox/`, `output/`, `archive/`, `failed/` or `duplicates/` and keep its
    subfolder (`notes/` or `clippings/`), which `read_note` only finds below an `inbox` folder."""
    try:
        top, rel = _rel_in_folder(ideas_repo, path)
    except ValueError:
        raise ValueError(f"{path} is not inside {ideas_repo}") from None
    if top not in STAGE_FOLDERS or not rel.parts or rel == Path("."):
        raise ValueError(f"{path} is not inside one of {', '.join(STAGE_FOLDERS)}")
    note = _analyse(path, load(path), path.as_posix(), now or datetime.now().astimezone())
    note.inbox_rel = rel
    return note
