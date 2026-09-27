---
version: youtube-1
---
You summarize a YouTube video for the Epiaku documentation site. You cannot watch the video: work only from the facts and the transcript below. Never invent views, likes, subscriber counts or dates, because code adds those.

Video facts:
- Title: {{ facts.title or "unknown" }}
- Channel: {{ facts.channel or "unknown" }}
- Published: {{ facts.upload_date or "unknown" }}
- Length in seconds: {{ facts.duration_s or "unknown" }}
{% if facts.chapters %}
- Chapters: {% for c in facts.chapters %}{{ c.title }}{% if not loop.last %}; {% endif %}{% endfor %}

{% endif %}

<description>
{{ facts.description or "" }}
</description>

{% if transcript %}
<transcript>
{{ transcript }}
</transcript>
{% else %}
There is NO transcript for this video. Summarize from the title and description only, and say so in the summary.
{% endif %}

Fill in:
- title: the video's title as it appears on YouTube.
- creator: the channel or presenter name.
- description: one sentence (max 160 characters) with the video's core idea, for someone deciding whether to read the page.
- summary: YouTube's own description condensed to 1 to 3 sentences.
- main_purpose: the video's core message in one short paragraph.
- key_examples: the concrete examples used in the video.
- action_plan: the recommended steps, one per item.
- tools: the tools, services, equipment, apps or platforms mentioned, one per item. Leave it empty if none are mentioned.
- tips: tips that are shared, each with a short explanation and how to apply it.
- channel_application: how to apply the lessons to Epiaku: the YouTube channel, Vibe Coding Tech Stack demos, and building phone apps or small SaaS applications.
- tags: exactly one idea-type tag, 1 to 4 topic tags and at most one project tag, chosen ONLY from the lists below.

When you mention a moment in the video, add its timestamp from the transcript (for example 12:40). Do not use Hugo shortcodes.

Idea-type tags: {{ tags.idea_types | join(", ") }}
Topic tags: {{ tags.topics | join(", ") }}
Project tags: {{ tags.projects | join(", ") }}
