---
title: "VS Code: Font Size and Window Zoom in Smaller Steps"
linkTitle: "VS Code: Font and Zoom Steps"
description: "Why Cmd and + makes VS Code too big in one step, and how to change the window zoom, the editor font, the terminal and the Markdown preview in smaller steps."
weight: 47
type: docs
---

Pressing `Cmd` and `+` in VS Code makes everything much bigger in one step. This page explains why, and how to change the sizes in smaller steps.

These are settings on your machine, **not** part of the project. The commands and settings below were checked against VS Code 1.140.0.

## In short

- `Cmd` and `+` zooms the **whole window** (menus, sidebar, tabs and editor). One press is about **20%**, which is why it feels like a big jump.
- The window zoom does not have to be a whole number: set `window.zoomLevel` to **`0.5`** for about +10%, or `0.25` for about +5%.
- To make only the **editor text** bigger, change `editor.fontSize` one pixel at a time, or use the command "Editor Font Zoom In".
- The terminal and the Markdown preview have their own font size settings.

## Why one press is a big step {#why}

`Cmd` and `+` runs the command "View: Zoom In", which changes the setting `window.zoomLevel` by **one whole step**. Each step multiplies the size by about 1.2 (a 20% change), so two presses make the window about 44% bigger. You cannot change the size of that step in a setting. You can only set the value of `window.zoomLevel` yourself, and it may be a decimal.

## Option 1: a fractional window zoom {#window-zoom}

Open the settings (`Cmd+,`), search for `window.zoomLevel`, and type a value. Or edit it in your JSON settings (`Cmd+Shift+P`, "Preferences: Open User Settings (JSON)"):

```json
"window.zoomLevel": 0.5,
```

| Value | Size of everything | About |
| ----- | ------------------ | ----- |
| `0` | the default | 100% |
| `0.25` | slightly bigger | +5% |
| `0.5` | a bit bigger | +10% |
| `1` | what one press of `Cmd` and `+` gives | +20% |
| `-0.5` | a bit smaller | -9% |

This changes the interface text (sidebar, tabs, menus) **and** the editor, and it is kept when you restart VS Code. If you then press `Cmd` and `+`, it moves one whole step from your value (for example from `0.5` to `1.5`). The command "View: Reset Zoom" (Command Palette, `Cmd+Shift+P`) returns to the default, `0`. (`Cmd+0` is not the shortcut for it on a Mac: it focuses the sidebar.)

There is also a setting `window.zoomPerWindow` that decides whether each VS Code window remembers its own zoom or all windows share one.

## How to set `window.zoomLevel` {#set-zoom}

There are two ways. The settings window is the easiest.

**1. In the settings window**

1. Press `Cmd+,` to open Settings.
2. Type `zoom level` in the search box at the top.
3. Find **Window: Zoom Level** and type a number in the box, for example `0.5`. Press Enter or click outside the box.

The window changes size immediately. `0` is the default, a positive number makes everything bigger and a negative number makes it smaller.

**2. In the JSON settings file**

1. Press `Cmd+Shift+P` and run **Preferences: Open User Settings (JSON)**.
2. Add the line inside the outer `{ }`, with a comma after the line before it:
   ```json
   "window.zoomLevel": 0.5,
   ```
3. Save the file (`Cmd+S`). The change applies right away.

**Values to try:** `0.25` (a little bigger, about +5%), `0.5` (about +10%), `1` (the same as one press of `Cmd` and `+`, about +20%) and `-0.5` (a bit smaller).

**After you have set it.** Pressing `Cmd` and `+` or `Cmd` and `-` moves one whole step from your value. The command **View: Reset Zoom** (Command Palette, `Cmd+Shift+P`) returns the window to the default level, `0`. To get your own value back after that, type it in Settings again.

## Option 2: only the editor text, in steps of one pixel {#editor-font}

- **The setting `editor.fontSize`** is in pixels, so every change is small. The default on macOS is `12`. Go up one or two at a time:
  ```json
  "editor.fontSize": 14,
  ```
  It changes the text in the editor only, not the sidebar, the tabs or the menus.
- **The command "Editor Font Zoom In"** (`editor.action.fontZoomIn`) makes the editor text one pixel bigger, "Editor Font Zoom Out" one smaller, and "Editor Font Zoom Reset" goes back to your `editor.fontSize`. They have no keyboard shortcut by default: run them from the Command Palette (`Cmd+Shift+P`), or give them keys in "Preferences: Open Keyboard Shortcuts" (`Cmd+K Cmd+S`). Treat the effect as temporary; to keep a size, put it in `editor.fontSize`.
- **The mouse wheel.** If you turn on `editor.mouseWheelZoom` (`"editor.mouseWheelZoom": true`), then holding `Cmd` and moving the mouse wheel over the editor changes the font size in small steps.

## Other places with their own font size {#other}

| Where | Setting | Default |
| ----- | ------- | ------- |
| Terminal | `terminal.integrated.fontSize` | 14 |
| Markdown preview | `markdown.preview.fontSize` | 14 |
| Interface text (sidebar, tabs, menus) | no font setting: use `window.zoomLevel` (option 1) | - |

`editor.fontSize` does not change the interface text, and `window.zoomLevel` changes all of them together.

## Which one to use {#which}

- **Everything is a bit small:** set `window.zoomLevel` to `0.5` (or `0.25`). Everything stays in proportion.
- **Only the code text is small:** raise `editor.fontSize` by one or two.
- **A size that you need only for a moment** (for example when you share your screen): "Editor Font Zoom In" and "Editor Font Zoom Reset", or `Cmd` and `+` and then "View: Reset Zoom" from the Command Palette.
- **The terminal or the Markdown preview is small:** raise its own font size setting.

A change in the settings is applied as soon as you save the settings file. If it does not change right away, reload the window (`Cmd+Shift+P`, "Developer: Reload Window").
