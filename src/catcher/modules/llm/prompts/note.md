---
version: note-1
---
You turn a short idea note, usually dictated on a phone, into a small documentation page.

The note may contain speech-to-text mistakes and filler words. Fix them, keep the author's tone and meaning, and do not add facts or ideas that are not in the note. A two-line idea stays short: never pad it into a long page.

Title hint (the note's file name, may be wrong): {{ title_hint }}

<note>
{{ body }}
</note>

Fill in:
- title: a short, specific title (max 70 characters).
- description: one sentence (max 160 characters) saying what the idea is.
- body: the cleaned-up note as Markdown. Use short paragraphs or bullets. Only use `##` headings, each starting with an emoji, if the note has several distinct parts. Do not repeat the title as a heading. Do not use Hugo shortcodes.
- tags: exactly one idea-type tag, 1 to 4 topic tags and at most one project tag, chosen ONLY from the lists below.

Idea-type tags: {{ tags.idea_types | join(", ") }}
Topic tags: {{ tags.topics | join(", ") }}
Project tags: {{ tags.projects | join(", ") }}
{% if capture_tags %}
Tags the author added while capturing (use them if they are in the lists): {{ capture_tags | join(", ") }}
{% endif %}
