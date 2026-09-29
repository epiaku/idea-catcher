---
version: youtube-gemini-2
---
Below is a Gemini web chat in which someone asked Gemini to summarize a YouTube video. Structure Gemini's
own answer into the fields below. This is a reformatting task, not a fact-check: you have no transcript
and no video metadata, only what Gemini said, so do not add anything of your own that is not in the chat.
Ignore any metrics Gemini gave (views, likes, subscribers): code does not use them.

<chat>
{{ body }}
</chat>

Fill in:
- title: the video's title, as Gemini gives it.
- creator: the channel or presenter name, as Gemini gives it (best guess if Gemini does not name one).
- description: one sentence (max 160 characters) with the video's core idea.
- summary: 1 to 3 sentences.
- main_purpose: the video's core message in one short paragraph.
- key_examples: the concrete examples Gemini mentions.
- action_plan: the recommended steps, one per item.
- tools: the tools, services, equipment, apps or platforms Gemini mentions, one per item.
- tips: tips Gemini shares, each with a short explanation and how to apply it.
- channel_application: how to apply the lessons to Epiaku: the YouTube channel, Vibe Coding Tech Stack demos, and building phone apps or small SaaS applications.
- tags: exactly one idea-type tag, 1 to 4 topic tags and at most one project tag, chosen ONLY from the lists below.

If Gemini gives a timestamp for a moment in the video, keep it (for example 12:40). Do not use Hugo shortcodes.

Idea-type tags: {{ tags.idea_types | join(", ") }}
Topic tags: {{ tags.topics | join(", ") }}
Project tags: {{ tags.projects | join(", ") }}
