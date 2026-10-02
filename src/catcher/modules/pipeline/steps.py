"""Pure steps of a run: no file is read or written here."""

from catcher.modules.pipeline.inbox import Note, is_snapshot_of


def order_notes(notes: list[Note]) -> list[Note]:
    """Oldest capture first; ties by id, then by path, so a run is deterministic."""
    return sorted(notes, key=lambda n: (str(n.doc.fm.get("captured", "")), n.doc_id, n.path.as_posix()))


def split_duplicates(ordered: list[Note]) -> tuple[list[Note], list[tuple[Note, Note]]]:
    """Several clips of one conversation: only the longest (the first one on a tie) is processed.

    Returns `(to_process, [(duplicate, winner), ...])`, both in input order. A clip with the same id
    but other content is not a duplicate: it is processed.
    """
    winners: dict[str, Note] = {}
    for note in ordered:
        best = winners.get(note.doc_id)
        if best is None or len(note.doc.body) > len(best.doc.body):
            winners[note.doc_id] = note
    to_process: list[Note] = []
    duplicates: list[tuple[Note, Note]] = []
    for note in ordered:
        winner = winners[note.doc_id]
        if note is winner or not is_snapshot_of(note.doc.body, winner.doc.body):
            to_process.append(note)
        else:
            duplicates.append((note, winner))
    return to_process, duplicates
