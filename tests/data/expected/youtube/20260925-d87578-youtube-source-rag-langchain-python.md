---
title: 'RAG + Langchain Python Project: Easy AI/Chat For Your Docs'
description: Build a Python RAG app that retrieves relevant document passages and uses OpenAI to answer questions with source references.
date: '2026-09-25'
weight: 100
type: docs
id: tcqEUSNCn8I
destination: youtube
stage: published
created_by: idea catcher
tags:
- tech-note
- llm-models
- second-brain
- idea-catcher
source: https://www.youtube.com/watch?v=tcqEUSNCn8I
source_file: clippings/20260925-d87578-youtube-source-rag-langchain-python.md
original_filename: youtube source - RAG + Langchain Python.md
video_id: tcqEUSNCn8I
llm:
  profile: youtube
  backend: openai
  model: gpt-6-sol
  prompt_version: youtube-6
---
**RAG + Langchain Python Project: Easy AI/Chat For Your Docs** by _pixegami_

## 📝 Summary

The tutorial builds a document-question-answering app with LangChain, OpenAI and ChromaDB. It covers loading and splitting files, creating embeddings, retrieving relevant chunks, and using those chunks to generate answers with source references.

## 📊 Metrics

| Metric              | Value |
| ------------------- | ----- |
| Metrics As Of       | 2026-10-02 |
| Published           | 2023-11-20 |
| Views               | 486,937 |
| Likes               | 13,054 |
| Channel Subscribers | 85,100 |

## 🗂️ Chapters

- 0:00 What is RAG?
- 1:36 Preparing the Data
- 5:05 Creating Chroma Database
- 6:36 What are Vector Embeddings?
- 9:38 Querying for Relevant Data
- 12:47 Crafting a Great Response
- 16:18 Wrapping Up

## 🎯 Main Purpose

Show how to build a retrieval-augmented generation app that answers questions using supplied documents rather than relying solely on a model's general knowledge. The workflow turns text into searchable chunks, retrieves the best matches for a question, and includes them in the prompt used to generate a response.

## 💡 Key Examples

- An Alice in Wonderland markdown file is split from one document into 282 chunks (4:14), then queried about how Alice meets the Mad Hatter (12:09).
- AWS Lambda documentation spread across multiple markdown files is used to answer which languages or runtimes Lambda supports, with references to different source files (15:29).
- The presenter compares embedding distances between “apple” and “orange,” “beach,” and “iPhone” to illustrate semantic similarity (8:20).

## ✅ Action Plan

- Put the markdown files you want to query in a data folder (1:51).
- Load the files as LangChain documents, retaining their source metadata (2:19).
- Split long documents into overlapping chunks (3:10).
- Generate OpenAI embeddings for the chunks and save them in a persistent Chroma database (5:05).
- Load the database with the same embedding function and retrieve the chunks most relevant to a question (9:38).
- Place the retrieved chunks and question in a prompt, generate an answer, and display source references from the chunk metadata (12:47).

## 🛠️ Tech Stack

- Python
- LangChain
- OpenAI
- ChromaDB
- AWS Lambda
- SQLite

## 💬 Tips

| Tip | Explanation | How to Apply |
| --- | ----------- | ------------ |
| Keep source metadata | Loaded documents and chunks retain the file path, which can be shown alongside an answer (2:57, 14:07). | Preserve source fields while loading and splitting files, then extract them from retrieved chunks when displaying results. |
| Tune chunk size and overlap | Smaller chunks may be focused but lack context; the demonstration uses 1,000-character chunks with 500-character overlap (3:46, 12:24). | Test different chunk sizes against real questions and inspect whether retrieved passages contain enough context to answer. |
| Check retrieval quality before answering | The tutorial suggests returning early when there are no suitable matches rather than proceeding to response generation (11:27). | Inspect retrieval results and scores, and establish an appropriate cutoff for the scoring method in use. |
| Persist the database | Saving Chroma to disk lets the app load an existing index instead of rebuilding it for every query (5:27). | Set a persistent directory when creating the database and load that directory in the query script. |

## 📺 Channel Application

- Build a question-answering prototype for Idea Catcher documentation pages: chunk the saved text, index it in Chroma, and show source references with answers.
- Use an Epiaku app's markdown documentation as the data source for a small support-chat prototype, following the video's retrieval-and-prompt workflow.
- Make a Vibe Coding Tech Stack demo that walks through loading files, creating the vector database, and querying it with source references.

## 🔗 Links

- [Tutorial code](https://github.com/pixegami/langchain-rag-tutorial)
- [Sample AWS Lambda documentation](https://github.com/awsdocs/aws-lambda-developer-guide)
- [Sample Alice in Wonderland text](https://www.gutenberg.org/ebooks/11)

## 📄 YouTube Source

{{< youtube-lite tcqEUSNCn8I `RAG + Langchain Python Project: Easy AI/Chat For Your Docs` >}}
