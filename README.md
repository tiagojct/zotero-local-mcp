# zotero-local-mcp

Two MCP servers for a local Zotero 10 library, plus a weekly alert script:

- `zotero-local-mcp` (librarian): search and edit the library, faceted tagging with a controlled vocabulary, citekeys, imports by DOI/PMID/ISBN, metadata audit and repair, duplicates, retraction check, open-access PDFs, notes, collections, undo.
- `zotero-scholar-mcp` (researcher): PubMed and OpenAlex search with "already in library" flags, citation graph, manuscript citation check, CSL JSON bibliographies for Quarto, import queue. It cannot write to Zotero.
- `zotero-alerts`: runs saved searches from an Obsidian note and writes new works to an Inbox note. No AI model.
- `zotero-review`: preview or apply several tag review notes from the terminal (`zotero-review apply "Inbox/Zotero tag review *.md"`). Asks before it writes; notes already applied are left out.
- `zotero-bakeoff`: model test for tagging. `sample` fixes 25 items and writes a reference note for your own tags; `run --model X` runs each model through `opencode run` (tools `bakeoff_items`, `bakeoff_submit`, no library writes); `score` writes precision, recall, edits needed, tokens and cost per model.

The library is reached only through Zotero's local API on `127.0.0.1:23119`: no zotero.org key, changes appear in Zotero at once and sync as normal edits (WebDAV file sync is not affected). Outside metadata comes from Crossref, PubMed (NCBI E-utilities), OpenAlex, Unpaywall and Open Library.

## Safety

- Every write tool defaults to `dry_run=true` and returns a preview.
- Added tags must be in the vocabulary file. Tags that start with `_` are system tags. Imports never add keywords or MeSH headings as tags.
- Each item write sends the item version; concurrent edits in Zotero are kept.
- Every applied write goes to a journal (`~/.local/share/zotero-local-mcp/journal/`). `undo` reverts it, skipping items edited since. Imports and attached PDFs are undone by moving them to the trash.
- No DELETE requests. Items go to the trash.
- The researcher's library client refuses every write except creating a new child note (attach_note: a short summary plus an obsidian:// link, journaled, undone by the librarian). Works it proposes go to an import queue note that a person ticks; the librarian imports the ticked lines. Queue labels are sanitised so they cannot create or tick lines.
- Duplicate detection by title needs the same year and first author, and no conflicting DOI/PMID. find_duplicates also finds titles cut off during import, shows the evidence for each item, and says when the item types differ (Zotero's Duplicate Items view shows only items of the same type).
- Tag limits per facet come from the vocabulary (`max_per_facet: topic=4, type=2`); tag_items skips a change that would go over. `replace` makes the given tags the complete set for those facets. write_tag_review writes a review note (a table of proposed tags, checked against the vocabulary, never overwriting a note); apply_tag_review applies the edited note exactly, removes the review marker, lists items whose tags changed after the note was written, and refuses a note that was already applied unless `again=true`.

## Tools

| Librarian: read | Librarian: write (dry run by default) | Researcher |
|---|---|---|
| status, library_overview | tag_items, rename_tags, remove_tags | search_pubmed, search_openalex |
| find_items, get_item, get_fulltext | remove_automatic_tags, set_citekeys | get_work, citation_graph |
| list_tags, get_vocabulary | update_fields, file_items, create_collection | library_lookup |
| list_collections, history | create_note, trash_items, undo | check_manuscript |
| audit_metadata, find_duplicates | import_identifiers, import_queue | export_bibliography |
| check_retractions, missing_pdfs | repair_metadata, attach_oa_pdfs | queue_imports, attach_note |
| tag_audit | write_tag_review, apply_tag_review | |

## Requirements

- Zotero 10 or later, running, with Settings > Advanced > "Allow other applications on this computer to communicate with Zotero".
- uv (`brew install uv`).

## Install

1. Quit Zotero. Copy `~/Zotero` to a backup location. Start Zotero.
2. Copy `examples/zotero.env` to `~/.config/opencode/zotero.env`. Fill in `ZOTERO_CONTACT_EMAIL` (required for Unpaywall).
3. Run `uv sync && uv run pytest -q`. All tests must pass.
4. Run `ZOTERO_MCP_ENV=~/.config/opencode/zotero.env uv run zotero-local-mcp --check`. The result must show `"zotero": "reachable"`.
5. Add the servers and the two agents to OpenCode: see `examples/opencode-global.jsonc`.
6. Optional: weekly alerts with launchd, see `examples/launchd/`.

## Configuration

Settings come from environment variables or from the file named by `ZOTERO_MCP_ENV` (default `~/.config/zotero-local-mcp/env`), one `KEY=VALUE` per line.

| Variable | Use |
|---|---|
| `ZOTERO_VOCAB` | Vocabulary Markdown file. Tag writes are blocked without it. |
| `ZOTERO_VAULT` | Obsidian vault: import queue (`Inbox/Zotero import queue.md`) and alert notes. |
| `ZOTERO_ALERTS` | Saved searches (default `<vault>/Systems/Literature alerts.md`). |
| `ZOTERO_CONTACT_EMAIL` | Required by Unpaywall; sent to Crossref, OpenAlex and NCBI for polite use. |
| `NCBI_API_KEY`, `OPENALEX_API_KEY` | Optional higher rate limits. |
| `ZOTERO_API_URL` | Default `http://127.0.0.1:23119/api`. |
| `ZOTERO_AGENT_MARKER` | Review marker tag, default `_agent`. |
| `ZOTERO_MCP_STATE` | Journal, write key, caches. Default `~/.local/share/zotero-local-mcp`. |

## Vocabulary file

```markdown
---
required_facets: topic, status
single_facets: status
---
## topic
- `topic/spirometry` Spirometry. aliases: Spirometry, FEV1
```

## Alerts file

```markdown
---
days: 7
max_per_query: 20
---
## pubmed
- `FeNO[tiab] AND asthma[tiab]` FeNO in asthma
## openalex
- `health data literacy` Health data literacy
```

## Write authorization

The first write opens a Zotero dialog. Choose "Always Allow"; the key is saved in the state folder (permissions 600). With "Allow", each batch shows the dialog again. To revoke: Zotero > Settings > Advanced > Clear Write Authorizations.

## Citekeys

`jacinto2026`, `jacinto2026a`. Stored in the native citationKey field when the item has one, otherwise as `Citation Key:` in Extra. Better BibTeX reads both.

## Not yet checked against a real Zotero 10

The tests use simulated services. Check on first use: trash and undo, the citekey location, one import, one PDF attachment (local file upload).

## Development

```sh
uv sync
uv run pytest -q
```

`tests/fake_zotero.py` imitates the Zotero 10 local API; `tests/fake_external.py` gives canned Crossref, PubMed, OpenAlex, Unpaywall and Open Library responses.
