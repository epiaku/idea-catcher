from catcher.modules.pipeline.tags import load_tags, normalize_tags

TAGS = load_tags()


def test_starter_list_is_loaded():
    assert "app-idea" in TAGS.idea_types
    assert "obsidian" in TAGS.topics
    assert "idea-catcher" in TAGS.projects
    assert TAGS.as_prompt_dict()["idea_types"][0] == "app-idea"


def test_unknown_tags_are_dropped():
    result = normalize_tags(["app-idea", "apps", "nonsense"], TAGS)
    assert result.tags == ["app-idea"]
    assert result.dropped == ["apps", "nonsense"]


def test_tags_are_cleaned_before_matching():
    result = normalize_tags(["App-Idea", "#obsidian", "Home Lab", "second_brain"], TAGS)
    assert result.tags == ["app-idea", "obsidian", "home-lab", "second-brain"]


def test_only_one_idea_type_and_one_project():
    result = normalize_tags(["todo", "app-idea", "idea-catcher", "yummystream"], TAGS)
    assert result.tags == ["todo", "idea-catcher"]
    assert result.dropped == ["app-idea", "yummystream"]


def test_at_most_four_topics_and_idea_type_first():
    raw = ["obsidian", "hugo", "automation", "home-lab", "crm", "tech-note"]
    result = normalize_tags(raw, TAGS)
    assert result.tags == ["tech-note", "obsidian", "hugo", "automation", "home-lab"]
    assert result.dropped == ["crm"]


def test_duplicates_are_ignored():
    assert normalize_tags(["obsidian", "Obsidian", "app-idea"], TAGS).tags == ["app-idea", "obsidian"]
