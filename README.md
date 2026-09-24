# zotero-local-mcp

MCP server for a local Zotero 10 library. It lets an agent (OpenCode, Claude Code) search the library, tag items with a controlled vocabulary, set citekeys, edit fields, file items in collections, add notes and move items to the trash.

It talks only to Zotero's local API on `127.0.0.1:23119`. It needs no zotero.org API key, and it does not touch the network. Changes appear in Zotero at once and sync as normal edits, so WebDAV file sync is not affected.

## Safety

- Every write tool defaults to `dry_run=true` and returns a preview.
- Added tags must be in the vocabulary file. Tags that start with `_` are system tags.
- Each item write sends the item version. If you edit the item in Zotero at the same time, the server reads it again and re-applies its change; your edit is kept.
- Every applied write goes to a journal (`~/.local/share/zotero-local-mcp/journal/`). `undo` reverts it and skips items edited since.
- The server never sends DELETE requests. Tags are removed from items; items go to the trash.
- Items that the agent tags get the marker tag `_agent`, so you can review them in Zotero.

## Tools

| Read | Write (dry_run by default) |
|---|---|
| status | tag_items |
| library_overview | rename_tags |
| find_items | remove_tags |
| get_item, get_fulltext | remove_automatic_tags |
| list_tags | set_citekeys |
| get_vocabulary | update_fields |
| list_collections | file_items, create_collection |
| history | create_note, trash_items, undo |

## Requirements

- Zotero 10 or later, running.
- uv (`brew install uv`).
- Zotero > Settings > Advanced: select "Allow other applications on this computer to communicate with Zotero".

## Install

1. Quit Zotero. Copy the folder `~/Zotero` to a backup location. Start Zotero again.
2. Open a terminal. Run:
   ```sh
   cd ~/Projects/zotero-local-mcp
   uv sync
   uv run pytest -q
   ```
   All tests must pass.
3. Check the connection:
   ```sh
   ZOTERO_VOCAB="$HOME/Notes/Systems/Zotero tags.md" uv run zotero-local-mcp --check
   ```
   The result must show `"zotero": "reachable"` and the vocabulary with no problems.
4. Add the server to OpenCode. See `examples/opencode-global.jsonc`.
5. Add the OpenRouter key: `opencode auth login`, then select OpenRouter.
6. In OpenRouter > Settings > Privacy, turn on zero data retention. Item titles and abstracts go to the model provider.

## Configuration

| Variable | Default | Use |
|---|---|---|
| `ZOTERO_VOCAB` | none | Path to the vocabulary Markdown file. Tag writes are blocked without it. |
| `ZOTERO_API_URL` | `http://127.0.0.1:23119/api` | Local API address. |
| `ZOTERO_AGENT_MARKER` | `_agent` | Review marker tag. Empty string turns it off. |
| `ZOTERO_MCP_STATE` | `~/.local/share/zotero-local-mcp` | Journal and saved write key. |
| `ZOTERO_AUTH_TIMEOUT` | `300` | Seconds to wait for the Zotero authorization dialog. |

## Vocabulary file

A Markdown file. The frontmatter sets the required facets and the facets that allow one tag only. Each tag is the first backticked token of a bullet:

```markdown
---
required_facets: topic, status
single_facets: status
---
## topic
- `topic/spirometry` Spirometry. aliases: Spirometry, FEV1
```

The file is read again when it changes, so edits in Obsidian apply at once.

## Write authorization

The first write opens a Zotero dialog. Choose "Always Allow". The key is saved in the state folder (permissions 600). With "Allow", the key is single-use and each batch of 50 items shows the dialog again (Zotero allows 5 dialogs per minute). To revoke all keys: Zotero > Settings > Advanced > Clear Write Authorizations.

## Citekeys

`set_citekeys` writes keys like `jacinto2026`, `jacinto2026a`. Older items (by date added) get the plain key. The key goes to the native citationKey field if the item has one, otherwise to a `Citation Key:` line in Extra. Better BibTeX reads both. Existing keys are kept unless `force=true`.

## Not yet checked against a real Zotero 10

The tests use a simulated local API. Check these on first use with a test item:

- `trash_items` sets `deleted: true`, and `undo` sets it back to false.
- Whether items expose the native `citationKey` field.

## Development

```sh
uv sync
uv run pytest -q
```

`tests/fake_zotero.py` imitates the Zotero 10 local API (Server ID, authorization, versions, 50-object batches).
