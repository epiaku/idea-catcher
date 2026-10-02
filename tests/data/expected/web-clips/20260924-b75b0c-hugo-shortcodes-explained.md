---
title: 'Hugo shortcodes: usage and custom templates'
description: Use Hugo shortcodes to add reusable elements to content files, pass parameters, and create custom templates.
date: '2026-09-24'
weight: 100
type: docs
id: 8dcbf3dbbc0b
tags:
- tech-note
- hugo
source: https://gohugo.io/content-management/shortcodes/
source_file: clippings/20260924-b75b0c-hugo-shortcodes-explained.md
original_filename: Hugo shortcodes explained.md
llm:
  profile: clippings
  backend: openai
  model: gpt-6-sol
  prompt_version: web-clip-2
---
## 📝 Summary

- Shortcodes add richer elements to content without repeating raw HTML.
- Calls can pass positional or named parameters; some shortcodes wrap content.
- Custom shortcodes are templates that can read parameters and wrapped content.
- The shortcode delimiter determines whether its output is processed as Markdown.

## 🔑 Key Points

- Place a custom shortcode template at `layouts/shortcodes/name.html`.
- Use `.Get 0` for a positional parameter and `.Get "title"` for a named parameter.
- Use `.Inner` to access content between opening and closing tags.
- Percent-sign shortcode delimiters process output as Markdown; angle-bracket delimiters leave it as is.
- Shortcodes cannot be used in front matter.
- A shortcode requiring substantial logic is better implemented as a partial template.

## 💡 Ideas to Use It

- Make a reusable card for links to documentation pages.
- Use a wrapping shortcode for notes or cautions in content.
- Move complex presentation logic into a partial template.

## 🧩 What shortcodes are

A shortcode is a call in a content file to a built-in or custom template. It can provide elements that Markdown alone does not cover, such as a YouTube player or a card, without repeating raw HTML on each page.

## ✍️ Calling a shortcode

A call names the shortcode and can supply values by position or by name. For example, a video call might pass an ID followed by a title, while a card call might use named `title` and `link` values. A shortcode that contains content, such as a note around the word “Careful,” needs both opening and closing tags.

| Delimiter style | How Hugo handles the output |
| --- | --- |
| Angle brackets | Uses the output as is |
| Percent signs | Processes the output as Markdown |

## 🛠️ Creating a custom shortcode

Create an HTML template at:

```text
layouts/shortcodes/name.html
```

Within that template, `.Get 0` reads the first positional parameter, `.Get "title"` reads a named parameter, and `.Inner` reads the wrapped content.

## ⚠️ Limits and scope

Shortcodes belong in content, not front matter. Keep them small; if one needs substantial logic, use a partial template instead.

---

Source: <https://gohugo.io/content-management/shortcodes/>
