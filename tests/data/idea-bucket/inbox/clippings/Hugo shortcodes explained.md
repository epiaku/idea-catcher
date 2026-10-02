---
title: "Hugo shortcodes explained"
source: "https://gohugo.io/content-management/shortcodes/?utm_source=newsletter&utm_medium=email#use-hugos-built-in-shortcodes"
author:
  - "[[Hugo Documentation]]"
published: 2025-11-04
created: 2026-09-24
description: "Shortcodes are simple snippets inside your content files that call built-in or custom templates."
tags:
  - "clippings"
---
# Shortcodes

Shortcodes are simple snippets inside your content files that call built-in or custom templates. Markdown is deliberately small, so shortcodes give you a way to add richer elements, such as a YouTube player or a card, without writing raw HTML in every page.

## Use a shortcode

Call a shortcode with the `{{< name >}}` notation. Pass parameters by position or by name:

```
{{< youtube-lite dQw4w9WgXcQ "A title" >}}
{{< card title="Hello" link="/docs/" >}}
```

Shortcodes that wrap content have a closing tag: `{{< note >}}Careful{{< /note >}}`.

## Create your own

Put a template in `layouts/shortcodes/name.html`. Inside it, read the parameters with `.Get 0` for a positional parameter or `.Get "title"` for a named one, and use `.Inner` for the content between the opening and closing tags.

## Good to know

- A shortcode with the `{{% name %}}` notation has its output processed as Markdown, while `{{< name >}}` is used as it is.
- Shortcodes cannot be used inside front matter.
- Keep them small. A shortcode that needs a lot of logic is better as a partial template.

Related: Hugo templates, partials, page bundles.

Menu · Documentation · Community · Sign in · Accept cookies
