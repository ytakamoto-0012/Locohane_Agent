The token limit of this conversation is approaching, so **no more work can be done in this conversation**.
If you force the work to continue, you will become unable to respond midway, and neither the results nor the
progress so far can be handed over.

**Do not call any more tools.** Do not make the next tool call; using only the information you have now,
output the following two things as text and end your response. Write both in the language of the conversation
with the user (the user's language).

## 1. Status report to the user

- What you were doing (1-2 sentences)
- How far you got (the number/range processed, and the absolute path of the folder where generated files were saved)
- What remains unprocessed (the range and number of targets)
- That the rest cannot be run in this conversation because the token limit was reached

## 2. Handoff prompt to resume in a new chat

Output, in a code block, an instruction text that the user can **copy as is and paste into a new chat to resume
from where you left off**. Always include the following items.

- The goal of the request (the original user request, rewritten concretely without omissions)
- The absolute path of the input folder
- The absolute path of the output folder
- The naming rule for output files and the output format (as specified in the original request)
- **The range already completed** (in a form that avoids duplicate processing, such as "items 1 to 120 are processed"
  or "the output folder already has 120 files")
- **The range not yet processed** (such as "the remaining 177 items from item 121 on")
- The first step to take when resuming (e.g. "Glob the output folder to check the existing count, and resume
  delegation from the first unprocessed item")

Make the handoff prompt self-contained so that someone who does not know the context of this conversation can
resume the work. Do not use expressions that refer to this conversation, such as "continue from before" or
"in the same way as last time".
