import pytest

from catcher.modules.pipeline.tags import load_tags, normalize_tags

TAGS = load_tags()


def test_starter_list_is_loaded():
    assert "app-idea" in TAGS.idea_types
    assert "obsidian" in TAGS.topics
    assert "idea-catcher" in TAGS.projects
    assert TAGS.as_prompt_dict()["idea_types"][0] == "youtube-idea"


def test_unknown_tags_are_dropped():
    result = normalize_tags(["app-idea", "apps", "nonsense"], TAGS)
    assert result.tags == ["app-idea"]
    assert result.dropped == ["apps", "nonsense"]


def test_tags_are_cleaned_before_matching():
    result = normalize_tags(["App-Idea", "#obsidian", "Home Lab", "second_brain"], TAGS)
    assert result.tags == ["app-idea", "obsidian", "home-lab", "second-brain"]


PRIORITY = [
    "youtube-idea",
    "saas-idea",
    "app-idea",
    "digital-product-idea",
    "ai-influencer-idea",
    "todo",
    "strategy",
    "tech-note",
]


def test_only_one_idea_type_and_one_project():
    result = normalize_tags(["todo", "app-idea", "idea-catcher", "yummystream"], TAGS)
    assert result.tags == ["app-idea", "idea-catcher"]
    assert result.dropped == ["todo", "yummystream"]


def test_at_most_four_topics_and_idea_type_first():
    raw = ["obsidian", "hugo", "automation", "home-lab", "crm", "tech-note"]
    result = normalize_tags(raw, TAGS)
    assert result.tags == ["tech-note", "obsidian", "hugo", "automation", "home-lab"]
    assert result.dropped == ["crm"]


def test_duplicates_are_ignored():
    assert normalize_tags(["obsidian", "Obsidian", "app-idea"], TAGS).tags == ["app-idea", "obsidian"]


def test_idea_type_priority_order():
    assert list(TAGS.idea_types) == PRIORITY


PAIRS = [(PRIORITY[i], PRIORITY[j]) for i in range(len(PRIORITY)) for j in range(i + 1, len(PRIORITY))]


@pytest.mark.parametrize(("higher", "lower"), PAIRS)
@pytest.mark.parametrize("lower_first", [True, False])
def test_higher_priority_idea_type_wins(higher, lower, lower_first):
    raw = [lower, higher] if lower_first else [higher, lower]
    result = normalize_tags(raw, TAGS)
    assert result.tags == [higher]
    assert result.dropped == [lower]


@pytest.mark.parametrize("other", PRIORITY[:-1])
def test_tech_note_loses_to_every_other_idea_type(other):
    assert normalize_tags(["tech-note", other], TAGS).tags == [other]


def test_live_run_case():
    raw = ["tech-note", "youtube-idea", "llm-models", "ai-agents", "video-production"]
    result = normalize_tags(raw, TAGS)
    assert result.tags == ["youtube-idea", "llm-models", "ai-agents", "video-production"]
    assert result.dropped == ["tech-note"]


def test_three_idea_types_keep_only_the_top_one():
    result = normalize_tags(["todo", "tech-note", "saas-idea"], TAGS)
    assert result.tags == ["saas-idea"]
    assert result.dropped == ["todo", "tech-note"]


def test_idea_type_is_cleaned_before_priority():
    result = normalize_tags(["todo", "App_Idea"], TAGS)
    assert result.tags == ["app-idea"]
    assert result.dropped == ["todo"]


def test_a_single_idea_type_is_unchanged():
    assert normalize_tags(["strategy", "obsidian"], TAGS).tags == ["strategy", "obsidian"]


def test_no_idea_type_is_fine():
    result = normalize_tags(["obsidian", "idea-catcher"], TAGS)
    assert result.tags == ["obsidian", "idea-catcher"]
    assert result.dropped == []
