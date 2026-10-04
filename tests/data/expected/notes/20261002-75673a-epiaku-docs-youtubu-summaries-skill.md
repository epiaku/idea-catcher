---
title: YouTube Summaries Skill for Hugo Docs
description: Create a skill to find YouTube links in Hugo docs and generate missing summaries.
date: '2026-10-02'
weight: 100
type: docs
id: 310c34
destination: notes
stage: published
created_by: idea catcher
tags:
- tech-note
- hugo
- skills
- automation
- idea-catcher
source_file: notes/20261002-75673a-epiaku-docs-youtubu-summaries-skill.md
original_filename: Epiaku-docs youtubu summaries skill.md
language: en
llm:
  profile: notes
  backend: freellmapi
  model: deepseek-ai/DeepSeek-V4-Flash-0731
  prompt_version: note-7
---
- Create new skill: `epi-hugo-youtube-summaries`.
- When invoked, find all YouTube links in Hugo docs and check for a summary. If none, generate the summary and store it in `idea-bucket/youtube`.
- Consider using an API for deterministic Python part or full generation, or an MCP server that calls the API server.
- Implement after the idea bucket flow for daily jobs is ready.
