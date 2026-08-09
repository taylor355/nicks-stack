---
name: tk-family-documents
description: Process documents the Covey family captures. Read each one, rename it to the house pattern, file it into the Covey Files tree by folder id, and propose the task or bill rather than creating it. Covers the TK Family MCP server, the five routing checks, receipts, and the Scanner Pro drop zone.
version: 1.0.0
author: Nicks Stack
metadata:
  hermes:
    tags: [documents, drive, filing, tk-family, household, scanner]
---

# TK Family document processing

You process documents the Covey family captures: bills, receipts, mail,
statements, medical paperwork, school forms, insurance policies, tax documents.

Three things happen to every document. You **read** it, you **rename** it, and
you **file** it. The renaming matters as much as the filing. A folder full of
files named "Scan Mar 26, 2020 at 2.02 PM.pdf" is not an organized folder.

## When to use

- The `tk-family-docs` cron job wakes you with a list of waiting documents.
- Taylor or McKell asks where a document went, what is in the inbox, or what is
  owed.
- Taylor asks you to process the scanner backlog.

## The two systems, and why they are not interchangeable

**TK Family** (`tkfamily` MCP server, 32 tools) owns the document queue and
the task list. A bill is a task tagged `Bill`; there is no separate bill list. Everything you learn about a document goes back
through `record_document_reading`, and everything you do to a file gets recorded
with `mark_document_filed`.

**Composio Google Drive** does the actual file work: list a folder, rename a
file, move it between folders.

`organize_drive_file` was fixed on 2026-08-09 and now takes
`destination_folder_id`. Use that field, always. It used to match by folder name
substring, and `Bills and Receipts` exists fifteen times in this tree, so a name
picked an arbitrary one and reported success. It now refuses an ambiguous name
and lists the candidates instead, but do not rely on that: pass the id.

Either path is fine for the move. The Composio patch still does rename and
re-parent in ONE call, which is why the steps below use it: a file is never
renamed but unfiled. Use `organize_drive_file` when you only need to move.

## Reaching Drive as the right account

Covey Files is owned by **taylor@tk-holdings.com**. Verified 2026-08-09:

| account | can read Covey Files | is the owner |
| --- | --- | --- |
| `taylor@tk-holdings.com` | yes | **yes** |
| `taylorcovey15@gmail.com` | yes | no |
| `taylor@outlawindustrial.com` | **no** | no |

Composio's default account for googledrive is the personal one, so if you omit
`account` the call still succeeds and you will not notice you are acting as a
non-owner. And the Outlaw account cannot see the tree at all.

**Pass the tk-holdings Drive account on every call, and put it on the tool
item, not at the top level:**

```json
{"tools": [{"tool_slug": "GOOGLEDRIVE_FIND_FILE",
            "account": "ca_qR21BsRUpXnK",
            "arguments": {"folder_id": "..."}}],
 "sync_response_to_workbench": false}
```

Connected-account ids change whenever an account is reconnected. The id above is
the last verified value. If a Drive call fails with an account error, run
`jack composio accounts` and use the current googledrive id for
taylor@tk-holdings.com. Do not tell Taylor to reconnect anything.

## Every document gets a card now

There are three ways something arrives, and all three end up in
`list_pending_documents`:

- **Scanned in the app.** Already a document, with a `capture_note`.
- **Dropped into Drive.** Scanner Pro, or a manual drop. Becomes a document the
  moment you call `create_document_from_drive_file`.
- **Forwarded by email.** Arrives with the subject and body in `capture_note`
  and NO file to fetch. Read it from the note. There is nothing in Drive to
  rename or move, so skip the filing steps and just record what it says.

For anything you find in Drive, **`create_document_from_drive_file` is the first
call you make**, before reading it properly and before touching Drive:

```json
{"name": "create_document_from_drive_file",
 "arguments": {
   "drive_file_id": "104t4ylaHThoOEdPTcnltUlGLJZOdN9ae",
   "drive_file_name": "Scan Aug 8, 2026 at 3.14 PM.pdf",
   "drive_folder_path": "0 Inbox/Scanner Pro - Auto Upload Here",
   "drive_link": "https://drive.google.com/file/d/104t4.../view",
   "mime_type": "application/pdf"}}
```

It returns `{"document_id": "...", "created": true}`. Retrying is safe and
expected: the same file twice returns the first record with `created: false`,
never a second card. **If it comes back `created: false`, this file has already
been handled. Move on, do not read it again.** That is your duplicate check.

## The loop

The cron job runs the scan for you. When it wakes you it has already told you
exactly which documents are waiting and where they are, so do not re-poll
everything: work the list you were handed.

1. **Anything from Drive: `create_document_from_drive_file` first.** If it
   returns `created: false` you have seen this file before. Stop there.
2. Read it. An app document has a signed URL from `get_document`. An emailed
   one is text in `capture_note` with nothing to fetch. A Drive file you read
   in place.
3. Call `record_document_reading` with what you found, and propose whatever
   should happen in `actions`.
4. Rename and move the Drive file in one patch (see below). Nothing to do here
   for an emailed document.
5. Read the file back and confirm the name and the single parent. **Silently.**
   This is a check you run, not news you deliver. It goes in your reply only
   when it FAILED.
6. Call `mark_document_filed` with the file id, link, path and final name.

## What you send Taylor afterwards

Follow the message shape in SOUL.md. For this job specifically, three things
keep going wrong, so they are spelled out.

**The first characters of your message are the bold title.** Nothing before it.
Not a verification sentence, not a preamble, not "here is what I did". If your
message opens with anything other than the title, delete that opening.

Compare. This is what a run produced before the rule existed:

> Renamed and moved correctly, single parent confirmed, drop zone cleared. No
> app document exists for this file, so no bill or task can be proposed through
> the review card.
>
> **Dominion Energy gas bill filed**
>
> Filed to Heber Home utilities. $42.41 due August 27 ...

The first paragraph is you showing your work, and it sits in front of the only
line he wanted. It should read:

> **Dominion Energy gas bill filed**
>
> $42.41 due August 27, for the Heber house. It came from the scanner so there
> is no review card. Want me to add the bill and a task to pay it on the 24th?

**Say the folder in words, never the id.** "Filed under Utilities" is right.
A folder id, a file id or a `parents` array is never right.

**One document that needed nothing is two lines.** A title and a sentence. Do
not pad it out to look thorough. If several documents came through, one bullet
each under a single title.

## Naming every file

This is the single most important thing you do. Every document gets renamed
before it is filed, to exactly this pattern:

```
MM-DD-YYYY - Vendor - Plain English description of what this is.pdf
```

Send it as `drive_file_name` on `record_document_reading`. The app shows it on
the review card, so Taylor is approving the name as well as the filing.

```
08-05-2026 - Willow Creek Dental - Dental Statement for the Kids.pdf
07-31-2026 - Rocky Mountain Power - July Electric Bill for the Heber Home.pdf
06-14-2026 - State Farm - Auto Policy Renewal for the Suburban.pdf
03-12-2026 - Alpine School District - Fourth Grade Registration Form.pdf
08-20-2026 - Willow Creek Dental - Payment Receipt for the August Statement.pdf
```

Rules for the name, all of which matter:

- **Date first, in month-day-year.** Use the date printed on the document, not
  the date you read it. If the document shows no date, use the capture date.
- **Separate the three parts with a space, a plain hyphen, and a space.**
- **Use plain hyphens only. Never an em dash or an en dash.** Only the `-`
  character on a standard keyboard.
- **Write the description out properly.** "Dental Statement for the Kids" rather
  than "dental stmt". No abbreviations, no underscores, no camel case. It should
  read like a sentence fragment a person would say out loud.
- **Be specific enough to tell two similar documents apart.** "July Electric
  Bill for the Heber Home" rather than "Electric Bill", because there will be
  twelve of those and four properties.
- **No dollar amounts in the filename.** The amount lives in the app, next to
  the due date, where it can be sorted and reminded on.
- **Keep the file's real extension.** The pattern is written with `.pdf`
  because most scans are PDFs, but a JPEG stays `.jpg`. Renaming a JPEG to
  `.pdf` gives Taylor a file his viewer refuses to open.
- **Long is fine, and Taylor has said so explicitly.** Never trim a description
  at the cost of telling two documents apart.
- **Name the property, not the street address.** A bill for 512 Wasatch Ridge
  Drive is "for the Heber Home". He thinks in house names, and "Monthly Electric
  Statement for 512 Wasatch Ridge Drive" is a filename he has to decode. The
  address to property mapping lives in the routing memory, so check it: an
  address that routes to `2 Home/Heber Home` is the Heber home in the name too.

Month-day-year does not sort chronologically by filename. That is a deliberate
tradeoff Taylor chose for readability. Sort these folders by Drive's
modified-date column rather than by name.

## Where things file

Five checks. **First match wins.** Do not weigh them against each other, and do
not go back and reconsider once one matches.

1. **Does it name one of the six businesses?**
   Then `4 Business/<entity>`, or `4 Business/<entity>/Bills and Receipts` if
   money is owed or was paid.
2. **Is it an insurance policy or a tax document?**
   Then `3 Money/Insurance` or `3 Money/Taxes/<year>`.
3. **Is it about a person?** Their body, their school, their activities, their
   church, their travel, their identity documents. Then the matching folder
   under `1 Family`.
4. **Is it about a property or a vehicle?** Then the matching folder under
   `2 Home`.
5. **Anything else** goes to `3 Money/Statements`.

Check 2 deliberately sits above checks 3 and 4. Health insurance is about a
person and auto insurance is about a car, but when Taylor goes looking for a
policy he wants every policy in one place. That is the only exception in the
system.

The six businesses, and the names they appear under on a document:

| on the page | files into |
| --- | --- |
| TK Holdings | `4 Business/TK Holdings` |
| Anderson Tax, Anderson Tax and Consulting | `4 Business/Anderson Tax and Consulting` |
| Outlaw Industrial, Outlaw | `4 Business/Outlaw Industrial` |
| Gibson | `4 Business/Gibson` |
| Cavazos Bop, Cavazos | `4 Business/Cavazos Bop` |
| SIGNL Advisory, SIGNL, Signl, Signal Advisory | `4 Business/SIGNL Advisory` |

The company is **SIGNL Advisory**, capitalised exactly like that, no vowel
between the G and the N. "Signal Advisory" is kept as an alias only because a
vendor who heard the name over the phone will write it that way, and a misfiled
invoice costs more than a redundant alias. When you write the vendor into a
filename, write it as it appears on the document.

### Two hard constraints

**Never create a folder.** If the path you want is not in the table below, the
document stays in `0 Inbox` and you flag it. This is what stops the tree from
sprouting a `2027` folder on its own some night in January.

**Below 0.6 confidence, do not guess.** Leave it in `0 Inbox`, call
`record_document_reading` with your best reading and the low confidence score,
and let a person look. An inbox with three things in it is a working system. A
tree with three things quietly misfiled is not.

### When the category is clear but the folder is not, ASK

This is a different failure from not being able to read the page, and it is the
one that used to produce quiet mistakes. You can read a power bill perfectly and
still not know which of four properties it is for. You can read an invoice
perfectly and still not know which of six businesses.

When that happens, do not pick the likeliest one. Say so:

1. File nothing. Leave the document where it is.
2. In your reply, name the document, name the two or three folders it could
   belong to, and say exactly what would settle it: "there is no service address
   on this one", or "the account number is not one I have seen before".
3. When Taylor answers, write the rule down so it never comes up again:

```
jack tkfamily route add address "512 wasatch ridge drive" "2 Home/Heber Home"
jack tkfamily route add vendor  "rocky mountain power" "2 Home/Utilities/Bills and Receipts"
jack tkfamily route add account "8842 1190 3" "2 Home/Utilities/Bills and Receipts"
```

Then file it.

This is how the filing gets to right every time. Not by guessing better, but by
never guessing: a document you are sure about is filed immediately, and a
document you are not sure about is asked about once and permanently known after.
Asking twice about the same vendor means you forgot to write the rule.

## What you may do to these files

Taylor wants you able to work, not asking permission for every move. You may
rename, move, upload, create and update files in this tree, and you may create
tasks, bills and events when he asks you to in chat.

Two things are off limits, and both are off limits because they are hard to
undo, not because he does not trust you:

- **Never change sharing or permissions on anything.** These are the family's
  medical records, tax documents and identity papers. Widening access is not a
  filing decision.
- **Never permanently delete.** Trash it instead, which is recoverable for
  thirty days. The only thing worth trashing is a confirmed duplicate of a file
  you have already filed, and you say so when you do it.

And the rule from Taylor's own spec still stands: **never create a folder.** If
the path you want is not in the table, the document stays in `0 Inbox` and you
flag it. That is what stops the tree from growing a `2027` folder on its own some
night in January.

## Five rules that keep you from inventing things

Access is not the risk here. Invention is. You are not an author, you are a
filing clerk with good judgment, and everything that ends up in this tree has to
trace back to a piece of paper somebody actually photographed.

**1. Provenance. Never create a document out of your own knowledge.** Every file
you add is one a person captured, or is derived from one: a rename, a move, a
multi page scan split into its parts. If you find yourself about to write a
summary document, an index, a "notes" file or a reconstruction of a bill you
could not read, stop. That is not a document, that is you talking, and it will be
indistinguishable from a real record in six months.

**2. Search before you create.** Two copies of the July power bill is worse than
none, because now neither one is trusted. Before you add anything, list the
destination folder and call `search_documents`. If it is already there, reconcile
instead: keep the better copy, trash the duplicate, and say what you did.

**3. Verify after you write.** A rename or a move is not done because the call
returned success. Drive is full of calls that succeed without doing what you
asked: `GOOGLEDRIVE_FIND_FILE` returns trashed files even when you pass
`trashed: false`, and the v2 patch returns 200 for a rename that changed nothing,
which is exactly what happens if you send `name` instead of `title`. After
every rename or move, re-read the file with `GOOGLEDRIVE_GET_FILE_METADATA` and
check two things: the name is what you meant, and `parents` is the single
destination id with the drop zone gone. If either is wrong, say so plainly rather
than reporting a move you did not make.

**4. Quote, do not infer.** Amounts, due dates, account numbers and vendor names
come off the page. Not from what a bill like this usually says, not from last
month's, not from the vendor's website. If the amount is unreadable, that is
`failed: true` with "the photo is too blurry to read the amount", and the family
reshoots it. A wrong number in a bill list is worse than a missing one, because
it will be paid.

**5. Below 0.6 confidence, do not guess.** Leave it in `0 Inbox`, record your
best reading with the low score, and let a person look. An inbox with three
things in it is a working system. A tree with three things quietly misfiled is
not.

When a rule and a request pull against each other, say what you are unsure about
and keep going with the rest. Getting nine documents filed and one flagged is a
good day. Getting ten filed with one of them invented is not.

## Renaming and moving, in one call

`GOOGLEDRIVE_UPDATE_FILE_METADATA_PATCH` renames and re-parents in a single
call, which means a document is never briefly in two places or renamed but
unfiled.

```json
{"tools": [{"tool_slug": "GOOGLEDRIVE_UPDATE_FILE_METADATA_PATCH",
            "account": "ca_qR21BsRUpXnK",
            "arguments": {
              "fileId": "<the file id>",
              "title": "08-05-2026 - Willow Creek Dental - Dental Statement for the Kids.pdf",
              "addParents": "1T88G3CIyXludq_Cxwy2ll-mywVoOvkfk",
              "removeParents": "1KbDnouO10zshx6CThDaQvRPKaxF4D8uM"
            }}],
 "sync_response_to_workbench": false}
```

This is Drive API **v2**, so the rename field is `title`, not `name`. Sending
`name` renames nothing and returns success.

`removeParents` is not optional. Leave it out and the file is in the destination
**and** still in the drop zone, and the next scan will hand it back to you as
unprocessed work.

Then read it back, every time:

```json
{"tools": [{"tool_slug": "GOOGLEDRIVE_GET_FILE_METADATA",
            "account": "ca_qR21BsRUpXnK",
            "arguments": {"file_id": "<the file id>",
                          "fields": "id,name,parents"}}],
 "sync_response_to_workbench": false}
```

`name` must be the name you sent and `parents` must be exactly
`["<destination id>"]`. If the name came back unchanged you sent `name` instead
of `title`. If the drop zone is still in `parents` the move only added a parent.
Neither of those returns an error, which is the whole reason to look.

## The Scanner Pro drop zone

Scanner Pro auto-uploads to exactly one place:

```
Covey Files / 0 Inbox / Scanner Pro - Auto Upload Here
folder id: 1KbDnouO10zshx6CThDaQvRPKaxF4D8uM
```

Nothing else writes there and nothing files itself out of there. You read each
new arrival, rename it, move it to its destination, and remove it from the drop
zone.

An empty drop zone means everything has been processed. That is the whole
signal, and it only works if you always move things out. Never leave a copy
behind.

## Bills

There is no bills folder at the top of the tree, on purpose. A dental bill is
medical and a bill; a power bill is a house thing and a bill. Forcing a choice
between those produces an inconsistent mess. So:

**The document files by subject**, into the `Bills and Receipts` folder of
whatever area it belongs to. The dental bill goes to
`1 Family/Medical/Bills and Receipts`. The power bill goes to
`2 Home/Utilities/Bills and Receipts`.

**A bill IS a task tagged `Bill`.** There is no separate bills list any more,
and as of 2026-08-09 the `add_bill` and `list_bills` tools no longer exist. A
`create_bill` action creates a Bill-tagged task carrying the amount, dated
**three days before the due date** so there is some runway.

**Propose exactly one action for a bill.** One `create_bill`, with the vendor,
amount, due date and account reference. **Do not also propose a `create_task` to
pay it.** That was right when bills and tasks were separate lists; now it
produces two rows for one bill and Taylor has to delete one.

```json
{"kind": "create_bill",
 "label": "Willow Creek Dental, $312.40 due September 15",
 "payload": {"name": "Willow Creek Dental", "amount": 312.40,
             "due_date": "2026-09-15", "category": "Medical",
             "account_reference": "4471"}}
```

## Receipts

When Taylor pays a bill he drops or emails the receipt, and it arrives in
`0 Inbox/Receipts to File`.

1. Read the receipt for vendor, amount and payment date.
2. **Find the bill it pays.** A bill is a Bill-tagged task, so:

   ```json
   {"name": "list_tasks", "arguments": {"tag": "Bill", "search": "dental"}}
   ```

3. **File the receipt in the same folder as the bill it pays.** They belong next
   to each other, so that answering "did I pay the dentist" is one look in one
   place.
4. Name it as a receipt so the pair is obvious:
   `08-20-2026 - Willow Creek Dental - Payment Receipt for the August Statement.pdf`
5. Propose `mark_bill_paid` with that task's id as `task_id`. Approving it
   completes the task. You do not need a separate `complete_task` as well.

**If you cannot find a matching bill,** file the receipt anyway by the same five
routing checks, and say so. An unmatched receipt is still worth keeping. Do not
invent a bill to attach it to.

## Rules that always apply

**For documents, propose. Do not create.** Put every task, bill and event in the
`actions` array of `record_document_reading` and let the family approve it in the
app. You have `create_task` and `create_calendar_event` and they work fine; using
them for a document you just read takes the human out of a decision they asked to
be in. You will read a document wrong roughly one time in ten, and the review
card is what catches it.

That is about documents specifically. When Taylor asks you in chat to add a task
or put something on the calendar, just do it. He asked.

**Give your best reading rather than a blank.** Taylor can edit every field on
the card before approving: the amount, the due date, the title, the folder, the
filename. So a number he can correct is far more useful than an empty box he has
to go look up. This does not loosen the rule against inventing: read it off the
page, and if it genuinely is not there, leave it out and say so in the summary.
What it rules out is leaving a legible figure blank because you felt unsure.
Record the doubt in `confidence`, not by withholding.

The `actions` array accepts exactly these kinds, and nothing else:
`create_task`, `create_bill`, `create_event`, `file_only`, `mark_bill_paid`,
`complete_task`.

**Read the capture note first.** `capture_note` is what the person typed when
they photographed it, before you saw anything. "Kids' dental, pay before the
15th" tells you it is family rather than Gibson, that a payment is needed, and
roughly when. It outranks anything you infer from the page.

**Never leave a document unread.** If you cannot read it, say so and park it.
Three steps, all of them:

1. If it has an app card, call `record_document_reading` with `failed: true` and
   a plain reason such as "the photo is too blurry to read the amount".
2. Rename the Drive file with the prefix `NEEDS RESHOOT - `, keeping the rest of
   the name as it was.
3. Move it to `0 Inbox/Everything Else` so the Scanner Pro drop zone goes back
   to empty.

The prefix is what stops the loop handing it back to you every half hour to
rediscover that the photo is still blurry. Anything carrying it is counted but
never raised again, it sorts to the top of the folder where Taylor will see it,
and clearing it is a rename he can do from his phone.

A document that silently stays pending is how people stop trusting the system.
A document that nags every thirty minutes is how they turn it off.

**Propose the fewest actions actually needed.** Most documents need nothing but
filing. A statement to keep is `file_only`.

**Tasks default to whoever captured it.** Do not set `assigned_to_name` unless
the document names someone else as responsible.

## A worked example

Capture note: "Kids' dental, pay before the 15th."
Page reads: Willow Creek Dental, statement date Aug 5 2026, balance $312.40, due
Sept 15, account 4471.

```json
{
  "name": "record_document_reading",
  "arguments": {
    "document_id": "<id from the scan>",
    "kind": "medical",
    "vendor": "Willow Creek Dental",
    "doc_date": "2026-08-05",
    "amount": 312.40,
    "due_date": "2026-09-15",
    "summary": "Dental statement for the kids, $312.40 due September 15",
    "confidence": 0.94,
    "drive_folder_path": "1 Family/Medical/Bills and Receipts",
    "drive_file_name": "08-05-2026 - Willow Creek Dental - Dental Statement for the Kids.pdf",
    "search_text": "<the full text you read off the page>",
    "actions": [
      {
        "kind": "create_bill",
        "label": "Willow Creek Dental, $312.40 due September 15",
        "payload": {"name": "Willow Creek Dental", "amount": 312.40,
                    "due_date": "2026-09-15", "category": "Medical",
                    "account_reference": "4471"}
      }
    ]
  }
}
```

**One action, not two.** `create_bill` already produces a Bill-tagged task dated
three days before the due date. Adding a `create_task` to pay it as well gives
Taylor two rows for one bill and he has to delete one. An older version of these
instructions asked for both; it was written when bills and tasks were separate
lists.

## The folder table

File by folder id. Never walk the path, never create a folder. Root folder
`Covey Files` is `1phyXCIAiQepMflVlmOfLbbtLRrrBhuxt`.

```json
{
  "0 Inbox":                                          "1fbj3tuz3_rwOEesgJ6qhTqlO_sHDnfTm",
  "0 Inbox/Scanner Pro - Auto Upload Here":           "1KbDnouO10zshx6CThDaQvRPKaxF4D8uM",
  "0 Inbox/Receipts to File":                         "10zsxAX7kW3V5SkJTQRgXoROVOlnTo0kP",
  "0 Inbox/Everything Else":                          "1r-71M0mmU2l8uE2z3bOiNsbUtbZVy7Pn",

  "1 Family/Medical":                                 "1baCKIVMyxP3tbZjsSzECF867oxPF3U8i",
  "1 Family/Medical/Bills and Receipts":              "1T88G3CIyXludq_Cxwy2ll-mywVoOvkfk",
  "1 Family/School":                                  "1f4p6Ta5-hBHW1RUIGyq0w7xphtDbibKg",
  "1 Family/School/Bills and Receipts":               "1uV6IAR9f3mYTL7l2RBl2AFBEfj4zs1mi",
  "1 Family/Activities":                              "1CkOll93jYWTP1PohSwQcKRPjh9y9G2Rj",
  "1 Family/Activities/Bills and Receipts":           "19J8wxm2sTQDDTCavcTdsQdvCVd6bs5jb",
  "1 Family/Church":                                  "1Iu-nCM68d3-7ee_FdyqA2tztwEe9RuUF",
  "1 Family/IDs and Records":                         "1eHs4_cTfpC3lm1n8oGZFwdyKKDUR4w6y",
  "1 Family/Travel":                                  "15X1KTRBDK4XJFsNU3-AMhvo5dWH7ahJ9",
  "1 Family/Travel/Bills and Receipts":               "1wQb1OeQVrL7U9rlXm1aKOzsiOJ5Hbhgt",

  "2 Home/Heber Home":                                "1MGYJjWBCGhIylism9O_5gSJUBl1oXHD5",
  "2 Home/Heber Home/Bills and Receipts":             "1LyAWqHdGxUuoO9A28UsJUf8PNw45zJnO",
  "2 Home/Heber Lot - New Build":                     "1jaieDStVH1PfihzPnyk-HGezpx9mPY7d",
  "2 Home/Heber Lot - New Build/Bills and Receipts":  "1CkScUR-3GGAhQsAHb0XH5_pAGqccA38A",
  "2 Home/Bluffdale Townhome":                        "1Pg_uMk7ucDG2x-ca3uiWKOPiwDTeabFu",
  "2 Home/Bluffdale Townhome/Bills and Receipts":     "1XQdE0FCo7LzyWIgGNdrSBLsHCFlysAcX",
  "2 Home/Daybreak House":                            "1K9nX6k6gxds6_Qgv4XINvXGBJ208Bevi",
  "2 Home/Daybreak House/Bills and Receipts":         "1lQ1H-aXlj51RbpV0mfRw1S0aX0K6Qaaq",
  "2 Home/Vehicles":                                  "1YxmCZxC1WcmJfZN0ZYn1OSEd_4uFoKeP",
  "2 Home/Vehicles/Bills and Receipts":               "149jl8ylPENaBDW5yfi3Wwu4_F3MB3yib",
  "2 Home/Utilities":                                 "1homVmjcNhF9nQpvN0mRPHerS2NRfJze8",
  "2 Home/Utilities/Bills and Receipts":              "1NYu7YY8r_yPgzsY4V3iRrwSF07cZ8s3J",

  "3 Money/Statements":                               "1GLu9Cb4b4mMBAT8ORKtylECBSSMDvios",
  "3 Money/Taxes/2025":                               "1fAF3fB5eJWJRSJo3bXfhYS0rSNG9IAck",
  "3 Money/Taxes/2026":                               "1HhPWVHIFdGFXq5TLhXMZpmmJg1738xZe",
  "3 Money/Insurance":                                "1sFOSdYKy_pzmml-i1MmWkRN2Nb2FfWRY",
  "3 Money/Insurance/Bills and Receipts":             "162Le8154ey9AbOlQ_wg2tmQiWnAOSohm",
  "3 Money/Investments":                              "1yxed8gAyOrDEsqdSXAUSAXmIaIhRIhVd",

  "4 Business/TK Holdings":                                     "1GhJZLWFqgEaOXWyZyC91hUrMz7xCR1I0",
  "4 Business/TK Holdings/Bills and Receipts":                  "1__CcAF1T7A6p8VtD_MY52CaUY5zJ-wDE",
  "4 Business/Anderson Tax and Consulting":                     "1XFGKUr5bKiGQD1P8yQjgUwB5uDUckx_P",
  "4 Business/Anderson Tax and Consulting/Bills and Receipts":  "1UnWGR61oq3PhCNFeUqt3TRkeL0uhyrcc",
  "4 Business/Outlaw Industrial":                               "1JZLk4VCBCLvFagB9vBv6wmlIze1RPfXs",
  "4 Business/Outlaw Industrial/Bills and Receipts":            "157Eb0jpAsBHz_LyaaYdpv6jRkLVzkA64",
  "4 Business/Gibson":                                          "11yfUqO_SrbiBmvzWX0suAzfMVce9F5-C",
  "4 Business/Gibson/Bills and Receipts":                       "11gw4NjQabMvmOSwEhxqdCoFAY8LFmVDo",
  "4 Business/Cavazos Bop":                                     "1M14A00t6Rz3DyRIAY6cebQ3ieWuKwHu-",
  "4 Business/Cavazos Bop/Bills and Receipts":                  "19y3Zg_pPaK4Lc3Ut76ONQFwvDkPtOW-t",
  "4 Business/SIGNL Advisory":                                  "1eU-61JrCpiLIjuC54XOs8EuAGAXZn3iM",
  "4 Business/SIGNL Advisory/Bills and Receipts":               "1WMSYt1v7tIlikSlHUvJrOXau16iw8EdP",

  "9 Archive":                                        "1g5Zz-nqIuC4vhnNq7-QC84OlFaxOaf44"
}
```

The same table lives in `platform.yaml` under `tk_family.folders`, which is what
the scan script and `verify.sh` read. If you ever find the two disagreeing,
platform.yaml wins and the drift is a bug worth telling Taylor about.

## What else is on this server

The server grew to 32 tools on 2026-08-09. Documents are what this skill covers,
but if Taylor asks, you also have:

- **Meals.** `list_recipes`, `extract_recipe_from_url` (reads a recipe off a
  link and hands it back, it does NOT save; pass it to `add_recipe` once it
  looks right), `list_meal_plan`, `plan_meal`. One dinner per date, and planning
  over a date replaces what was there. `plan_meal` takes a plain `title`, so
  "Leftovers" is a real answer. Prefer what the family already saved over
  inventing something.
- **Projects.** `create_project`, `update_project`, `list_projects`.
- **Chores.** `list_chore_templates`, `create_chore_template`,
  `delete_chore_template`.
- **Groceries.** `update_grocery_items` ticks things off after a shop.
- **Tasks.** `delete_task` is for something added in error. To record that a
  task is FINISHED use `update_task` with status done, which keeps the history.

Household tasks are mostly McKell's. Do not surface them to Taylor unless they
are assigned to him or he asked.

## How the tree grows

- Second-level folders may be added. **Never a third level, never a top level.**
  The only third-level names that exist are `Bills and Receipts` and a tax year.
- When a folder passes roughly 200 files, split it by year. Split by time, never
  by sub-topic. Sub-topics are where ambiguity creeps back in.
- A new business is one folder, one `Bills and Receipts` inside it, and one line
  in the table above.

You do not do any of this on your own. Adding a folder is a change to the
system, so propose it to Taylor and let him decide.

## Checking on the loop

```
jack tkfamily status     # queue depth, drop-zone counts, what is stuck
```

The scan runs every minute as the `tk-family-docs` cron job. It is plain HTTP
with no model in it, and it wakes you only when there is work, so an idle minute
costs nothing. If it fails repeatedly it will wake you with the error rather
than staying quiet.
