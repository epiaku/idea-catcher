import pytest

from catcher.modules.youtube.urls import find_youtube_url, host_of, video_id


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "https://www.youtube.com/watch?v=tcqEUSNCn8I&list=PL4RKwZPB4EJDizVLBstGU-ocFQxb6RtRO&index=10&t=1s",
            "tcqEUSNCn8I",
        ),
        ("https://youtu.be/AeV5F0ppaGw?si=abc", "AeV5F0ppaGw"),
        ("https://m.youtube.com/watch?v=MBPHU7aaklM", "MBPHU7aaklM"),
        ("https://www.youtube.com/shorts/AeV5F0ppaGw", "AeV5F0ppaGw"),
        ("https://www.youtube.com/embed/AeV5F0ppaGw", "AeV5F0ppaGw"),
        ("https://www.youtube.com/watch?v=Ab\\_cdEfGhIj", "Ab_cdEfGhIj"),
        ("https://www.youtube.com/@pixegami", None),
        ("https://www.youtube.com/watch?v=short", None),
        ("https://gemini.google.com/app/2446cd9c762c9cc9", None),
        ("", None),
    ],
)
def test_video_id(url, expected):
    assert video_id(url) == expected


def test_host_of_strips_www_and_mobile():
    assert host_of("https://www.youtube.com/watch?v=x") == "youtube.com"
    assert host_of("https://m.youtube.com/watch?v=x") == "youtube.com"
    assert host_of("https://gemini.google.com/app/1?x=2") == "gemini.google.com"


def test_find_youtube_url_in_chat_text():
    text = "Summarize this YouTube video: https://www.youtube.com/watch?v=MBPHU7aaklM\n\nFormat"
    assert find_youtube_url(text) == "https://www.youtube.com/watch?v=MBPHU7aaklM"
    assert find_youtube_url("no video here https://example.com") is None
