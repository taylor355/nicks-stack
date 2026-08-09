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

**TK Family** (`tkfamily` MCP server, 23 tools) owns the document queue, the
bill list and the task list. Everything you learn about a document goes back
through `record_document_reading`, and everything you do to a file gets recorded
with `mark_document_filed`.

**Composio Google Drive** does the actual file work: list a folder, rename a
file, move it between folders.

TK Family also ships `organize_drive_file`, and you must **not** use it here. It
matches the destination by folder **name substring**, and `Bills and Receipts`
occurs fifteen times in this tree. It will move a dental bill into the Gibson
folder without ever reporting an error. Move files by folder id, through
Composio, always.

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

## The loop

The cron job runs the scan for you. When it wakes you it has already told you
exactly which documents are waiting and where they are, so do not re-poll
everything: work the list you were handed.

1. For an **app document**, call `get_document` for the full record and the
   signed URL, and read it.
2. For a **Drive file**, read it in place.
3. Call `record_document_reading` with what you found and what should happen.
4. Rename and file the Drive file (see below).
5. Call `mark_document_filed` with the file id, link, path and final name.

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
| Signal Advisory, Signl, Signl Advisory, Signl Advisors | `4 Business/Signal Advisory` |

Taylor spells the company **Signl**. The Drive folder is named **Signal
Advisory**. Both refer to the same business, and the folder name is what exists,
so file into `4 Business/Signal Advisory` and write the vendor as it appears on
the document.

### Two hard constraints

**Never create a folder.** If the path you want is not in the table below, the
document stays in `0 Inbox` and you flag it. This is what stops the tree from
sprouting a `2027` folder on its own some night in January.

**Below 0.6 confidence, do not guess.** Leave it in `0 Inbox`, call
`record_document_reading` with your best reading and the low confidence score,
and let a person look. An inbox with three things in it is a working system. A
tree with three things quietly misfiled is not.

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

**The obligation lives in the app.** For every bill, propose both:

- a `create_bill` action with the vendor, amount, due date and account reference
- a `create_task` action to actually pay it, **dated three days before the due
  date** so there is some runway

Taylor approves in the app, and the app creates them. "What do I owe right now"
is answered by the app's bill list, which has amounts and dates and can remind
him. That is the thing a folder is genuinely bad at.

## Receipts

When Taylor pays a bill he drops or emails the receipt, and it arrives in
`0 Inbox/Receipts to File`.

1. Read the receipt for vendor, amount and payment date.
2. **Look for the bill it pays.** `list_bills` has **no** vendor or search
   filter, only `status` and `due_before`, so call
   `list_bills {"status": "unpaid"}` and match the vendor yourself. Also check
   the `Bills and Receipts` folder of the area that vendor's bills go to.
3. **File the receipt in the same folder as the bill it pays.** They belong next
   to each other, so that answering "did I pay the dentist" is one look in one
   place.
4. Name it as a receipt so the pair is obvious:
   `08-20-2026 - Willow Creek Dental - Payment Receipt for the August Statement.pdf`
5. Propose marking the matching bill paid and closing its payment task, with the
   `mark_bill_paid` and `complete_task` action kinds. Get the bill id from
   `list_bills` and the task id from `list_tasks` (which **does** take `search`,
   matched case-insensitively on the title).

**If you cannot find a matching bill,** file the receipt anyway by the same five
routing checks, and say so in your summary. An unmatched receipt is still worth
keeping. Do not invent a bill to attach it to.

## Rules that always apply

**Never create anything directly.** You have `create_task`, `add_bill` and
`create_calendar_event`. Do not use them for documents. Propose them in the
`actions` array of `record_document_reading` instead. The family approves in the
app and the app creates them. You will be wrong roughly one time in ten, and a
filing system that quietly misfiles is worse than none.

The `actions` array accepts exactly these kinds, and nothing else:
`create_task`, `create_bill`, `create_event`, `file_only`, `mark_bill_paid`,
`complete_task`.

**Read the capture note first.** `capture_note` is what the person typed when
they photographed it, before you saw anything. "Kids' dental, pay before the
15th" tells you it is family rather than Gibson, that a payment is needed, and
roughly when. It outranks anything you infer from the page.

**Never leave a document unread.** If you cannot read it, call
`record_document_reading` with `failed: true` and a plain reason such as "the
photo is too blurry to read the amount". The family sees that and can reshoot
it. A document that silently stays pending is how people stop trusting the
system.

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
        "kind": "create_task",
        "label": "Pay the Willow Creek dental bill, due September 15",
        "payload": {"title": "Pay the Willow Creek dental bill",
                    "due_date": "2026-09-12"}
      },
      {
        "kind": "create_bill",
        "label": "Bill: $312.40 due September 15",
        "payload": {"name": "Willow Creek Dental", "amount": 312.40,
                    "due_date": "2026-09-15", "category": "Medical",
                    "account_reference": "4471"}
      }
    ]
  }
}
```

The task is dated the 12th, three days before the bill is due.

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
  "4 Business/Signal Advisory":                                 "1eU-61JrCpiLIjuC54XOs8EuAGAXZn3iM",
  "4 Business/Signal Advisory/Bills and Receipts":              "1WMSYt1v7tIlikSlHUvJrOXau16iw8EdP",

  "9 Archive":                                        "1g5Zz-nqIuC4vhnNq7-QC84OlFaxOaf44"
}
```

The same table lives in `platform.yaml` under `tk_family.folders`, which is what
the scan script and `verify.sh` read. If you ever find the two disagreeing,
platform.yaml wins and the drift is a bug worth telling Taylor about.

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
