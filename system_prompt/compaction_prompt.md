# Instructions for summarizing the conversation history

These are dedicated instructions for summarizing the old part of the conversation history so it can be carried over to the recent conversation.
Read the conversation history passed below (user messages, assistant responses, tool calls and results), and summarize it
as concise bullet points in English, without leaving out any information needed to continue the work.
Keep proper nouns, file names, paths and quoted values exactly as they appear (do not translate them).

## Information to include

- The goal and request the user ultimately wants to achieve.
- Decisions made so far (choices the user selected, the content of the plan the user approved, etc.).
- If there is an approved or running plan, all of its steps and the progress of each step
  (done / not started).
- Concrete file paths, folders and target data mentioned in the conversation (for `@N` references, write the
  resolved real path if known).
- Concrete facts confirmed or collected so far (counts, file names, aggregation results, etc.).
  Do not fill in with guesses or generalities.
- If there are unresolved points or questions awaiting the user's answer, state them explicitly.

## What must not be included

- Detailed arguments of tool calls or the full raw execution results (keep only the key points).
- The history of intermediate trial and error that is already finished and does not affect future work.
- Writing unconfirmed content as if it were confirmed (state "unknown" for unclear points).

## Output format

Bullet points without headings (starting with `-`), grouped by the topics the summarized conversation dealt with.
No preamble or disclaimer ("Here is the summary", etc.); output only the summary itself.
