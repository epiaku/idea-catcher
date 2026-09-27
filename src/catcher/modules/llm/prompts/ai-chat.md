---
version: ai-chat-1
---
You condense a long AI chat (Gemini or Claude) into a structured documentation page for the Epiaku docs site.

The chat has detours, repeated answers and step-by-step click guides. Keep the decisions, the options that were compared and the open questions. Drop fluff and detours. If code appears in several versions, keep only the latest version. Do not invent anything that is not in the chat.

Title hint (may be wrong or just "New chat"): {{ title_hint }}
Source: {{ source or "unknown" }}

<chat>
{{ body }}
</chat>

Fill in:
- title: short and specific (max 70 characters). Never "New chat".
- description: one sentence (max 160 characters).
- summary: 3 to 7 bullet points with the key takeaways.
- decisions: what was decided, one per item (empty if nothing).
- options: the alternatives compared, one per item with the main trade-off (empty if none).
- open_questions: what is still open, one per item (empty if nothing).
- body: the detailed page in Markdown: `##` headings that each start with an emoji, tables where options are compared, and only the latest version of any code in fenced code blocks. Do not repeat the summary, decisions, options or open questions. Do not use Hugo shortcodes.
- tags: exactly one idea-type tag, 1 to 4 topic tags and at most one project tag, chosen ONLY from the lists below.

Idea-type tags: {{ tags.idea_types | join(", ") }}
Topic tags: {{ tags.topics | join(", ") }}
Project tags: {{ tags.projects | join(", ") }}
{% if capture_tags %}
Tags the author added while capturing (use them if they are in the lists): {{ capture_tags | join(", ") }}
{% endif %}
