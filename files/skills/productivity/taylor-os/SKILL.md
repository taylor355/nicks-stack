---
name: taylor-os
description: Use BEFORE any substantive work on Taylor's businesses, people, priorities, or personal life — drafting to a named person, prepping a meeting, screening an acquisition, weighing a decision, or answering "what do you know about X". Taylor OS is his own written operating context on disk (44 files across TK Holdings, Gibson/URA, Outlaw Industrial, Anderson Tax, SIGNL Advisory, family, people, workflows). Covers where each thing lives, what to read for a given task, and which source wins when two disagree.
version: 1.0.0
author: Nicks Stack
metadata:
  hermes:
    tags: [context, taylor-os, companies, people, priorities, workflows, briefing]
    related_skills: [taylor-email-voice, tk-family-documents, mac-bridge]
---

# Taylor OS

Taylor wrote his own operating context and keeps it in a Git repository. A
read-only mirror lives at:

```
/root/.hermes/taylor_os
```

It is 44 markdown files, about 84KB. **You have it. Read from it rather than
guessing, and rather than asking him to re-explain something he has already
written down.**

`python3 /root/.hermes/scripts/platform/taylor_os_sync.py --status` tells you
what is checked out. If it says nothing is checked out, say so plainly instead
of answering from memory.

**"Sync Taylor OS now"** means run that script without `--status`. It pulls
from GitHub immediately rather than waiting for the hourly job. Taylor asks
for this right after pushing an edit he wants you to see.

**The mirror can only ever be as current as GitHub.** Taylor edits in
`~/Documents/Taylor_OS` on his Mac, and work reaches you only when he pushes.
This has already bitten once: a commit sat unpushed for a day while the sync
reported "up to date" every hour, which was true and useless. So if he refers
to something you cannot find, do not conclude it does not exist — say which
file you looked in, and ask whether it has been pushed. `taylor-os status` on
his Mac answers that in one line.

## Read the smallest useful set

This is Taylor's instruction, in his own file, and it is not a suggestion:

> Do not load every file by default. Pull in the smallest useful set of files
> for the task.

So do not read the tree. Two or three files is a normal answer. The map:

| The question is about | Read |
|---|---|
| Anything substantive, first time in a conversation | `01_Global_Context/Taylor_Profile.md`, `Working_With_Taylor.md` |
| How to say it | `01_Global_Context/Communication_Style.md` |
| What matters more than what | `01_Global_Context/Priorities_And_Principles.md` |
| Corrections he has already given | `07_Memory/Learned_Preferences.md` |
| Portfolio, acquisitions, operators, ownership | `02_Holding_Company/TK_Holdings/` |
| Gibson / URA, insurance leadership | `03_Companies/Gibson_URA/` |
| Outlaw Industrial | `03_Companies/Outlaw_Industrial/` |
| Anderson Tax Consulting | `03_Companies/Anderson_Tax_Consulting/` |
| SIGNL Advisory | `03_Companies/SIGNL_Advisory/` |
| A person, or how to handle them | `05_People_Relationships/People_Map.md` |
| Family, health, personal finance | `04_Personal_Life/` |
| A process he repeats | `06_Workflows_And_Skills/` |
| What he knows is missing | `09_Gaps_And_Strengthening_Plan/` |

Each company folder holds `Company_Context.md` and usually
`Gaps_To_Strengthen.md`. Start with the context file.

## When two sources disagree

There are now two places that describe Taylor's world, and they will drift.
When they conflict:

- **Taylor OS wins on facts about him and the businesses** — who owns what,
  who the people are, what a company does, what he is trying to build, what he
  values. He wrote it. It is the source.
- **Your skills win on mechanics** — how to file a document, how to reach a
  Drive folder, how the Composio accounts map, which tool to call. Taylor OS
  deliberately contains none of that, by his rule, and it never will.

If a real contradiction turns up, do not quietly pick one. Say which two files
disagree and ask. A confident answer built on the stale half is the failure
that matters here.

## MEMORY.md and Learned_Preferences.md

Not the same thing, and not interchangeable.

- `07_Memory/Learned_Preferences.md` in Taylor OS is the **durable** record of
  corrections he has given. It is his, it is versioned, it survives everything.
- Your MEMORY.md is **live state** — capped at 2,200 characters and truncated
  silently when full. It is for what is true this week.

When Taylor corrects something durable about how he works, that belongs in
Taylor OS. Propose the file and the wording; do not write it yourself without
asking. When he tells you something that will be stale in a month, that is
MEMORY.md.

## Never write to the mirror

`/root/.hermes/taylor_os` is a read-only checkout, hard-reset on every sync.
Anything written there is destroyed without warning and never reaches GitHub.

To change Taylor OS: draft the exact file and wording, show him, and let him
commit it. He edits it on GitHub; the sync brings it here within the hour.

His own rule governs what may go in at all:

> Do not add technical system assumptions, tool-specific dependencies, or
> temporary setup details to these files.

So never propose adding Hermes paths, tool names, API details or setup steps to
Taylor OS. That is what your skills are for. The repository stays portable
enough to hand to a new employee, and that portability is the point.

## The through line

He states it twice in his own files, so treat it as load-bearing:

> Resourcefulness over resources. Gritty, scrappy, creative, practical. Nobody
> is above the work.

Practical effect on your answers: when you present options, lead with the one
that can be done now with what he has, not the one that needs a new tool, a new
hire, or a budget. Name obstacles as things to route around, never as reasons
the thing cannot happen. And when the right answer is that he should do it
himself, say so.
