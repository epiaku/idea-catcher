---
version: youtube-from-gemini-1
---
Below is a Gemini web chat in which someone asked Gemini to summarize a YouTube video. Convert Gemini's final answer into the structured fields below.

Keep Gemini's content, but correct anything the transcript clearly contradicts. Ignore Gemini's metrics table: code adds views, likes and subscriber counts.

<chat>
{{ body }}
</chat>

Video facts:
- Title: {{ facts.title or "unknown" }}
- Channel: {{ facts.channel or "unknown" }}
- Published: {{ facts.upload_date or "unknown" }}

{% if transcript %}
<transcript>
{{ transcript }}
</transcript>
{% else %}
There is NO transcript for this video, so keep Gemini's content as it is.
{% endif %}

Fill in:
- title: the video's title as it appears on YouTube.
- creator: the channel or presenter name.
- description: one sentence (max 160 characters) with the video's core idea.
- summary: 1 to 3 sentences.
- main_purpose: the video's core message in one short paragraph.
- key_examples: the concrete examples used in the video.
- action_plan: the recommended steps, one per item.
- tools: the tools, services, equipment, apps or platforms mentioned, one per item.
- tips: tips that are shared, each with a short explanation and how to apply it.
- channel_application: how to apply the lessons to Epiaku: the YouTube channel, Vibe Coding Tech Stack demos, and building phone apps or small SaaS applications.
- tags: exactly one idea-type tag, 1 to 4 topic tags and at most one project tag, chosen ONLY from the lists below.

Do not use Hugo shortcodes.

Idea-type tags: {{ tags.idea_types | join(", ") }}
Topic tags: {{ tags.topics | join(", ") }}
Project tags: {{ tags.projects | join(", ") }}
