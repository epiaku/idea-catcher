---
title: 'Idea Catcher: Auto-Create Obsidian Cards from YouTube Links'
description: Automatically create formatted Obsidian cards from YouTube links in Idea Catcher by fetching the video descriptions.
date: '2026-10-02'
weight: 100
type: docs
id: 988a85
destination: notes
stage: published
created_by: idea catcher
tags:
- todo
- obsidian
- automation
- idea-catcher
source_file: notes/20261002-d42425-idea-catcher-youtube.md
original_filename: Idea catcher YouTube.md
language: en
llm:
  profile: notes
  backend: freellmapi
  model: gemini-3.6-flash
  prompt_version: note-7
---
When Idea Catcher for Obsidian receives files:

- Check if the file is a YouTube link.
- If it is a YouTube link, fetch its description.
- Automatically create a card in the required format.
