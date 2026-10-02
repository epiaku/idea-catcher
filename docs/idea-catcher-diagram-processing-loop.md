---
title: "Idea Catcher Diagram: The Processing Loop"
linkTitle: "Diagram: Processing Loop"
description: "What happens to a document inside a run: the loop over the inbox, the parsing and Python steps, the YouTube facts with the gap and breaker, the LLM call with its retries and limits, and how each outcome ends."
weight: 62
type: docs
---

This page follows a document **through the program**: the loop that picks documents from the inbox, the steps Python does, the calls to YouTube and to the LLM, and every circuit breaker and limit on the way. Where the files end up is on [the document flow page](../idea-catcher-diagram-document-flow/), and what runs where is on [the architecture page](../idea-catcher-diagram-architecture/).

## 1. The loop over the inbox

This is `catcher run pipeline`, from start to the summary line.

```mermaid
flowchart TB
  start(["catcher run pipeline"]) --> pre{"idea-bucket inbox and<br/>epiaku-docs exist?"}
  pre -->|"no"| stopcfg["log an error, exit code 2<br/>nothing is touched"]
  pre -->|"yes"| lock{"another run in progress?<br/>(a dry run needs no lock)"}
  lock -->|"yes"| stoplock["report a problem, exit code 2<br/>nothing is touched"]
  lock -->|"no"| pull["with --push only:<br/>git pull both repos<br/>a failed pull is reported, nothing changed"]
  pull --> req{"--requeue or<br/>--retry-deferred?"}
  req -->|"yes"| rq["move the archived original back to inbox/<br/>clear the stale copy in output/ and failed/"]
  req -->|"no"| scan
  rq --> scan["scan inbox/<br/>parse every file: class, id, source"]
  scan --> unread["a file that cannot be read:<br/>archive it, move it to failed/"]
  scan --> dup["clips with the same id:<br/>only the longest goes on,<br/>earlier snapshots go to duplicates/"]
  dup --> next{"next document"}
  next -->|"none left"| arts["copy the artifacts:<br/>PDFs and images to epiaku-docs"]
  next -->|"one"| lim{"--limit reached?"}
  lim -->|"yes"| skipped["skipped: run limit reached"]
  skipped --> next
  lim -->|"no"| isyt{"a YouTube clip?"}
  isyt -->|"yes"| wait{"must it wait?<br/>no saved facts and the gap<br/>or the breaker says no"}
  wait -->|"yes"| waiting["waiting: stays in inbox/ untouched<br/>the next run takes it"]
  waiting --> next
  wait -->|"no"| startwork
  isyt -->|"no"| startwork["start work: copy to archive/,<br/>working copy in output/, leave inbox/"]
  startwork --> proc["process the document<br/>see diagrams 2 to 5"]
  proc --> outcome{"outcome"}
  outcome -->|"a valid page"| publ["write the page to epiaku-docs<br/>write the final page to output/"]
  outcome -->|"YouTube closed after the<br/>check (gap or breaker)"| back["waiting: back in inbox/<br/>under the same name"]
  outcome -->|"Ctrl-C or kill"| intr["interrupted: back in inbox/,<br/>commit what is done, stop"]
  outcome -->|"a temporary problem"| def["deferred: the working copy<br/>says why and stays in output/"]
  outcome -->|"a permanent problem:<br/>invalid output, empty or too long"| fail["failed/ with an .error.txt"]
  publ --> next
  back --> next
  def --> next
  fail --> next
  intr --> arts
  arts --> commit["commit only the files the run touched:<br/>epiaku-docs, then idea-bucket"]
  commit --> pushq{"--push?"}
  pushq -->|"yes"| push["git push both repos"]
  pushq -->|"no"| fin
  push --> fin(["print one line per document<br/>and a summary, set the exit code"])
```

- **One broken document never stops the run.** Every document is handled on its own, and an unexpected error becomes `failed` for that document only.
- **`--dry-run`** goes through the same steps, but writes no file and makes no commit. It still calls the LLM if the profile is a real one, unless a good reply is saved (it reads those, free), so use `--profile fake` for a free check. It **never calls YouTube** and does not use up the gap: a clip without saved facts shows `would_fetch`. A dry run takes no run lock.
- The commit is **one commit per repo at the end of the run**, with only the files the run touched.

## 2. One document: parse, analyse, summarize, check, write

`process_note` picks one path for the class. The three paths share the LLM call and the page steps.

```mermaid
flowchart TB
  p0["process_note"] --> p1["choose the LLM profile:<br/>the class default, or --profile"]
  p1 --> p2{"was this backend blocked<br/>earlier in this run?"}
  p2 -->|"yes"| d1["deferred: usage limit reached earlier"]
  p2 -->|"no"| p3{"which class?"}

  p3 -->|"note, ai-chat, web-clip"| t1["build the LLM input:<br/>the text, the title hint, the allowed tags,<br/>the tags from the capture,<br/>the glossary (notes only)"]
  p3 -->|"youtube"| y1["video id from the source link"]
  p3 -->|"youtube-gemini"| g1["video id from the Gemini chat text<br/>no call to YouTube"]

  y1 --> y2["get the facts<br/>see diagram 3"]
  y2 --> y3{"is there a transcript?"}
  y3 -->|"no"| d2["deferred: no transcript"]
  y3 -->|"yes"| y4["build the LLM input:<br/>facts, transcript, description,<br/>the business context"]
  g1 --> g2["build the LLM input:<br/>the Gemini chat only"]

  t1 --> llm["the LLM call<br/>see diagram 4"]
  y4 --> llm
  g2 --> llm

  llm --> post{"which class?"}
  post -->|"note, ai-chat, web-clip"| s1["keep tags from the allowed list,<br/>at most one idea type<br/>keep the language of a note"]
  post -->|"youtube"| s2["keep only the links found in the description<br/>chapters and metrics from the facts, not the LLM<br/>free checks: timestamps and tools"]
  post -->|"youtube-gemini"| s3["keep the metrics and chapters Gemini wrote<br/>no links and no checks:<br/>there is nothing to check against"]

  s1 --> r1
  s2 --> r1
  s3 --> r1
  r1["render the page: Python builds the frontmatter,<br/>a Jinja template builds the body"] --> r2{"validate the page"}
  r2 -->|"valid"| ok["a page, with warnings for YouTube"]
  r2 -->|"problems"| bad["failed: the page is invalid"]
```

**Who owns what.** The LLM returns only JSON (a summary, examples, tips, tools, tags, and for a note the language). **Python owns everything else**: the ids, the file names, the frontmatter, the metrics, the chapters, the tag list, the link check, the page layout and the validation. A number from YouTube is never written by the LLM.

## 3. The YouTube facts: saved facts, the gap, the breaker

Only a `youtube` clip does this. A `youtube-gemini` document never calls YouTube.

```mermaid
flowchart TB
  f0["facts for a video id"] --> f1{"saved in facts/<br/>and not refreshed?"}
  f1 -->|"yes: it has a transcript,<br/>or is less than a day old"| fok["use the saved facts<br/>no call to YouTube"]
  f1 -->|"no"| f2{"YOUTUBE_OFFLINE is on?"}
  f2 -->|"yes"| foff["not available: deferred"]
  f2 -->|"no"| fdry{"a dry run?"}
  fdry -->|"yes"| fwf["would_fetch: no call,<br/>the gap is not used"]
  fdry -->|"no"| f3{"is the breaker open?"}
  f3 -->|"yes"| fblk["wait until it ends<br/>no call is made"]
  f3 -->|"no"| f4{"has the gap passed<br/>since the last fetch?"}
  f4 -->|"no"| fgap["wait, or sleep through it<br/>with --wait-youtube"]
  f4 -->|"yes"| f5["take the slot: the next fetch<br/>is allowed in 2 minutes plus jitter"]
  f5 --> f6["one yt-dlp extraction:<br/>watch page, player data, caption file<br/>10 seconds between the requests"]
  f6 --> f7{"what did YouTube answer?"}
  f7 -->|"the facts"| f8["close the breaker<br/>save facts/video id.json"]
  f7 -->|"429 or a bot check"| f9["open the breaker: 6 hours,<br/>then 12, then 24<br/>the clip goes back to inbox/ as waiting"]
  f7 -->|"another error"| f10["deferred: facts not available<br/>the breaker stays closed<br/>a private or removed video is<br/>remembered for a day"]
```

## 4. The LLM call: retries and limits

One call per document, with two kinds of retry and two kinds of stop.

```mermaid
flowchart TB
  l0["render the prompt for the class<br/>and add the JSON schema"] --> l1["call the backend"]
  l1 --> l2{"what came back?"}
  l2 -->|"a reply"| l3["parse the JSON<br/>and check it against the schema"]
  l2 -->|"a 5xx, a timeout,<br/>a dropped connection"| l4{"was that call 5 of 5?"}
  l4 -->|"no"| l5["wait 2, 4, 8 or 16 seconds"]
  l5 --> l1
  l4 -->|"yes"| l6["backend unavailable: deferred"]
  l2 -->|"the budget is used up"| l7["block this backend for the rest of the run<br/>deferred, and one error line says<br/>how many documents are waiting"]
  l2 -->|"a 429 rate limit or a bad key"| l8["block this backend for the rest of the run<br/>deferred"]
  l2 -->|"another 4xx"| l6
  l3 -->|"valid"| l9["the result: output, model,<br/>tokens, number of attempts"]
  l3 -->|"invalid, the first time"| l10["ask again, with the error message added"]
  l10 --> l1
  l3 -->|"invalid, the second time"| l11["invalid output: failed"]
```

## 5. The whole path of a YouTube clip

The same steps in time order, with everything that touches the document.

```mermaid
sequenceDiagram
  autonumber
  participant R as Run loop
  participant F as idea-bucket files
  participant G as YouTube gate and facts
  participant Y as YouTube through yt-dlp
  participant L as LLM
  participant P as Python checks
  participant D as epiaku-docs

  R->>F: scan inbox: a clip with a YouTube link
  R->>G: must this clip wait?
  G-->>R: no, the gap has passed and the breaker is closed
  R->>F: start work: archive copy, working copy in output
  R->>G: facts for the video id
  G->>Y: watch page, player data, caption file, 10 s apart
  Y-->>G: the info and the captions
  G->>F: save facts/video id.json
  G-->>R: the facts with the transcript
  R->>L: prompt with facts, transcript, description and business context
  L-->>R: JSON: summary, examples, tips, tools, links
  R->>P: verify the links, check timestamps and tools, clean the tags
  P-->>R: the page and any warnings
  R->>D: write the page
  R->>F: write the final page in output
  R->>D: commit and push at the end of the run
```

## What Python does to a document

| Step | Input | Output | Where in the code |
| --- | --- | --- | --- |
| Scan and parse | A markdown file in `inbox/` | A note in memory: frontmatter, text, source link | `inbox.py` (`scan_inbox`) |
| Detect the class | The `type` or `class` line, else the source link | `note`, `ai-chat`, `web-clip`, `youtube` or `youtube-gemini` | `doctypes.py` (`detect`) |
| Give a stable id | The class, the source link or a new id | A page id (a video id for YouTube) | `doctypes.py` |
| Compare clips | All clips with one id | The longest one goes on, the rest to `duplicates/` | `inbox.py` (`is_snapshot_of`) |
| Name it | The date, a short id, the title | `YYYYMMDD-<id>-<title>.md`, used in every folder | `inbox.py` |
| Start work | The note | The archive copy and the working copy | `inbox.py` (`start_work`) |
| Get the facts (YouTube) | The video id | Facts: counts, description, chapters, transcript | `youtube/access.py`, `gate.py`, `cache.py`, `facts.py` |
| Build the LLM input | The text, the facts, the tag lists, the glossary, the context | One dictionary for the prompt | `inputs.py` |
| Call the LLM | The prompt and the schema | A validated summary | `llm/service.py`, `llm/backends` |
| Check the summary (YouTube) | The summary and the facts | Verified links, warnings for a wrong timestamp or a tool that is not in the transcript | `youtube/checks.py` |
| Normalize the tags | The tags from the LLM and the capture | Tags on the allowed list, at most one idea type | `tags.py` |
| Render the page | The summary, the facts, the note | Frontmatter and body | `render.py` and `templates/` |
| Validate the page | The page | A list of problems, empty when it is fine | `validate.py` |
| Write | The page | The page in `epiaku-docs`, the final page in `output/` | `publish.py` |
| Commit | The touched files | One commit per repo | `core/git.py` |

## How a document ends

| What happened | Status | Where the file is | What you do |
| --- | --- | --- | --- |
| A valid page was written | `published` | A page in `epiaku-docs`, the final page in `output/`, the original in `archive/` | Nothing |
| A YouTube clip must wait for the gap or the breaker | `waiting` | Still in `inbox/`, untouched | Run again later |
| The gap closed after the check (another run used it), or YouTube answered with a block | `waiting` | Back in `inbox/` under the same name | Run again later |
| Ctrl-C or `kill` during a document | `interrupted` | Back in `inbox/` under the same name. What was done is committed. The run exits with code 2 | Run again |
| The LLM or its budget is unavailable, or YouTube gave no facts | `deferred` | `output/`, `stage: deferred` and the reason | `--retry-deferred` (all) or `--requeue NAME` (one) |
| The LLM output was invalid twice, the page is invalid, the document is empty or longer than `LLM_MAX_INPUT_CHARS`, or an unexpected error | `failed` | `failed/` with an `.error.txt` | Read the error, fix the cause, `--requeue` |
| A dry run found a YouTube clip without saved facts | `would_fetch` | Nothing was written, YouTube was not called | Run without `--dry-run` |
| A shorter clip of the same conversation | `duplicate` | `duplicates/` | Nothing |
| It was a dry run | `would_publish` | Nothing was written | Run without `--dry-run` |

## The circuit breakers and limits

| Protection | Where | What it does |
| --- | --- | --- |
| **The YouTube gap** | Before every fetch | At least 2 minutes (plus up to 5 minutes of jitter) between two fetches, so the calls are spread out. A clip that must wait stays in `inbox/`, or goes back there when the gap closed after its check |
| **The YouTube breaker** | After a 429 or a bot check | No call to YouTube for 6 hours, then 12, then 24. A fetch that works closes it. Retrying during a block would only make it longer |
| **Saved facts** | Before every fetch | A video is fetched once, ever. A retry, a requeue or a rerun reads `facts/<id>.json` |
| **The offline switch** | `YOUTUBE_OFFLINE=1` | Never call YouTube (development and tests) |
| **LLM retries** | Inside the backend | A 5xx, a timeout or a dropped connection is retried up to 5 calls, with 2, 4, 8 and 16 seconds between them. One more call if the JSON is invalid |
| **The usage-limit block** | During a run | A used-up budget, a 429 or a bad key blocks that backend for the rest of the run. The other documents that need it are deferred at once, without a call |
| **The run lock** | Start of a real run | Only one `catcher run pipeline` at a time on a machine (`pipeline.lock`). A second one is refused. A dry run needs no lock |
| **A damaged gate file** | The YouTube gate | The file is kept as `youtube-gate.corrupt` and the gate closes for the block hours, because losing an active block is the expensive mistake |
| **Empty or too long** | Before the LLM call | A document that is empty or longer than `LLM_MAX_INPUT_CHARS` fails without a call |
| **One broken document** | The loop | An unexpected error fails that one document only |
| **`--limit N`** | The loop | At most N documents are started. The rest stay in the inbox |
| **`--dry-run`** | The whole run | No file is written and nothing is committed |
