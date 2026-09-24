Rules for AI agents (OpenCode, Claude Code) that work on the Zotero library through the zotero MCP server. Code: ~/Projects/zotero-local-mcp. Tag vocabulary: [[Zotero tags]].

## Safety

- Every write tool has a dry_run option. Call it first with dry_run=true. Show the result. Call it with dry_run=false only after Tiago approves that batch.
- Add only tags from [[Zotero tags]]. If an item needs a tag that is not there, propose it to Tiago. Do not edit [[Zotero tags]] without his approval.
- Work in batches of 25 to 50 items.
- Do not guess the reading status. Set status/ tags only as Tiago says.
- To revert a change, use history and undo. Do not reverse changes by hand.
- Move items to the trash only when Tiago names them. Nothing is deleted permanently.
- The first write opens a dialog in Zotero. Tell Tiago to choose Always Allow.
- If Zotero is not running, stop and tell Tiago. Do not work around it.

## Tag system

- Facets: topic/ (what the item is about), method/ (how the work was done), type/ (kind of study or document), status/ (reading status).
- Every item needs at least one topic/ tag and exactly one status/ tag. Add method/ and type/ tags when they apply. Books get a type/ tag (textbook, monograph, edited-book).
- Use 1 to 4 topic/ tags. Prefer the most specific tag.
- Tags are lower case, words joined with hyphens.
- The server adds the marker tag _agent to every item that the agent tags. After review, Tiago removes the marker with remove_tags ["_agent"].
- Automatic tags (MeSH and keywords from imports) are hints only. After the library is tagged, remove them with remove_automatic_tags.

## Tagging untagged items

1. Call find_items with missing_facet="topic", detail=true, limit=25, offset=0.
2. Write a review file: Inbox/Zotero tag review NN.md (NN = batch number). Make one table row per item with these columns: key, citekey, item (first author, year, short title), proposed tags. Add a reason only for uncertain rows.
3. Give Tiago the file name. He edits the proposed tags column.
4. When Tiago approves the batch: read the file again. Call tag_items with exactly the tags in the file, first with dry_run=true, then with dry_run=false.
5. Report the missing_facet counts from library_overview. Continue with the next batch.

## Cleaning existing tags

1. Call list_tags with outside_vocabulary=true.
2. Write Inbox/Zotero tag mapping.md: one row per old tag with its item count and the proposed action (rename to a vocabulary tag, or remove).
3. After Tiago approves: call rename_tags and remove_tags, first with dry_run=true.

## Citekeys

- Format: first author surname and year (jacinto2026). Collisions get a, b, c. Use set_citekeys.
- Do not change an existing key unless Tiago asks (force=true).

## Literature notes

- Folder: Resources/Zotero/. File name: the citekey (jacinto2026.md).
- Create a note only when Tiago asks. Ask before creating more than 10 notes.
- Use get_item for the data. Do not copy the abstract in full.
- Write the summary from the abstract and the Zotero notes. If you did not read the full text, write "(from abstract)" after the summary. Do not invent results or numbers.

Template:

```markdown
---
title: "Full title"
authors:
  - Surname, Given
year: 2026
citekey: jacinto2026
item-type: journalArticle
venue: Journal name
doi: 10.xxxx/xxxxx
zotero: zotero://select/library/items/ITEMKEY
status: to-read
tags:
  - topic/spirometry
  - type/cohort
created: YYYY-MM-DD
---

## Summary

## Key points

## Methods

## Notes
```

- status: the status/ tag without the prefix (same values as the Kindle notes).
- tags: the topic/, method/ and type/ tags from Zotero, as a YAML list without #.
