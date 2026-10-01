---
version: youtube-gemini-5
---
Below is a Gemini web chat in which someone asked Gemini to summarize a YouTube video. Structure Gemini's
own answer into the fields below. This is a reformatting task, not a fact-check: you have no transcript
and no video metadata, only what Gemini said, so do not add anything of your own that is not in the chat.
Copy numbers exactly as Gemini wrote them: never round, estimate or make one up.

<chat>
{{ body }}
</chat>
{% if context %}
About Epiaku (only for channel_application):
{{ context }}
{% endif %}

Fill in:
- title: the video's title, as Gemini gives it.
- creator: the channel or presenter name, as Gemini gives it (best guess if Gemini does not name one).
- description: one sentence (max 160 characters) with the video's core idea.
- summary: 1 to 3 sentences.
- main_purpose: the video's core message in one short paragraph.
- key_examples: the concrete examples Gemini mentions.
- action_plan: the recommended steps, one per item.
- tools: the tools, services, equipment, apps or platforms Gemini says the video mentions or uses, from its Tech Stack and Action Plan sections, one per item. Do not take tools from Gemini's Channel Application advice: those are suggestions for the author, not what the video covers.
- metrics: the values in Gemini's Metrics table, copied as written: as_of (the date Gemini gives), views, likes, subscribers. Use null for a value Gemini does not give or marks "Not available".
- chapters: the chapters Gemini lists for the video, each with its start time exactly as Gemini wrote it (for example 1:36) and its title. Leave it empty if Gemini lists none or writes "Not available".
- tips: tips Gemini shares, each with a short explanation and how to apply it.
- channel_application: how to apply the lessons to Epiaku{% if context %}, using the context about Epiaku above. Give 2 to 4 concrete suggestions that fit it, and name only what Gemini says the video teaches. If the video does not fit Epiaku, say so in one sentence instead of forcing a fit{% else %}: the YouTube channel, Vibe Coding Tech Stack demos, and building phone apps or small SaaS applications{% endif %}.
- tags: exactly one idea-type tag, 1 to 4 topic tags and at most one project tag, chosen ONLY from the lists below. Choose them from what the video covers, not from the Channel Application advice.

If Gemini gives a timestamp for a moment in the video, keep it (for example 12:40). Do not use Hugo shortcodes.

Idea-type tags: {{ tags.idea_types | join(", ") }}
Topic tags: {{ tags.topics | join(", ") }}
Project tags: {{ tags.projects | join(", ") }}
