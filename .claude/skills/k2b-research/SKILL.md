---
name: k2b-research
description: Deep dive into external topics -- scan for new AI tools, techniques, and ideas; analyze URLs, YouTube videos, and GitHub repos. This skill should be used when Keith says /research, "look into this", "what's new in AI", or wants to deep-dive into a topic, URL, or repo. Also triggers on /research deep for multi-source NotebookLM research. For internal system health, use /improve instead.
---

# K2Bi Research Agent

On-demand research that scans externally for new tools, techniques, and ideas, and deep dives into specific topics or URLs. Supports multi-source deep research via NotebookLM.

> **Phase 2 port status:** The `/research videos` YouTube playlist flow and several helper scripts (`yt-search.py`, `send-telegram.sh`, `parse-nblm.py`, `motivations-helper.sh`, `k2b-playlists.json`) still reference the sibling K2B repo at `~/Projects/K2B/scripts/`. That flow is not in the K2Bi Phase 1 scope. For standalone K2Bi sessions, prefer the text/URL modes (`/research "topic"`, `/research <url>`) and `/research deep <topic>` (which uses the global `notebooklm` CLI and writes to `~/Projects/K2Bi-Vault/raw/research/`). Full port of the YT flow lands when Phase 2 closes on the trading-research use case.

## Commands

- `/research` -- External scanning using research-topics.md
- `/research "topic"` -- Deep dive on a specific topic
- `/research <url>` -- Deep dive on a specific URL (YouTube, GitHub, article)
- `/research deep <topic>` -- Multi-source deep research via NotebookLM (see below)
- `/research deep <topic> --sources <url1> <url2> ...` -- Deep research with specific sources

> For internal vault health and system auditing, use `/improve vault` instead.

## Vault & Skill Paths

- Vault: `~/Projects/K2Bi-Vault`
- Skills: `~/Projects/K2Bi/.claude/skills/`
- Research topics: `~/Projects/K2Bi-Vault/wiki/context/research-topics.md`
- Output: `~/Projects/K2Bi-Vault/raw/research/`

## External Scanning

### Default Mode (no topic/URL)

1. Read `K2Bi-Vault/wiki/context/research-topics.md` for the topic list
2. For each topic category, run 1-2 targeted web searches
3. For each finding:
   - Brief summary (2-3 sentences max)
   - K2B relevance: how could this be applied to K2B specifically?
   - Actionability: is this something Keith could use now, soon, or someday?
4. Prioritize findings by relevance to K2B

### Topic Mode (`/research "topic"`)

1. Run 3-5 targeted web searches on the specific topic
2. Read and synthesize findings
3. Produce a deep-dive analysis focused on K2B applicability
4. Include specific recommendations: "K2B could implement X by doing Y"

### URL Mode (`/research <url>`)

Detect URL type and handle accordingly:

**YouTube URLs:**
1. Fetch transcript using the YouTube Transcript MCP tool
2. Analyze the full transcript
3. Extract key concepts, techniques, tools mentioned
4. Map each to K2B applicability
5. Note timestamps for the most relevant segments

**GitHub repo URLs:**
1. Fetch and read the README
2. Explore the repo structure (key files, directory layout)
3. Assess: what patterns or code could K2B reuse?
4. Note specific files or techniques worth adopting

**Article/web page URLs:**
1. Fetch the page content
2. Extract key insights and techniques
3. Analyze through the lens of "what can K2B learn from this?"

**All URL types produce:**
- Source summary
- Key takeaways (5-10 bullet points)
- K2B applicability analysis
- Specific recommendations with implementation ideas

## Deep Research Mode (`/research deep <topic>`) -- added 2026-04-12

Multi-source research powered by Google NotebookLM. Creates a dedicated notebook, loads multiple sources, runs structured queries (analysis done by Gemini at zero token cost to K2B), then synthesizes findings into the vault.

**Prerequisites**: `notebooklm-py` installed and authenticated (`notebooklm auth check --test`).

### When to use deep vs regular research

- **Regular** (`/research "topic"` or `/research <url>`): Single source or quick web scan. Fast, cheap.
- **Deep** (`/research deep <topic>`): Multi-source synthesis across 10-50 sources. When the topic requires cross-referencing multiple perspectives, comparing implementations, or building a comprehensive understanding.

### Workflow

#### Phase 1: Source Gathering

1. Search for sources in parallel:
   - **YouTube**: Run `python3 ~/Projects/K2B/scripts/yt-search.py "<topic>" --count 15 --months 6` for relevant videos. Uses YouTube Data API v3 with K2B's OAuth credentials (works on both MacBook and Mac Mini). Costs ~101 quota units per search (100 for search.list + 1 for videos.list) out of 10,000/day.
   - **Perplexity**: Use `mcp__perplexity-ask__perplexity_ask` for broader research including GitHub repos, Reddit discussions, blog posts, tweets. Ask for specific URLs and repo names.
   - **Vault**: Grep `~/Projects/K2Bi-Vault/wiki/` for existing vault notes on the topic
2. Present a numbered source list to Keith. Include title, source type, and brief reason for inclusion.
3. Keith reviews, adds/removes sources, approves.

If Keith provides `--sources <url1> <url2>`, skip the search phase and use those directly.

#### Phase 2: NotebookLM Setup

Run these commands sequentially:

```bash
notebooklm create "K2B Research: <topic>" --json
# Parse notebook_id from JSON output
notebooklm use <notebook_id>
```

Add each approved source:
```bash
# URLs, YouTube, articles
notebooklm source add "<url>"

# Vault notes (pass path directly -- notebooklm-py handles .md files natively)
notebooklm source add ~/Projects/K2Bi-Vault/wiki/path/to/note.md

# Local files (PDFs, text, markdown, Word docs)
notebooklm source add ./path/to/file.pdf
```

Wait for all sources to be indexed:
```bash
notebooklm source list --json
# All sources should show status: "ready"
```

**Source limit**: 50 per notebook (standard tier). If more than 50 sources, prioritize by relevance.

#### Phase 3: Structured Research Queries

Run 5-8 targeted questions against the notebook. NotebookLM (Gemini) does ALL the analysis -- zero Opus tokens for this phase.

```bash
notebooklm ask "<question>"
```

**Standard question categories** (adapt to topic):
1. **Landscape**: "What are the main approaches/patterns for <topic> across these sources?"
2. **Architecture**: "What architectural or implementation patterns do these sources recommend?"
3. **Comparison**: "Compare the different approaches. What are the tradeoffs?"
4. **Risks**: "What are the biggest failure modes, limitations, or mistakes people report?"
5. **Minimal viable**: "What's the simplest starting point, and what's the recommended evolution path?"
6. **Keith-specific**: "How would this apply to someone who is [Keith's context -- SJM executive, building a personal AI second brain, uses Obsidian vault]?"

Add 1-2 topic-specific questions based on Keith's original prompt.

Use `--json` if you need citation references for attribution in the vault note.

#### Phase 4: Optional Deliverables

Ask Keith: "Want an audio overview to listen to, a mind map, or an infographic?"

If yes:
```bash
# Audio overview (podcast)
notebooklm generate audio "Focus on <specific angle>" --json
# Wait for completion (can use subagent or poll)
notebooklm artifact wait <artifact_id> --timeout 1200
notebooklm download audio ~/Projects/K2Bi-Vault/Assets/audio/<date>_research_<topic>.mp3

# Mind map (instant)
notebooklm generate mind-map
notebooklm download mind-map ~/Projects/K2Bi-Vault/Assets/<date>_research_<topic>_mindmap.json

# Infographic
notebooklm generate infographic --detail detailed
notebooklm artifact wait <artifact_id> --timeout 600
notebooklm download infographic ~/Projects/K2Bi-Vault/Assets/images/<date>_research_<topic>.png
```

#### Phase 5: Synthesis

Opus reads all NotebookLM answers and writes a structured vault note. This is the ONLY phase that costs Opus tokens. Apply K2B identity framing:
- How does this apply to Keith's SJM/Signhub/TalentSignals context?
- What maps to K2B's existing architecture (raw/wiki/review, commander/worker)?
- What's actionable now vs later?

Save to `raw/research/YYYY-MM-DD_research_<topic-slug>.md` using the Deep Research Output Format below.

#### Phase 6: Compile

Trigger k2b-compile on the new raw research note:
- Updates relevant wiki pages (concepts, projects, reference)
- Creates new reference pages if needed
- Updates cross-links

### Deep Research Output Format

```markdown
---
tags: [research, deep-dive, {topic-tags}]
date: YYYY-MM-DD
type: research-briefing
origin: k2b-generate
source: "NotebookLM deep research, N sources"
notebooklm-notebook: "<notebook-id>"
up: "[[Home]]"
---

# Deep Dive: [Topic Title]

## Sources Analyzed
N sources (X YouTube, Y GitHub repos, Z articles, W vault notes)
NotebookLM notebook: [notebook-id] (persistent -- can revisit for follow-up queries)

## Key Findings
1. [finding with context]
2. ...

## Architecture/Patterns
### [Pattern/Approach A]
- What it is
- Who uses it
- Tradeoffs

### [Pattern/Approach B]
...

## K2B Applicability
### What maps directly to our architecture
- [specific mapping]

### What we'd need to build new
- [gap analysis]

### Recommended approach for Keith
- [concrete recommendation]

## Risks and Limitations
- [risk 1]
- [risk 2]

## Implementation Ideas
- [ ] [concrete next step]
- [ ] ...

## Deliverables
- Audio overview: [[Assets/audio/YYYY-MM-DD_research_topic.mp3]] (if generated)
- Mind map: [[Assets/YYYY-MM-DD_research_topic_mindmap.json]] (if generated)

## Linked Notes
[wikilinks to related vault notes]
```

### Commander/Worker Pattern for Deep Research

Deep research adds Gemini (via NotebookLM) as a third worker alongside MiniMax:

| Role | Who | What they do in deep research |
|------|-----|-------------------------------|
| Commander | Opus | Source gathering, question design, K2B framing, vault integration |
| Worker 1 | Gemini (NotebookLM) | Multi-document analysis, cross-referencing, citation-grounded answers |
| Worker 2 | MiniMax M2.7 | Bulk extraction on individual long sources (if needed, per size gate) |

Gemini handles the expensive multi-doc synthesis for free. Opus adds identity-aware judgment. MiniMax handles individual source extraction when sources exceed the 10K char size gate.

## `/research videos "<query>"` -- on-demand video discovery via NotebookLM

Retires the old YouTube recommend agent. Finds videos matching a query, filters them via NotebookLM using Keith's baked framing + the tail of `wiki/context/video-preferences.md`, adds suitable ones to the K2B Watch playlist, drops per-video review notes, sends a Telegram notification.

**Prerequisites:** `notebooklm auth check --test` passes. `K2B_BOT_TOKEN` is set in env.

### Baked Keith framing (do not edit per query)

> Senior TA leader running AI transformation in a large traditional corporate (SJM Resorts, Macau). Also operates Signhub Tech (HK), TalentSignals, Agency at Scale. Content angle: showing how senior executives in traditional corporates use AI to 10x effectiveness. Prefer content creators with clear concrete examples over academic papers. Prefer actionable over theoretical. Prefer deployable in 90 days over visionary. Skip pure hype, skip thumbnails with "SHOCKING" / "INSANE", skip anything under 3 minutes, skip Chinese-only content unless specifically requested.

### Flow

**Capture the query as the first thing the skill does.** The query is user-supplied and may contain quotes, backticks, dollar signs, or other shell metacharacters. Never interpolate `<query>` literally into a bash block -- always go through the bound variables defined here.

```bash
QUERY="$1"                           # raw user input, never echoed unescaped into shell
QUERY_SAFE=$(printf '%q' "$QUERY")   # safe for inline use inside double-quoted strings
QUERY_SLUG=$(printf '%s' "$QUERY" | tr '[:upper:]' '[:lower:]' | sed 's/[^a-z0-9]\{1,\}/-/g; s/^-//; s/-$//' | cut -c1-60)
```

1. **Get candidate YouTube URLs** via `yt-search.py` (not NotebookLM -- NotebookLM is the filter, not the discovery engine). Use a per-run scratch file via `mktemp` so concurrent scheduled runs cannot stomp each other:
   ```bash
   CANDIDATES=$(mktemp -t k2b-candidates.XXXXXX.json)
   trap 'rm -f "$CANDIDATES"' EXIT
   python3 ~/Projects/K2B/scripts/yt-search.py "$QUERY" --count 25 --months 1 --json > "$CANDIDATES"
   ```
   Parse the JSON (shape: `{"query", "count", "results": [{"url", "title", "channel", "duration", "published", ...}]}`).

   **Zero-candidate + unparseable-output guard (mandatory before Step 2).** The old `[[ "$COUNT" == "0" ]]` check silently passed on any non-numeric / empty `$COUNT`, so a broken `$CANDIDATES` (disk full, truncated write, upstream yt-search failure writing garbage to stdout) would bypass the guard and hit `notebooklm create` anyway. Fail loud on parse errors, separately from the legitimate zero-result case:
   ```bash
   COUNT=$(jq '.count' "$CANDIDATES" 2>/dev/null)
   if [[ -z "$COUNT" || ! "$COUNT" =~ ^[0-9]+$ ]]; then
     echo "FATAL: yt-search produced unparseable output in $CANDIDATES" >&2
     ~/Projects/K2B/scripts/send-telegram.sh "K2B /research videos: yt-search output unparseable for: $QUERY -- aborting, see run log" || true
     exit 1
   fi
   CANDIDATES_SCREENED="$COUNT"   # consumed by Step 10 zero-picks Telegram message
   if (( COUNT == 0 )); then
     ~/Projects/K2B/scripts/send-telegram.sh "K2B /research videos: no recent videos found for: $QUERY"
     exit 0
   fi
   ```

   **Why these args:** `--count 25` gives higher recall before filtering (Gemini typically rejects 60-80%, so 25 candidates yields ~5-10 suitable, matching the 3-10-per-run target). `--months 1` keeps the semantics at "what's new and worth knowing" -- six months of backlog dilutes recency. If a topic is thin in the last month, the run returns fewer videos and that's fine; silence beats stale.

2. **Create fresh notebook and add each candidate as a YouTube source.** Build the notebook title via the safe-quoted variable; iterate URLs via a `while read` loop (NOT `for URL in $(jq ...)`) so URLs containing whitespace or shell metacharacters cannot break the loop:
   ```bash
   NB_ID=$(notebooklm create "Videos: $QUERY" --json | jq -r '.notebook.id')
   SOURCE_IDS=()
   while IFS= read -r URL; do
     [[ -z "$URL" ]] && continue
     SRC_ID=$(notebooklm source add "$URL" -n "$NB_ID" --json 2>/dev/null | jq -r '.source.id // .id // empty')
     if [[ -n "$SRC_ID" ]]; then
       SOURCE_IDS+=("$SRC_ID")
     fi
   done < <(jq -r '.results[].url' "$CANDIDATES")
   ```
   `notebooklm source add` auto-detects YouTube URLs and pulls the transcript as the indexed content. `source add` itself returns quickly; processing happens in the background and is polled via `source wait`.

3. **Wait for all sources to index in parallel, with per-source retry, then canonical-reconcile against `notebooklm source list --json` before enforcing the >=5 ready threshold.** A bare `wait` only checks the last backgrounded job, so we collect each child PID and `wait $PID` individually. A single-shot `source wait` loses transient failures to silent timeouts (the first-run incident on 2026-04-15 dropped 8 of 25 sources with no retry and no visibility into whether those were real failures or indexer stalls), so each child gets one retry with a 2s backoff before it concedes. And the ephemeral `$READY_LOG` can diverge from NotebookLM's actual state because it reflects the per-child `source wait` return code, not notebook truth -- the same run observed NBLM describe 24 sources while `$READY_LOG` said only 17 were ready. After the parallel loop returns, we re-query the notebook once more via `source list --json` and prefer that canonical count for the threshold check, falling back to `$READY_LOG` only if the canonical query fails.
   ```bash
   declare -a WAIT_PIDS=()
   declare -a WAIT_IDS=()
   READY_LOG=$(mktemp -t k2b-ready.XXXXXX.log)
   CANONICAL_JSON=$(mktemp -t k2b-canonical.XXXXXX.json)
   trap 'rm -f "$CANDIDATES" "$READY_LOG" "$CANONICAL_JSON"' EXIT

   for ID in "${SOURCE_IDS[@]}"; do
     (
       # One retry with 2s backoff. `source wait` may spuriously time out on
       # transient indexer stalls -- a second attempt after a short pause
       # usually resolves it. Total worst-case per source: 600 + 2 + 600 = 1202s.
       for ATTEMPT in 1 2; do
         if notebooklm source wait "$ID" -n "$NB_ID" --timeout 600 --json 2>/dev/null \
              | jq -e '.status == "ready"' >/dev/null 2>&1; then
           echo "ready $ID attempt=$ATTEMPT" >> "$READY_LOG"
           exit 0
         fi
         (( ATTEMPT == 1 )) && sleep 2
       done
       echo "fail  $ID attempts=2" >> "$READY_LOG"
     ) &
     WAIT_PIDS+=("$!")
     WAIT_IDS+=("$ID")
   done

   for PID in "${WAIT_PIDS[@]}"; do wait "$PID" || true; done

   # Canonical reconcile: ask NotebookLM for the current source list once more
   # after all child waits have returned. Prefer this over $READY_LOG because
   # (a) parallel children race on shared-file appends (<PIPE_BUF so atomic, but
   # the counts can still diverge from truth), and (b) a `source wait` that
   # returned non-ready may have quietly finished indexing just after its timeout.
   notebooklm source list -n "$NB_ID" --json > "$CANONICAL_JSON" 2>/dev/null || true
   READY_COUNT=""
   TOTAL_COUNT=""
   if [[ -s "$CANONICAL_JSON" ]] && jq empty "$CANONICAL_JSON" 2>/dev/null; then
     READY_COUNT=$(jq '
       if type == "array" then [.[] | select((.status // "") == "ready")] | length
       elif type == "object" and has("sources") then [.sources[] | select((.status // "") == "ready")] | length
       else empty end' "$CANONICAL_JSON" 2>/dev/null)
     TOTAL_COUNT=$(jq '
       if type == "array" then length
       elif type == "object" and has("sources") then .sources | length
       else empty end' "$CANONICAL_JSON" 2>/dev/null)
   fi
   if [[ -z "$READY_COUNT" || -z "$TOTAL_COUNT" ]]; then
     READY_COUNT=$(grep -c '^ready ' "$READY_LOG" 2>/dev/null || true)
     TOTAL_COUNT=${#SOURCE_IDS[@]}
     RECONCILE_SOURCE="ephemeral \$READY_LOG (canonical reconcile failed)"
   else
     RECONCILE_SOURCE="canonical source list"
   fi
   FAIL_COUNT=$(grep -c '^fail '  "$READY_LOG" 2>/dev/null || true)

   if (( READY_COUNT < 5 )); then
     ~/Projects/K2B/scripts/send-telegram.sh \
       "K2B /research videos: only $READY_COUNT of $TOTAL_COUNT videos indexed for: $QUERY (reconcile=$RECONCILE_SOURCE) -- aborting, see run record"
     # Still write a partial run record + delete the notebook before exiting
     notebooklm delete -n "$NB_ID" -y >/dev/null 2>&1 || true
     exit 1
   fi
   ```

   Per-source failures below the threshold (private video, transcription unavailable, rate-limited) are tolerated -- only failures that drop the canonical ready count below 5 abort the run. Log BOTH the per-source results from `$READY_LOG` (with `attempt=` / `attempts=` suffixes so the retry pattern is auditable) AND the `$RECONCILE_SOURCE` marker into the run record in Step 9.

4. **Read the preference tail** into a variable:
   ```bash
   PREF_TAIL=$(tail -n 30 ~/Projects/K2Bi-Vault/wiki/context/video-preferences.md | sed 's/"/\\"/g')
   ```

5. **Ask NotebookLM for content descriptions only -- NotebookLM is the reader, K2B is the judge.** The baked Keith framing and the preference tail are NOT passed to Gemini; K2B holds them for Step 6. Gemini must not rank, filter, or judge suitability, even when viewer context is provided (see below).

   Load Keith's active motivations (gated by the `K2B_MOTIVATIONS_ENABLED` rollback toggle, default `true`). When empty, the prompt reverts byte-for-byte to its pre-feature form:
   ```bash
   MOTIVATIONS=""
   if [[ "${K2B_MOTIVATIONS_ENABLED:-true}" == "true" ]]; then
     MOTIVATIONS=$(~/Projects/K2B/scripts/motivations-helper.sh read 2>/dev/null || true)
   fi

   if [ -n "$MOTIVATIONS" ]; then
     MOT_BLOCK=$(cat <<EOF

   Viewer context (use for EXTRACTION GUIDANCE only, not ranking):
   ${MOTIVATIONS}

   For every video, maintain the baseline description quality defined above
   (2-3 sentences in what_it_covers, all schema fields populated).

   Additionally, when a video's content touches any viewer context area,
   add these fields to that video's entry:
   - "motivation_overlap": ["short phrase from context that connects"]
   - "motivation_detail": "2-3 sentences describing specifically how the video
     addresses the matching motivation (which timestamps, which examples, which claims)"

   Do NOT remove detail from videos that don't match viewer context. Do NOT use
   this context to judge, rank, filter, or downgrade any video. Extraction is
   equal for all videos; overlap videos get ADDITIONAL fields, never reduced
   ones.
   EOF
   )
   else
     MOT_BLOCK=""
   fi
   ```

   Then compose and send the prompt:
   ```bash
   PROMPT=$(cat <<EOF
   Each source in this notebook is a YouTube video. For each one, return an objective content description.

   Return JSON: [
     {
       "url",
       "title",
       "channel",
       "duration",
       "what_it_covers": "2-3 sentences describing the topic and main claims",
       "style": "tutorial | talk | demo | news | listicle | interview",
       "level": "beginner | intermediate | advanced",
       "concrete_examples": true,
       "key_speakers_or_companies": ["..."]
     }
   ]
   ${MOT_BLOCK}

   Do NOT judge whether a video is good, suitable, relevant, or recommended. Do NOT rank or sort. Describe what the video contains and stop. Return ONLY the JSON array, no prose before or after.
   EOF
   )

   notebooklm ask "$PROMPT" -n "$NB_ID"
   ```

   `motivation_overlap` / `motivation_detail` are additive; they appear only on videos whose content touches the viewer context. Non-overlap videos retain the full baseline schema. `scripts/parse-nblm.py` preserves unknown fields through its `dict(nblm_entry)` copy (line 165), so these fields survive parse into Step 6.

   Capture the raw answer into a variable `NBLM_RAW`.

6. **K2B-as-judge -- Claude reasoning inline (NOT a bash invocation).** This step is the Claude session running the skill applying judgment to the NBLM descriptions and writing `$SUITABLE_JSON`. Execute the following substeps explicitly:

   a. **Parse the NBLM answer defensively** via the committed helper `scripts/parse-nblm.py`. The helper encapsulates three defensive passes that a prior incident (2026-04-15) hit in a single run, so this step no longer describes inline Python: (1) citation-marker stripping with a regex that handles comma lists AND dash ranges (`[44, 47]`, `[1-4]`, mixed `[13-16, 17-20]`; the old inline regex was comma-only and silently let dash markers survive into `json.loads`), (2) literal-newline normalization inside JSON string literals via a character walker that tracks `in_string` state (NBLM sometimes wraps long `what_it_covers` values with raw `\n` bytes which `json.loads` rejects as invalid control characters), and (3) rejoin-by-title against `$CANDIDATES` so every entry carries authoritative `real_url`, `real_title`, `real_channel`, `real_duration`, `real_published`, and `video_id` from yt-search instead of NBLM's synthetic `v=<name>` placeholders.

   Run the helper via:
   ```bash
   NBLM_RAW_FILE=$(mktemp -t k2b-nblm-raw.XXXXXX.txt)
   PARSED_JSON=$(mktemp -t k2b-nblm-parsed.XXXXXX.json)
   NBLM_PARSE_ERR=$(mktemp -t k2b-nblm-err.XXXXXX.log)
   trap 'rm -f "$CANDIDATES" "$READY_LOG" "$CANONICAL_JSON" "$NBLM_RAW_FILE" "$PARSED_JSON" "$NBLM_PARSE_ERR"' EXIT
   printf '%s' "$NBLM_RAW" > "$NBLM_RAW_FILE"

   if ! python3 ~/Projects/K2B/scripts/parse-nblm.py "$NBLM_RAW_FILE" "$CANDIDATES" > "$PARSED_JSON" 2>"$NBLM_PARSE_ERR"; then
     # Parse failed after all defensive passes in parse-nblm.py. Write the raw
     # NBLM answer + the parse-nblm.py stderr to raw/research/ as the durable
     # audit trail BEFORE aborting, then send Telegram, delete the notebook,
     # exit 1. The audit trail is most valuable on failure, so it is written
     # first and always.
     #
     # Not implemented here (deliberate): an NBLM ask retry with a stricter
     # prompt. parse-nblm.py already encapsulates every defensive pass we
     # know about, so a failure here almost always means NBLM returned
     # genuinely malformed output (safety refusal, API error, empty body)
     # that a retry would not fix. If you see repeat failures in production,
     # harden parse-nblm.py rather than adding a retry loop around it.
     echo "FATAL: parse-nblm.py could not normalize NBLM output for: $QUERY" >&2
     cat "$NBLM_PARSE_ERR" >&2

     QUERY="$QUERY" QUERY_SLUG="$QUERY_SLUG" NBLM_RAW="$NBLM_RAW" \
       NBLM_PARSE_ERR_FILE="$NBLM_PARSE_ERR" python3 - <<'PYEOF'
import os, datetime
vault = os.path.expanduser("~/Projects/K2Bi-Vault")
slug  = os.environ.get("QUERY_SLUG") or "unknown"
query = os.environ.get("QUERY") or slug
query_yaml = query.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")
today = datetime.date.today().isoformat()
path  = os.path.join(vault, "raw", "research", f"{today}_videos_{slug}.md")
raw   = os.environ.get("NBLM_RAW") or "(NBLM_RAW not captured)"
err_path = os.environ.get("NBLM_PARSE_ERR_FILE") or ""
try:
    err = open(err_path).read() if err_path else ""
except OSError:
    err = ""
with open(path, "w") as f:
    f.write(
        f'---\ntype: research-run\nstatus: nblm-parse-failed\nquery: "{query_yaml}"\n---\n\n'
        f'# NBLM parse failed\n\n## parse-nblm.py stderr\n\n```\n{err}\n```\n\n'
        f'## Raw NBLM answer\n\n```\n{raw}\n```\n'
    )
PYEOF

     ~/Projects/K2B/scripts/send-telegram.sh "K2B /research videos: NBLM parse failed for: $QUERY -- see run record" || true
     notebooklm delete -n "$NB_ID" -y >/dev/null 2>&1 || true
     exit 1
   fi
   ```

   The helper outputs a JSON array where each entry carries both the NBLM content fields (`what_it_covers`, `style`, `level`, `concrete_examples`, `key_speakers_or_companies`) AND the candidate-sourced identity fields. Entries where title-rejoin failed appear with `identity_resolved: false`, `match_method: "failed"`, `video_id: ""`, and `real_published: "unknown"` -- **these are NOT dropped silently**; Step 6d must sort them into `rejects[]` with `reason: "identity resolution failed"`. `real_published` is pre-normalized by the helper to `YYYY-MM-DD` or the literal `"unknown"`, and the jq schema gate in Step 6g accepts both forms. The recency veto in Step 6d simply skips candidates with `"unknown"` (neither fresh nor stale).

   b. **Load Keith's framing from the skill header, explicitly.** Read the block at the "### Baked Keith framing (do not edit per query)" callout above (lines 262-264 of this SKILL.md). The Claude session reads that block directly from the skill file -- single source of truth, explicit load, no implicit "scroll up and remember". Do NOT re-inline the framing text into the Step 5 NBLM prompt; Gemini does not get taste context.

   c. **Load the preference tail AND reload active motivations** (point-in-time snapshots, both re-read fresh here because Step 6 runs in Claude reasoning, not in the Step 5 shell):
   ```bash
   PREF_TAIL=$(tail -n 30 ~/Projects/K2Bi-Vault/wiki/context/video-preferences.md)
   MOTIVATIONS=""
   if [[ "${K2B_MOTIVATIONS_ENABLED:-true}" == "true" ]]; then
     MOTIVATIONS=$(~/Projects/K2B/scripts/motivations-helper.sh read 2>/dev/null || true)
   fi
   ```

   `$MOTIVATIONS` here must be reloaded even though Step 5 also loaded it -- Step 5's bash scope does not survive into the Claude-side judgment. When non-empty, it represents Keith's current active projects (Building section) and self-added learning questions, available to K2B-the-judge for the match-bonus rule in (d). When empty (rollback toggle off or helper returns nothing), the judgment rubric below runs identically to the pre-feature flow. Step 5 and Step 6 must use the SAME motivations text for a given run; do not re-run the helper between them in a way that could produce different output (the helper is deterministic given unchanged vault state, so back-to-back reads are safe).

   d. **Apply the judgment rubric** to each parsed NBLM entry:
   - A candidate is a **pick** only if K2B can name a *specific* reason Keith will finish it through one of these lenses: concrete-examples, 90-day-deployable, senior-TA-in-traditional-corporate.
   - Any line in the preference tail that contradicts a candidate (disliked channel, disliked style, level mismatch) is a **veto** -- it goes into `rejects`.
   - **Recency veto.** If the topic moves fast (new model capabilities, tool releases, market news, "what's new in X") AND `real_published` is more than 6 months older than the run date (i.e. `today - real_published > 180 days`), the candidate goes into `rejects` with `reason: "outdated: published <date>, topic moves faster than that"`. Evergreen topics (mindset, frameworks, interview patterns, management principles) are NOT subject to this veto. K2B decides per candidate whether the topic is fast-moving. The run date anchor is always "today" at run time, never hardcoded, so the veto window drifts with the calendar.
   - **Motivation match bonus.** When a candidate's `motivation_overlap` (populated by NBLM in Step 5) intersects a Building item or Active Question in `$MOTIVATIONS`, treat that as ADDITIONAL evidence toward pick. This bonus DOES NOT bypass the quality bar -- candidates must still satisfy the concrete-examples / 90-day-deployable / senior-TA-in-traditional-corporate lenses to be picked. Motivation alignment alone is not enough.
   - **`why_k2b` enrichment.** When a pick has a non-empty `motivation_overlap` array, `why_k2b` MUST include a motivation-aware sentence of the form: "Connects to [Building item or Active Question]: [how, referencing motivation_detail when present]". When `motivation_overlap` is empty or absent, `why_k2b` stays as-is (no mention of motivations; do not fabricate a connection).
   - Fewer than 5 clear winners → pick fewer. **Pad-to-target is forbidden.**
   - **Zero picks is allowed and explicit.** If nothing clears the bar, emit `picks: []`.
   - **Cap is 5.** Even if 10 candidates look good, pick the top 5 by confidence.
   - For each pick, K2B chooses a `suggested_category` from the playlist map (see below). If nothing fits, K2B may suggest a new category name -- that pick is flagged for Keith's approval in the run note and `/review` leaves it `playlist_action: pending` with `new category suggested: <name>` appended to `notes:`.

   e. **Write `$SUITABLE_JSON` with this strict shape.** Every field is mandatory.
   ```json
   {
     "picks": [
       {
         "pick_id": "2026-04-14-<query-slug>-01",
         "video_id": "...",
         "real_url": "...",
         "real_title": "...",
         "real_channel": "...",
         "real_duration": "...",
         "real_published": "2026-03-20",
         "why_k2b": "2-3 sentences in K2B's voice: what the video covers, why it matches Keith's lens, which preference-tail lines it respects or avoids. Mention age only when relevant (e.g. 'from 3 weeks ago, so tool versions still current')",
         "suggested_category": "K2B Claude",
         "confidence": 0.85,
         "preference_evidence": ["2026-04-13 liked: Matt Wolfe -- clear concrete examples"]
       }
     ],
     "rejects": [
       {
         "real_title": "...",
         "real_channel": "...",
         "reason": "one sentence in K2B's voice"
       }
     ]
   }
   ```

   **Hard rules K2B MUST follow when building this JSON:**
   - `picks` sorted by `confidence` descending.
   - Every pick has `confidence` in `[0.0, 1.0]`. If K2B cannot confidently rate a candidate, it belongs in `rejects`, not `picks`.
   - Every pick has `preference_evidence: []` -- a list of zero or more verbatim lines from `$PREF_TAIL` that justified the decision. Empty list is allowed; the field must exist.
   - `rejects` MUST include every non-pick candidate parsed from the NBLM answer. Silent drops are forbidden. `len(picks) + len(rejects)` must equal the count of parsed NBLM entries.
   - `pick_id` format: `YYYY-MM-DD-<query-slug>-NN` (1-indexed, zero-padded NN, stable across the run).
   - `suggested_category` must be one of the names in `scripts/k2b-playlists.json` OR a new name clearly flagged by K2B for Keith's approval.

   f. **Allocate the tmpfile, extend the trap, and write the JSON.** The Claude session running this skill executes these steps explicitly via its Bash and Write tools (the session IS the judge, so the "write" is not a subagent hand-off -- it is Claude directly emitting the validated object to the file):

   ```bash
   SUITABLE_JSON=$(mktemp -t k2b-suitable.XXXXXX.json)
   trap 'rm -f "$CANDIDATES" "$READY_LOG" "$CANONICAL_JSON" "$NBLM_RAW_FILE" "$PARSED_JSON" "$NBLM_PARSE_ERR" "$SUITABLE_JSON"' EXIT
   echo "$SUITABLE_JSON"   # print the path so Claude can reference it explicitly
   ```

   Then Claude writes the `{picks, rejects}` JSON object to that exact path via the Write tool (preferred for JSON correctness) OR via an atomic heredoc:
   ```bash
   cat > "$SUITABLE_JSON" <<'JSON_EOF'
   { "picks": [...], "rejects": [...] }
   JSON_EOF
   ```
   Do NOT use `jq -n --argjson` with shell-interpolated pick data -- the JSON is too structured and too long, and shell metacharacters in `why_k2b` / `preference_evidence` will corrupt it. The Write tool or a quoted heredoc is the only safe path.

   After the write, verify the file is non-empty and parseable as JSON before proceeding:
   ```bash
   [[ -s "$SUITABLE_JSON" ]] || { echo "FATAL: $SUITABLE_JSON is empty" >&2; exit 1; }
   jq empty "$SUITABLE_JSON" || { echo "FATAL: $SUITABLE_JSON is not valid JSON" >&2; exit 1; }
   ```

   g. **Schema validation gate (mandatory before Step 7).** Validates BOTH `picks[]` and `rejects[]` object shapes. Identity fields are checked with `type == "string"` AND `length > 0` so an empty-string `video_id` cannot slip past a bare `has(...)` check and hit `yt-playlist-add.sh` with a blank ID. On failure, log the raw NBLM answer to the run record, Telegram abort, delete notebook, exit 1.
   ```bash
   jq -e '
     (type == "object") and
     (has("picks") and (.picks | type == "array")) and
     (has("rejects") and (.rejects | type == "array")) and
     (.picks | all(
       (has("pick_id")) and ((.pick_id | type) == "string") and ((.pick_id | length) > 0) and
       (has("video_id")) and ((.video_id | type) == "string") and ((.video_id | length) > 0) and
       (has("real_url")) and ((.real_url | type) == "string") and ((.real_url | length) > 0) and
       (has("real_title")) and ((.real_title | type) == "string") and ((.real_title | length) > 0) and
       (has("real_channel")) and ((.real_channel | type) == "string") and ((.real_channel | length) > 0) and
       (has("real_duration")) and ((.real_duration | type) == "string") and
       (has("real_published")) and ((.real_published | type) == "string") and
       ((.real_published | test("^[0-9]{4}-[0-9]{2}-[0-9]{2}")) or (.real_published == "unknown")) and
       (has("why_k2b")) and ((.why_k2b | type) == "string") and ((.why_k2b | length) > 0) and
       (has("suggested_category")) and ((.suggested_category | type) == "string") and ((.suggested_category | length) > 0) and
       (has("confidence")) and ((.confidence | type) == "number") and (.confidence >= 0) and (.confidence <= 1) and
       (has("preference_evidence")) and ((.preference_evidence | type) == "array")
     )) and
     (.rejects | all(
       (has("real_title")) and ((.real_title | type) == "string") and ((.real_title | length) > 0) and
       (has("real_channel")) and ((.real_channel | type) == "string") and ((.real_channel | length) > 0) and
       (has("reason")) and ((.reason | type) == "string") and ((.reason | length) > 0)
     ))
   ' "$SUITABLE_JSON" >/dev/null || {
     # Write raw NBLM answer to partial run record FIRST -- the audit trail is most valuable on failure.
     # Env vars are passed explicitly on the python3 line because QUERY / QUERY_SLUG / NBLM_RAW are set
     # earlier in the skill without `export`, so a child process won't inherit them otherwise.
     QUERY="$QUERY" QUERY_SLUG="$QUERY_SLUG" NBLM_RAW="$NBLM_RAW" python3 - <<'PYEOF'
import os, datetime
vault = os.path.expanduser("~/Projects/K2Bi-Vault")
slug  = os.environ.get("QUERY_SLUG") or "unknown"
query = os.environ.get("QUERY") or slug   # real query for the YAML frontmatter; fall back to slug if unset
query_yaml = query.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")
today = datetime.date.today().isoformat()
path  = os.path.join(vault, "raw", "research", f"{today}_videos_{slug}.md")
raw   = os.environ.get("NBLM_RAW") or "(NBLM_RAW not captured)"
with open(path, "w") as f:
    f.write(f'---\ntype: research-run\nstatus: schema-validation-failed\nquery: "{query_yaml}"\n---\n\n# Schema validation failed\n\n## Raw NBLM answer\n\n```\n{raw}\n```\n')
PYEOF
     ~/Projects/K2B/scripts/send-telegram.sh "K2B /research videos: schema validation failed for: $QUERY -- see run record"
     notebooklm delete -n "$NB_ID" -y >/dev/null 2>&1 || true
     exit 1
   }
   ```

7. **For each `.picks[]` entry, add to K2B Watch.** Playlist ID comes from the canonical map, never hardcoded:
   ```bash
   K2B_WATCH_ID=$(jq -r '."K2B Watch"' ~/Projects/K2B/scripts/k2b-playlists.json)
   jq -c '.picks[]' "$SUITABLE_JSON" | while IFS= read -r PICK; do
     VIDEO_ID=$(printf '%s' "$PICK" | jq -r '.video_id')
     ~/Projects/K2B/scripts/yt-playlist-add.sh "$K2B_WATCH_ID" "$VIDEO_ID" \
       || echo "WARN: playlist add failed for $VIDEO_ID" >&2
   done
   ```

8. **Write one run-level review note.** Replaces the old per-video template. Path:
   ```
   K2Bi-Vault/review/videos_$(date +%F)_${QUERY_SLUG}.md
   ```
   Use an atomic write-then-rename so `/review` and Telegram feedback paths can never observe a half-written file:
   ```bash
   RUN_NOTE="$K2B_VAULT/review/videos_$(date +%F)_${QUERY_SLUG}.md"
   RUN_NOTE_TMP="$K2B_VAULT/review/.videos_$(date +%F)_${QUERY_SLUG}.md.tmp.$$"
   # ... build the rendered content into $RUN_NOTE_TMP via cat/python/jq ...
   mv "$RUN_NOTE_TMP" "$RUN_NOTE"
   ```

   **Template** (rendered from `.picks[]` and `.rejects[]`):

   ````markdown
   ---
   type: video-run
   review-action: pending
   review-notes: ""
   query: "<QUERY>"
   run-date: <YYYY-MM-DD>
   picks-count: <N>
   watch-playlist: K2B Watch
   up: "[[index]]"
   ---

   # Videos for: <QUERY>

   **Run:** <YYYY-MM-DD> · **Candidates screened:** <CANDIDATES_SCREENED> · **K2B picked:** <N>

   ## K2B's picks

   ### 1. <real_title>

   <why_k2b prose — this is what Keith reads>

   - **url:** <real_url>
   - **channel:** <real_channel>  ·  **duration:** <real_duration>  ·  **published:** <real_published>

   ```yaml
   pick_id: 2026-04-14-<query-slug>-01
   video_id: <video_id>
   real_url: <real_url>
   real_title: "<real_title>"
   real_channel: "<real_channel>"
   real_published: "<real_published>"
   suggested_category: K2B Claude
   category_override: ""
   decision: pending          # keith: keep | drop | neutral
   playlist_action: pending   # pending | done | failed
   preference_logged: false
   processed_at: null
   notes: ""
   ```

   The `real_url`, `real_title`, `real_channel`, and `real_published` fields are duplicated from `$SUITABLE_JSON` into the YAML block so `/review` and the Telegram feedback path can match picks and distill preference lines without parsing the prose above the fence. This keeps the "YAML block is the sole parse surface" contract clean. `real_published` is sourced from yt-search, never NBLM.

   ### 2. <real_title>

   ... same block ...

   ## Candidates K2B rejected (<N_rejected>)

   - <real_title> · <real_channel> — <reason>
   - ...
   ````

   **Parsing contract.** The `/review` handler and the Telegram feedback path parse ONLY the fenced ` ```yaml ... ``` ` block after each `### N. ` heading (via PyYAML). The prose above the YAML fence is for Keith to read, never parsed. Keith edits only `decision:`, `category_override:`, and `notes:` -- everything else is K2B-managed state.

   **N=0 still writes the note.** The note exists with an empty picks section and the full rejects list. Audit trail is mandatory even when nothing cleared the bar.

9. **Write run record** at `K2Bi-Vault/raw/research/$(date +%F)_videos_${QUERY_SLUG}.md`. Sections:
   - Frontmatter: `type: research-run`, `query`, `candidates_screened: <count from $CANDIDATES, never hardcoded>`, `picks-count`, `outcome: zero-picks | has-picks`.
   - Query + baked framing version in use.
   - Preference tail snapshot that K2B saw (the literal `$PREF_TAIL` value).
   - **Picks-mode runs (`outcome: has-picks`):** the NBLM `what_it_covers` descriptions for picks only, plus K2B's `why_k2b` reasoning per pick, plus the rejects list (title · channel · one-line reason).
   - **Zero-picks runs (`outcome: zero-picks`):** include ALL NBLM descriptions (all 25, or however many were parsed) so the "why nothing was worth watching" evidence trail is durable. Zero-picks runs are exactly the ones Keith will want to audit later.
   - Ready/fail log from Step 3.
   - Per-video playlist-add results from Step 7.
   - Any Telegram failures from Step 10.

   This is the durable audit trail. It persists after `/review` moves the run note out of `review/`.

10. **Send Telegram notification.** Count picks from `.picks[]` (NOT `READY_COUNT`). Zero picks gets a dedicated message pointing Keith at the run note.
    ```bash
    PICKS_COUNT=$(jq '.picks | length' "$SUITABLE_JSON")

    if (( PICKS_COUNT == 0 )); then
      MSG="K2B: nothing worth watching this week for: $QUERY"$'\n\n'
      MSG+="Candidates screened: $CANDIDATES_SCREENED. K2B picked 0. See the run note for per-candidate reasons."
    else
      MSG="K2B picked $PICKS_COUNT videos for: $QUERY"$'\n\n'
      MSG+=$(jq -r '.picks[] | "- \(.real_title)\n  why: \(.why_k2b)\n  category: \(.suggested_category)\n  \(.real_url)\n"' "$SUITABLE_JSON")
    fi

    if ! ~/Projects/K2B/scripts/send-telegram.sh "$MSG"; then
      echo "WARN: telegram notification failed for query: $QUERY" >&2
      # Continue: notebook delete + run record still need to happen.
    fi
    ```
    `send-telegram.sh` handles the 4096-byte chunking internally; callers do NOT pre-batch. Exit-code check is mandatory.

11. **Delete the notebook** (fresh per run, no accumulation):
    ```bash
    notebooklm delete -n "$NB_ID" -y >/dev/null 2>&1 || true
    ```

12. **Append to skill-usage-log** as usual.

### Scheduling (not a skill concern)

Wrap with `/schedule`:
```
/schedule add "research-videos-ai-recruiting" weekly "/research videos \"AI recruiting tools for large enterprises\""
```
The scheduler runs the command on the Mac Mini weekly. Output lands in the vault via Syncthing. Telegram notification fires from the Mac Mini using `send-telegram.sh`.

### Playlist ID resolution

All playlist IDs come from `~/Projects/K2B/scripts/k2b-playlists.json` -- the canonical name→ID map. Lookup via `jq -r '."<name>"' ~/Projects/K2B/scripts/k2b-playlists.json`. Never hardcode an ID in this skill, `k2b-review/SKILL.md`, or CLAUDE.md. If K2B suggests a category name not present in the JSON (K2B-invented category), the run note still writes the pick but `/review` leaves `playlist_action: pending` with a "new category suggested" note until Keith approves creating the playlist.

### Failure modes

- **NotebookLM ask times out or errors:** abort, log to run record, notify Keith "research timed out on: <query>", delete the notebook.
- **NBLM JSON parse fails twice:** log raw answer to run record, notify Keith "NBLM descriptions weren't parseable, see raw/research/", abort downstream steps.
- **K2B schema validation fails on `$SUITABLE_JSON`:** Write partial run record with raw NBLM answer to `raw/research/` first, then Telegram abort, delete notebook, exit 1. This is the Step 6g gate.
- **Playlist add fails for a pick:** log per-video in run record, continue with the others, include the failure count in the Telegram summary.
- **Zero picks:** still write the run note + run record + Telegram message ("nothing worth watching this week"), still delete the notebook. N=0 is normal, not an error.

### What NOT to do

- Do NOT cache NotebookLM notebooks across runs. Fresh per run.
- Do NOT dedupe across runs via a URL log. The preference tail handles this naturally once Keith rates videos.
- Do NOT ask Keith to confirm each add. K2B already decided in Step 6. Keith's feedback comes after watching via Telegram reaction or `/review`.
- Do NOT write to `wiki/context/video-preferences.md` from `/research videos`. Only `/review` and the Telegram feedback path append there, and both do so via atomic write-then-rename.
- Do NOT re-inline the baked Keith framing into the Step 5 NBLM prompt. Gemini must not see Keith's taste. Only K2B (Step 6) sees framing + preference tail.
- Do NOT hardcode playlist IDs. Always `jq`-lookup from `scripts/k2b-playlists.json`.
- Do NOT pad picks to 5 when fewer clearly deserve it. Zero is a valid outcome.

## MiniMax extraction offload (added 2026-04-10)

**Why**: Bulk extraction (TLDR, key claims, entities) is pattern-matching work that burns Opus tokens on long sources (YouTube transcripts, papers, READMEs). Offload the extraction to MiniMax M2.7 and keep Opus focused on K2B applicability analysis, which requires identity-aware judgment. See `wiki/projects/project_minimax-offload.md` for the full rationale, provenance contract, and phase-gate protocol.

**Contract**:
- MiniMax produces a compressed, citation-backed digest: `{tldr, source_type, key_claims[], entities[], methodology_notes[], open_questions[]}`.
- Every `key_claim` carries a verbatim `source_span`, a `confidence` rating, and an `ambiguity` note.
- Opus reads the digest (not the raw source) and adds the K2B applicability section before writing the `raw/research/` note.
- Fail-open: if MiniMax is unavailable or returns invalid JSON, fall back to Opus-direct extraction on the raw source with a visible warning. Research notes are not durable commitment memory, so fail-open is safe.

**When to use (size gate)**:
- URL deep-dive mode (`/research <url>`) when the fetched source exceeds **~10,000 chars**.
- Long YouTube transcripts, full papers, READMEs for large repos, long-form articles.
- SKIP for short topic-scan findings, landing pages, or anything under 10K chars. On short sources, MiniMax's structured digest is typically LARGER than the original, so there are no token savings (this was measured empirically on 2026-04-10 against 3-9KB K2B research notes where the digest ran 1.0x-2.8x the input size). In that range, Opus-direct is cheaper AND faster.
- Rule of thumb: if you would only read the source once to extract, Opus-direct wins. If you would read it multiple times or the source is longer than Keith would skim in one sitting, MiniMax-extract wins.

**Workflow**:
1. Fetch the source content as usual (WebFetch, YouTube transcript MCP, Read for GitHub README, etc.).
2. If fetched content is under 10K chars, skip the offload entirely and extract on Opus. See size gate above.
3. Otherwise, write the fetched content to a temp file, e.g. `/tmp/k2b-research-input-$(date +%s).txt`, remembering the exact filename for the next step.
4. Call the extractor with the SAME filename from step 3:
   ```bash
   ~/Projects/K2B/scripts/minimax-research-extract.sh \
     "$TEMP_FILE" \
     "<source-url>" \
     "<source-title>"
   ```
5. Parse the returned JSON.
6. Spot-check 3 random `source_span` values against the fetched content. A simple substring match after collapsing whitespace is sufficient (`python3 -c 'import json,re,sys; ...'` or just visual). If any spot-check fails, fall back to Opus-direct extraction on the full source and append a manual-override entry to `wiki/context/minimax-jobs.jsonl`.
7. Write the K2B applicability section (this is Opus's job, NOT MiniMax's) using the digest as input plus Keith's framing (SJM/Signhub/TalentSignals positioning, content angle, his role).
8. Compose the final `raw/research/` note: frontmatter, Source, Key Takeaways (from digest), K2B Applicability (from Opus), Implementation Ideas.
9. Delete the temp file.
10. Trigger k2b-compile on the new note as usual.

**Fallback behavior**: if the extractor script exits non-zero (network error, invalid JSON, empty content), do NOT retry the script. Instead read the raw source content directly and extract in Opus, mentioning "MiniMax extractor unavailable, using Opus-direct path" in the session. The `minimax-jobs.jsonl` log already captured the failure via the script's own logging, so no additional action is needed.

**Observability**: every invocation appends a line to `wiki/context/minimax-jobs.jsonl` via the `log_job_invocation` helper in `scripts/minimax-common.sh`. Parse failure rate, cost, and duration are surfaced by `/improve`.

**Revert criteria** (per project_minimax-offload.md):
- If parse failure rate exceeds 5% over two weeks, revert to the Opus-direct path.
- If a sample audit of 10 outputs shows semantic drift (dropped claims, invented content, flattened voice) in 2+ cases, revert.
- If Keith manually overrides the extractor output more than twice in the first two weeks, revert.

## Output Format

Save to `raw/research/YYYY-MM-DD_research-briefing.md` (or `raw/research/YYYY-MM-DD_research-[topic-slug].md` for focused research).

After saving to raw/research/, trigger k2b-compile to digest the raw source into wiki pages. k2b-compile reads the raw research note, shows Keith a summary of wiki pages to update, and on approval updates wiki pages, indexes, and wiki/log.md.

```markdown
---
tags: [research, k2b-system]
date: YYYY-MM-DD
type: reference
origin: k2b-generate
up: "[[Home]]"
---

# Research Briefing -- YYYY-MM-DD

## External Findings
### [Finding 1 Title]
- **Source**: [URL or search]
- **Summary**: [2-3 sentences]
- **K2B Relevance**: [how this applies]
- **Action**: [what Keith could do with this]

### [Finding 2 Title]
...

## Recommendations
### New Ideas from Research
- [ ] [actionable idea with implementation sketch]

## Linked Notes
[wikilinks to related vault notes]
```

## For URL Deep Dives

Use a more focused output format:

```markdown
---
tags: [research, deep-dive, {topic-tags}]
date: YYYY-MM-DD
type: research-briefing
origin: k2b-generate
source: "[Title](URL)"
up: "[[Home]]"
---

# Deep Dive: [Topic/Source Title]

## Source
[URL and brief description of what this is]

## Key Takeaways
1. [takeaway with context]
2. ...

## K2B Applicability
### What We Can Use
- [specific technique] -- could apply to [specific K2B area]

### What's Interesting But Not Actionable Yet
- [concept] -- relevant when [condition]

### Implementation Ideas
- [ ] [concrete next step]

## Linked Notes
[wikilinks to related vault notes]
```

## Usage Logging

After completing the main task, log this skill invocation:
```bash
echo -e "$(date +%Y-%m-%d)\tk2b-research\t$(echo $RANDOM | md5sum | head -c 8)\tran research: FOCUS" >> ~/Projects/K2Bi-Vault/wiki/context/skill-usage-log.tsv
```

## Notes

- No em dashes, no AI cliches, no sycophancy
- Be specific in recommendations -- "improve the meeting processor" is useless, "add an explicit instruction for formatting action items with owner names in brackets" is actionable
- External findings should be filtered for relevance -- don't dump every search result
- When scanning YouTube videos, use the transcript MCP tools
- When scanning GitHub repos, focus on README, key source files, and patterns
- Always cross-link findings to existing vault notes where relevant
