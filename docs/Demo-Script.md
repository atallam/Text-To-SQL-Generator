# Demo video script (target 3:30, hard cap 4:00)

Audience: supply chain and data leaders on LinkedIn, plus hiring managers. One message: **a text-to-SQL tool you
can trust is mostly about what it refuses to do and how honestly it scores itself.**

Record at 1080p, browser zoom 125%, dark terminal. Rehearse once and record a take where the answers match
this script. Confidence values vary slightly between runs.

Setup before recording: `docker compose up -d`, UI at http://localhost:8501, source set to **Procurement**,
a terminal open in the repo with the venv active, `evals/reports/` open in a second tab.

| Time | On screen | Narration |
|---|---|---|
| 0:00-0:15 | Title card, then the UI | "Ask a database a question in plain English and you get SQL. The hard part is trusting it. This is a text-to-SQL engine built around that: guardrails, validation, and a confidence score on every answer." |
| 0:15-0:55 | Ask: **How many suppliers do we have in each region?** Point at the result table, the SQL, the plain-English description and the confidence badge | "Simple question, procurement data. It shows the SQL it ran, translates it back to English to check it matches what I asked, and gives a confidence score. This one is high." |
| 0:55-1:30 | Ask: **What is the total quantity on hand at each site?** Expand the confidence breakdown | "The score is not a vibe. It combines the back-translation check, a second independently written query that must agree, and sanity checks on the result. Each signal is visible." |
| 1:30-2:05 | Ask: **Which suppliers are usually late?** The clarification card appears; pick one reading | "'Late' has more than one meaning: past the original request date, or past the date the supplier promised. The answer changes a lot, so it asks instead of guessing. Pick one, then it runs." |
| 2:05-2:35 | Ask: **Delete all cancelled purchase orders** (refused). Then **What is the carbon footprint of each supplier?** (refused, says the data is missing) | "Write requests are refused. And when the data simply isn't there, it says so rather than inventing a column." |
| 2:35-3:05 | Terminal: `python -m evals.redteam` and show the summary line | "Behind that sits a SQL guardrail that parses every query. I threw 77 dangerous queries at it: schema dumps, stacked statements, system functions, comment tricks. None got through, and 20 harmless look-alikes still ran. The database role is also read-only, as a second layer." |
| 3:05-3:30 | Open `evals/reports/pipeline_procurement_*.md`, scroll to the headline | "Full-pipeline eval on this source: 94% exact on answerable questions, 100% correct clarifications, 100% correct refusals, zero unsafe statements executed. Small synthetic set, so treat it as a regression suite. The limits are in the README." |
| 3:30-3:40 | End card: repo URL, "Supply Chain x AI for Business Leaders" | "Code, evals and limitations are in the repo. Link in the comments." |

## Optional 20-second cut-in (if under time)

Switch the source to **WealthLens**, ask **What is my net worth?** and show it asks which definition (assets minus
liabilities, or including goals). Same engine, different database, configured by files only. That is the
multi-source point.

## Things to say honestly if asked

* Data is synthetic; I built the procurement source because I had no safe real one to use.
* Strict accuracy is 94% and 67% on the two sources; relaxed is 100% on both. Most misses are extra columns.
* Confidence catches wrong logic well (98% recall in calibration) but not answer-shape differences.

## LinkedIn post angle (draft separately in your own voice)

Hook: "Everyone demos text-to-SQL answering questions. Few show it refusing." Then three beats: it asks when a
term is ambiguous, it refuses what it can't know, it scores itself. Close with the numbers and the limitation.
Attach the 30-second clip of the clarification card, not the full video, as the native post.
