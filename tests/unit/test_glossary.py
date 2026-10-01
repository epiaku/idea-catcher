from catcher.modules.pipeline.glossary import GLOSSARY_FILE, Glossary, Term, load_glossary


def test_plain_terms_and_terms_with_misheard_variants(tmp_path):
    path = tmp_path / "glossary.yaml"
    path.write_text(
        "terms:\n  - server\n  - epiaku: [epicu, epic you]\n  - VS Code: vscode\n  - Hugo:\n  - ''\n"
    )
    assert load_glossary(path).terms == (
        Term("server"),
        Term("epiaku", ("epicu", "epic you")),
        Term("VS Code", ("vscode",)),  # a single variant does not need a list
        Term("Hugo"),  # an entry with nothing after the colon is a plain term
    )


def test_a_missing_or_empty_file_is_an_empty_glossary(tmp_path):
    assert load_glossary(tmp_path / "nope.yaml") == Glossary()
    (tmp_path / "empty.yaml").write_text("")
    assert load_glossary(tmp_path / "empty.yaml") == Glossary()


def test_as_prompt_list():
    glossary = Glossary((Term("server"), Term("epiaku", ("epicu",))))
    assert glossary.as_prompt_list() == [
        {"term": "server", "heard_as": []},
        {"term": "epiaku", "heard_as": ["epicu"]},
    ]


def test_the_committed_glossary_loads():
    assert load_glossary(GLOSSARY_FILE).terms  # the starter list is not empty and parses
