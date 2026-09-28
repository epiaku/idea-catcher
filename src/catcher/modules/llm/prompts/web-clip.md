---
version: web-clip-1
---
You condense a web page that the author clipped with the Obsidian Web Clipper (an article, blog post, documentation page or tutorial) into a structured documentation page for the Epiaku docs site.

The clip can contain menus, cookie notices, ads, comment sections and "related articles" that are not part of the content. Ignore those. Keep the facts, the argument, the steps and the examples. Do not invent anything that is not in the page. Do not copy long passages: summarise in your own words, with a short quote only where the exact wording matters.

Title hint (may be wrong or generic): {{ title_hint }}
Source: {{ source or "unknown" }}

<page>
{{ body }}
</page>

Fill in:
- title: short and specific (max 70 characters), based on what the page is about.
- description: one sentence (max 160 characters).
- summary: 3 to 6 bullet points with the key takeaways.
- key_points: the concrete facts, numbers, steps or claims worth remembering, one per item (empty if there are none).
- ideas_to_use: how the author could use this in their own projects, one per item, concrete and short (empty if nothing fits).
- body: the detailed page in Markdown: `##` headings that each start with an emoji, tables where things are compared, code in fenced code blocks. Do not repeat the summary, key points or ideas. Do not use Hugo shortcodes.
- tags: exactly one idea-type tag, 1 to 4 topic tags and at most one project tag, chosen ONLY from the lists below.

Idea-type tags: {{ tags.idea_types | join(", ") }}
Topic tags: {{ tags.topics | join(", ") }}
Project tags: {{ tags.projects | join(", ") }}
{% if capture_tags %}
Tags the author added while capturing (use them if they are in the lists): {{ capture_tags | join(", ") }}
{% endif %}
