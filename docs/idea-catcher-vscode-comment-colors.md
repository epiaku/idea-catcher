---
title: "VS Code: Readable Comments in the Atom One Dark Theme"
linkTitle: "VS Code: Comment Colours"
description: "Why comments and Markdown block quotes were hard to read in the Atom One Dark theme, how to override parts of a theme without changing it, and the exact settings we added."
weight: 45
type: docs
---

Comments and Markdown block quotes were hard to read in VS Code with the **Atom One Dark** theme. This page explains why, how you can override parts of a theme, and what we changed in the settings.

This is a setting on your machine (`~/Library/Application Support/Code/User/settings.json` on macOS), **not** part of the project.

## In short

- The theme draws comments **and** Markdown block quotes (the lines that start with `>`) in the same dark grey, `#5C6370`, on a `#282c34` background. That is a contrast of about **2.3:1**, below the 4.5:1 that is usually recommended for text.
- You do not have to change the theme. VS Code lets you **override single colours** in your own settings with `editor.tokenColorCustomizations`.
- We set comments and block quotes to a readable green, **`#73a85f`** (about 5.0:1), in italic.

## The theme {#theme}

The theme is "Atom One Dark" from the extension `akamud.vscode-theme-onedark` (shown in your settings as `"workbench.colorTheme": "Atom One Dark"`). Its definition has these rules for the text we looked at:

| Scope | Colour in the theme | What it is |
| ----- | ------------------- | ---------- |
| `comment` | `#5C6370`, italic | comments in every language (`#` lines in `.http`, YAML, shell and Python, `//` in JSON with comments) |
| `punctuation.definition.comment` | `#5C6370` | the comment marker itself |
| `markup.quote.markdown` | `#5C6370`, italic | Markdown block quotes (`> text`) |

The block quote was the surprise: it is not a comment, but the theme uses the same grey for it. That is why the first fix, for comments only, did not change the grey text you saw in a `.md` file.

## How to override a theme {#override}

You can override parts of a theme without editing the theme or its extension:

- **`editor.tokenColorCustomizations`**: the colours and styles of text (comments, strings, keywords, Markdown). This is what we used.
- **`workbench.colorCustomizations`**: the colours of the interface (sidebar, tabs, selection, the editor background).
- **`editor.semanticTokenColorCustomizations`**: colours that come from a language server instead of the theme's text rules.

Rules in your settings win over the theme's rules. If you wrap them in the theme's name (`"[Atom One Dark]"`), they only apply while that theme is active: if you switch theme, the override is ignored and the other theme looks as designed.

Where to put them:

- **User settings** (`Cmd+Shift+P`, "Preferences: Open User Settings (JSON)"): personal, for every project. We used this.
- **Workspace settings** (`.vscode/settings.json` in a project): only for that project. It is committed with the project unless the file is ignored.

You could also make a complete copy of the theme as your own extension. For one or two colours the overrides above are simpler, and they keep working when the theme updates.

## What we changed {#changes}

We changed the settings in three steps:

1. **Comments.** Added a rule for `comment` and `punctuation.definition.comment` with a green, `#73a85f`, in italic.
2. **Block quotes, first try.** A screenshot of a Markdown file showed the `>` lines still in grey. We looked up the theme file and found the scope `markup.quote.markdown`. We added a rule for it (`markup.quote.markdown` and `markup.quote`) in the theme's normal text colour, `#abb2bf` (about 6.6:1).
3. **Block quotes, same as comments.** You asked for the quotes in the same green as the comments, so that rule now also uses `#73a85f`.

The setting as it is in your user settings now:

```json
"editor.tokenColorCustomizations": {
  "[Atom One Dark]": {
    "textMateRules": [
      {
        // comments: a green that stays readable on the dark background (about 5:1 contrast)
        "scope": ["comment", "punctuation.definition.comment"],
        "settings": { "foreground": "#73a85f", "fontStyle": "italic" },
      },
      {
        // Markdown block quotes (the lines that start with ">")
        "scope": ["markup.quote.markdown", "markup.quote"],
        "settings": { "foreground": "#73a85f", "fontStyle": "italic" },
      },
    ],
  },
},
```

(VS Code settings files allow `//` comments and trailing commas.) Nothing else in the settings file was changed, and VS Code applies the change as soon as the file is saved.

## Why this green {#why-green}

A really dark green is not readable on a dark background. Contrast ratios against the theme's background `#282c34` (4.5:1 or more is the usual target for text):

| Colour | Contrast | Remark |
| ------ | -------- | ------ |
| `#5C6370` (the theme's grey) | 2.3:1 | the original: hard to read |
| `#006400` (a true dark green) | 1.9:1 | worse than the grey |
| `#6b9e5a` | 4.4:1 | about the darkest green that is still readable |
| **`#73a85f` (used)** | **5.0:1** | muted green, readable |
| `#7cb36a` | 5.7:1 | lighter green |
| `#abb2bf` (normal text) | 6.6:1 | used briefly for the quotes |

## Change or undo it {#change}

- **Another colour:** edit the `foreground` value of the rule in your settings.
- **Upright text:** remove `"fontStyle": "italic"` from a rule.
- **Back to the theme's own colours:** delete the whole `editor.tokenColorCustomizations` block, or switch to another theme (the rules only apply to "Atom One Dark").
- **Different colours for comments and quotes:** give the two rules different `foreground` values.

## Find the colour setting of other text {#inspect}

If another kind of text is hard to read, put the cursor on it and run **"Developer: Inspect Editor Tokens and Scopes"** (`Cmd+Shift+P`). It shows the scope names of that text (for example `comment.line.number-sign.http` or `markup.quote.markdown`) and which rule gives the colour. Add a rule for that scope in the same way as above, in the same `textMateRules` list.
