# Lessons from zotero-claude-bridge (2026-11-21)

Source: github.com/ChanglinWu/zotero-claude-bridge v0.5.0, read on 2026-11-21. Not installed: one day old, auto-updates with full privileges, no dry run or journal.

## Decisions

- The window selection is the only thing that needs code inside Zotero. It goes in gam (our plugin, our release path) as a read-only endpoint. The library stays under zotero-local-mcp (dry run, journal, vocabulary).
- Annotations come from the local API. Zotero 10.0.9 does not return them from `items/<attachment>/children`, so the tool reads `items?itemType=annotation` and filters by parentItem.
- Show in Zotero uses `open zotero://select/...`. No plugin code.
- export_markdown writes files outside the vault only, and overwrites only files that it wrote before (frontmatter `zotero-export: true`).
- Long calls: after 45 s a tool returns a job id; get_job gives the result. The task keeps running in the server.

## Tasks

1. gam 0.4.0: `GET /gam/selection` on Zotero's server. Refuses a request with an Origin header. Pure logic in content/lib/selection.mjs with node tests. Test in Zotero with a dev install.
2. zotero-local-mcp 0.9.0: get_annotations, get_selected_items (calls gam), show_in_zotero, export_markdown, get_job and the job wrapper on import_identifiers, attach_oa_pdfs, check_retractions, repair_metadata, export_markdown. Tests with the fake Zotero.
3. Register both MCP servers in Claude Code at user scope.
4. quarterdeck plugin: skill `zotero` that routes push, ask, cite, check, read, notes to the tools. Bump the version, update the marketplace.
