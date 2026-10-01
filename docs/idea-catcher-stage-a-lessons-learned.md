---
title: "Idea Catcher Stage A: What We Built and What We Learned"
linkTitle: "Stage A Lessons Learned"
description: "What Stage A of the Idea Catcher delivered, how the design changed while building it, and the lessons for stage B: facts and prose, degrading instead of failing, free LLM providers, YouTube, testing and documentation."
weight: 50
type: docs
---

Stage A is the local Python pipeline: it turns captures in the `idea-bucket` repo into pages in `epiaku-docs`, run by hand from the Mac. This page records what was built, where the real result differs from the [plan](../idea-catcher-service-architecture/#mvp-stage-a), and what we learned, so stage B starts from facts and not from memory.

**Period:** 2026-09-27 to 2026-10-01 (five days, about 50 commits)
**Size at the end:** about 3,500 lines of Python, about 4,400 lines of tests (391 tests), five document classes
**Written:** 2026-10-01, from the git history, the test repos and the reruns of the real pages

## What Stage A delivered

| Area | Result |
| --- | --- |
| **Document classes** | `note` (dictated or typed), `ai-chat`, `web-clip`, `youtube` (a clipped YouTube page) and `youtube-gemini` (a Gemini chat about a video). Files that are not markdown (PDFs, images) are sent to `epiaku-docs` as artifacts |
| **Flow** | `inbox/` → calculated file name → original to `archive/` → working copy in `output/` → page in `epiaku-docs`; `failed/` and `duplicates/` for the rest. A run only looks at `inbox/` |
| **LLM** | One call per document, on three profiles (`notes` on FreeLLMApi, `clippings` and `youtube` on the OpenAI API) and a `fake` profile for tests. Output is JSON checked by a schema |
| **Python-owned parts** | ids, file names, frontmatter, tags, metrics, chapters, link checks, git commits |
| **Commands** | `run pipeline` (`--file`, `--requeue`, `--limit`, `--dry-run`, `--push`, `--profile`), `scan`, `reason`, `render`, `youtube facts`, `testdata reset`, `version` |
| **Safety** | Overwrite by id (reruns do not duplicate), validation before writing, per-note errors, usage-limit blocking, `--push` off by default |
| **Test data** | A committed data set and `testdata reset`, which makes throw-away git repos in `tmp/ic`. Every run there is one commit, so two runs can be compared with `git diff` |

The "done when" of the plan was met: a real run turns every class in the inbox into correct pages.

## How the design changed while building it

The plan was good enough to start with, and it changed a lot. Most changes came from running real data, not from thinking.

| Date | Built first | Replaced by | Why |
| --- | --- | --- | --- |
| 09-27 → 09-28 | A `claude -p` subscription backend, with an evening run | API-key profiles only (three of them) | The CLI hangs, changes output and hits limits at unpredictable times, and it needs a login |
| 09-28 | A `staging/` folder for work in progress | The inbox-only flow: `inbox/`, `archive/`, `output/`, `failed/`, `duplicates/`, with a calculated file name per document | Stable names make reruns overwrite instead of duplicate, and the folder a file is in shows its state without a database |
| 09-27 → 09-28 | A second LLM call that reviews a YouTube summary against the facts | One call per class plus **free Python checks** (timestamps after the end of the video, tools not in the transcript) | A second model re-checking a summary built from the real transcript costs money and adds no new source of truth |
| 09-28 | `youtube-gemini` checks Gemini's answer against our transcript | It only restructures Gemini's answer, with no YouTube call | It then keeps working when YouTube blocks us, and it stays a clean "second opinion" |
| 09-30 | Retrying by moving a file back from `archive/` by hand | `--requeue NAME`, which moves it back and clears what the earlier run left | Three copies of one document (archive, output, inbox) are confusing, especially after a crash |
| 10-01 | "Exactly one idea-type tag" per page | At most one, optional | A good page (a Dutch note) failed for lacking a tag, and most pages on the site have no tags |
| 10-01 | A "this video was also summarized…" link between the two YouTube pages | Nothing | It was not needed |

**Added, not in the plan:** the `web-clip` class, artifacts, `--requeue`, retries inside a call, a glossary, Dutch translation with the original language recorded, YouTube chapters, links and the upload date, a business context for the Channel Application section, clear errors for wrong paths, a version string, **protections against a YouTube IP ban** (saved facts per video, one paced extraction, a gap between fetches, a breaker) and the test data tooling.

## Lessons learned

### Design

1. **Python owns the facts, the LLM writes prose.** Views, likes, subscribers, dates, chapters, ids and names come from code. When the LLM must pick something that code can check (links from the video description), code keeps only what it can find in the source. A made-up value then cannot reach a page.
2. **Degrade, don't fail.** Optional data must never fail a good page. Tags are optional, a bad `language` or a bad chapter is dropped, a link that is not in the description is dropped. A page fails only when it is really broken (empty body, bad frontmatter, an unknown shortcode, two idea-type tags). A rule that sounded neat in the design (exactly one tag) cost a real page.
3. **A document is in one place at a time.** Moving (not copying) on a requeue and deleting stale copies means that after a crash there is exactly one thing to look at.
4. **Keep the untouched original.** `archive/` made three things possible: rerunning a document, auditing the LLM (compare the archived Gemini chat with the page it became), and finding errors we caused (FastAPI moved into the tool list by our own reformat step).
5. **Fail loudly on setup, quietly on content.** A wrong `--ideas` path used to give "nothing to do" and exit 0. Now it logs an error and exits 2. A single bad note is reported and the run goes on.
6. **A name that is also a rule needs one owner.** The version lives in one string, the tags in one file, the prompt version in each prompt and in each page it made.

### Working with the LLM

7. **The free provider is unstable.** FreeLLMApi answered 502 or timed out often (a call can take a minute), and the model behind `auto` changed between calls. Retrying up to five calls with a doubling wait works because each call may be routed to a different provider.
8. **Do not classify an error from words in its message.** A 502 that said "retry time budget exceeded" matched our "budget" pattern and was reported as an empty API budget. Look at the status code first.
9. **One prompt, one job.** The Gemini prompt said "only restructure Gemini's answer" and, in the same breath, "give 2 to 4 concrete suggestions for Epiaku". The model wrote its own advice and presented it as Gemini's. Direct pages (where the LLM must write the advice) get the business context. Gemini pages (which only restructure) do not.
10. **A prompt that copies a source fails when the source is junk.** "Summarize the YouTube description" worked for a tutorial and produced a page about a free masterclass for a video whose description was only promotion. Say what to do when the source is empty, promotional or only links.
11. **Generic context gives generic advice.** One sentence about Epiaku produced advice that fits any channel. A short context file (what the company does, the audience, what is still being set up) made the Channel Application specific.
12. **Dictated notes need help.** A glossary of often-misheard words and a translation step (Dutch to English, with the original language kept in the frontmatter) fixed the main problems of notes.
13. **Runs vary.** The same input gave different tags and wording on each run. Judge a prompt change on several pages, not one, and keep the prompt version on every page so two runs can be compared.
14. **Do not rely 100% on the LLM, and you cannot fix someone else's errors.** A Gemini page repeated Gemini's mistakes ("30% to 40% *of* sales" where the video says *more* sales; "the video warns against sales calls" where it recommends them for expensive offers). Our code cannot see them. The direct page is the check when a claim matters.
15. **Check your own checks.** Our free "tool not in the transcript" check raised a false alarm because captions write "type form" for Typeform. A wrong warning on a correct page makes people ignore the real ones.

### YouTube

16. **Two methods, two jobs.** The direct page has real metrics, timestamps, chapters and free checks, but needs YouTube to answer. The Gemini page needs no YouTube call and often has more concrete numbers, but nothing can be verified. The direct page is the default, the Gemini page the fallback and a second opinion. See [the comparison](../idea-catcher-youtube-methods-comparison/), done on two videos and checked against the transcripts.
17. **Do not hit YouTube while developing.** Heavy live testing on 2026-09-28 got this machine rate-limited (429) on both the transcript API and yt-dlp. It worked again days later. The protections that came out of it are built: the facts are saved once per video, one paced extraction, a 10 minute gap between fetches, and a breaker that stops all calls for 6 to 24 hours after a block. See [YouTube IP bans and the queue](../idea-catcher-youtube-bans-and-queue-options/). The tests now use saved data (a caption file and a trimmed `info.json` of the same video) and never call YouTube. See [the research](../idea-catcher-youtube-transcript-research/) and [the command-line tests](../idea-catcher-youtube-transcript-cli-tests/).
18. **Facts change.** Views move, and chapters can appear on a video days after it was published (the same video had no chapters at 11:27 and nine at 12:58, with the description unchanged). The fetch date is stored on every page, and a rerun gives newer facts.
19. **Descriptions are mostly promotion.** Do not summarize them, and do not trust a link in them without checking. A description is useful mainly for the code and sample-data links, and only when code verifies each URL.

### Testing

20. **Tests never call the real LLM or YouTube.** Live runs are done by hand. Tests use a fake backend, saved responses, and real git in temporary folders for the pipeline flow.
21. **Do not hardcode what the test data holds.** A test that listed folder and file names broke when the data was refreshed. The tests now scan the data when they run and check that `testdata reset` copies exactly that.
22. **Trim saved responses.** A raw `yt-dlp` dump was 660 KB, nearly all signed URLs. Keeping only the fields the code reads made it 380 bytes. Keep the files of one video together so a test can run end to end on one real example.
23. **Pin decisions with tests.** The tests say that a Gemini page has no cross-link, that a missing idea-type tag is fine, and that a direct page never takes chapters from the LLM. When a decision is reversed, the test is what shows it.
24. **Run the checks on every commit.** The pre-commit hook (ruff, format, pyright, unit and component tests) caught mistakes early. A limit: without interactive staging a commit goes by whole files, so mixed changes land in one commit.

### Operating it

25. **One commit per run in the test repos is a gift.** `git diff` between two runs of a page showed exactly what a prompt change did, and the commit times showed which runs had been done.
26. **Say what a command writes.** `render` writes only a page (no `archive/`, no `output/`, no commit), `run pipeline` does the whole flow. Mixing them up made people look for files that were never meant to exist.
27. **Names must match completely.** `--file` and `--requeue` need the whole name; a shortened name in a recipe found nothing.
28. **A recipe is a test.** Documented commands must run against the committed test data. Renamed test files broke two recipes, and zsh does not treat `#` as a comment by default, so a `;` in an inline comment became a command.

### Documentation and method

29. **Design docs drift within days.** By the end, the pipeline and architecture docs still talked about a reviewer, "exactly one tag" and a `review_profile`, and the obsolete folder design had its own page. Update the doc in the same change as the behavior, or at the latest at the end of a stage. Keep an "OBSOLETE" banner for what was dropped, and say what replaced it.
30. **Verify a claim before you write it down.** Several statements were wrong at first and were corrected only after looking at the data: that a failed note could not be requeued (it can, its original is in `archive/`), that Gemini's metrics were not exact (a timing difference), and that a page could not be published without an idea-type tag (it was only our rule). Look in the archive, the facts file or the transcript.
31. **Keep git as the safety net.** One automated edit of a long doc cut 169 lines by mistake and was undone with `git checkout`. Commit often, and read a diff before moving on.
32. **Ask before adding a rule.** The tag rule came from the first design and nobody questioned it until it failed a note. When a rule can fail a page, check that something depends on it.

## What we got wrong, in short

A reviewer call, a staging folder and a subscription backend were built and then dropped (about a day each). The first requeue copied a file and left three copies. A label line "reported by Gemini" was added to the Gemini metrics and removed again, because everything on that page is from Gemini. A cross-link between the two YouTube pages was added and removed. The business context was first sent to both YouTube classes. Hardcoded test data names broke tests. None of these cost much, because each was found in a rerun or a review, and the tests and git history made the change cheap.

## Left open (not fixed, on purpose)

- **Gemini's own errors** pass through. Rely on the direct page when a claim matters.
- **Tag drift.** Do nothing now. After about 20 pages, open a few tag pages and see if pages appear where they do not belong.
- **Retry layers.** In-call retries (stage A) and a job-level `retry_delay` (stage B) must be settled together. See the [open questions](../idea-catcher-service-architecture/#open-questions).
- **YouTube bans.** The protections are built (saved facts, pacing, a gap, a breaker, an offline switch). What is left is Stage B: the gate state in Postgres, the queue and the pull and publish schedules, and the backfill import. See [YouTube IP bans and the queue](../idea-catcher-youtube-bans-and-queue-options/). Try `YOUTUBE_SKIP_MANIFESTS=1` by hand once.
- **A capture that cannot be read** and a **duplicate** are not found by `--requeue`; they are moved back by hand.
- **No Hugo build check** and **no repeatable model test suite** yet.
- **A name prefix** is not accepted by `--file` and `--requeue`.
- **Long inputs** go whole to the OpenAI models (1M context). Free models still fail on long texts.

## Rules of thumb for stage B

1. Reuse the stage A functions as they are. The queue and the API only call them.
2. Keep the rule "Python owns facts, the LLM writes prose, code verifies what code can".
3. Treat a missing optional field as normal, and fail a document only when it is broken.
4. Keep tests offline, and keep the test data scanned, not listed.
5. Update the design doc in the same change as the behavior.
6. Log the version at the start of every run, and the prompt version on every page.
