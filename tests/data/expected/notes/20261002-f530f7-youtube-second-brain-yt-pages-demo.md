---
title: YouTube Second Brain Demo
description: Build a YouTube second brain that summarizes videos into wiki pages, with a React frontend, MCP server, and optional Postgres backend.
date: '2026-10-02'
weight: 100
type: docs
id: c7c422
destination: notes
stage: published
created_by: idea catcher
tags:
- digital-product-idea
- second-brain
- ai-agents
- automation
source_file: notes/20261002-f530f7-youtube-second-brain-yt-pages-demo.md
original_filename: YouTube second brain yt pages demo.md
language: en
llm:
  profile: notes
  backend: freellmapi
  model: dots-studio/dots-3-note-preview:free
  prompt_version: note-7
---
## 🧠 Core Concept & Frontend

Build a simple second brain for YouTube:

- Keep a folder or file of YouTube links, or select a channel.
- Use a YouTube summarizer to create pages for each video.
- Build an LLM wiki based on those docs.

Create a React frontend so you can upload a URL, see the list of videos, open a summarized page, or chat with an agent that prompts the pages.

## 🛠️ Build Process & Architecture

Process:

- Create design and implementation docs using Opus.
- Run a second-opinion brainstorming skill with Sonnet or Opus.
- Implement using an AI agent.
- Test and refactor.

Architecture options for a web extension that catches a URL and passes it to the system:

- Run locally or in the cloud.
- Use a Postgres backend.

Or build the demo with increasing functionality: a basic version running locally, then add Postgres, then run everything in the cloud.

## 🔌 MCP Server & Use Cases

As a final feature, build an MCP server that serves the second brain. The frontend web chat can consume the MCP, and you can also consume it from a harness like Claude Code or VS Code.

Other use cases for the final MCP second brain include company domain knowledge, dev docs, and more.
