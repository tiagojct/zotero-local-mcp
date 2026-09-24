Local MCP server that lets an AI agent (OpenCode, Claude Code) read and edit the Zotero library on [[ahab]]. It uses the Zotero 10 local API only: no zotero.org key, and WebDAV file sync is not affected.

- Code: ~/Projects/zotero-local-mcp (git, see README.md)
- Agent rules: [[Zotero agent]]
- Tag vocabulary: [[Zotero tags]]
- OpenCode, global config: ~/.config/opencode/opencode.jsonc (model, Zotero server, permissions). Backup of the old file: opencode.jsonc.bak-2026-09-24
- OpenCode, vault config: Notes/.opencode/opencode.jsonc (loads .claude/CLAUDE.md and [[Zotero agent]])
- Journal of all changes: ~/.local/share/zotero-local-mcp/journal/
- Model: OpenRouter, z-ai/glm-5.3-flash. Change it with /models in OpenCode.

## First setup

1. Quit Zotero.
2. Copy the folder ~/Zotero to a backup location.
3. Start Zotero.
4. In Zotero, open Settings > Advanced. Select "Allow other applications on this computer to communicate with Zotero".
5. Open a terminal. Type `cd ~/Projects/zotero-local-mcp && uv sync && uv run pytest -q`. Make sure that all tests pass.
6. Type `ZOTERO_VOCAB="$HOME/Notes/Systems/Zotero tags.md" uv run zotero-local-mcp --check`. Make sure that the result shows `"zotero": "reachable"` and no vocabulary problems.
7. Type `opencode auth login`. Select OpenRouter. Paste the OpenRouter key.
8. In OpenRouter, open Settings > Privacy. Turn on zero data retention.
9. Delete ~/Projects/zotero-local-mcp.tar.gz.

## First test

1. Start Zotero.
2. Type `cd ~/Notes && opencode`.
3. Ask: "Run zotero status and library_overview."
4. Ask the agent to add topic/spirometry to 3 items. Approve the preview.
5. When Zotero shows the authorization dialog, click Always Allow.
6. In Zotero, look at the 3 items. Make sure that they have topic/spirometry and _agent.
7. Ask: "Undo the last change." Make sure that the tags are gone.
8. Make a test item in Zotero. Ask the agent to move it to the trash, then to undo. Make sure that the item comes back.
9. Ask the agent to set the citekey of one item. Make sure that the key is in the Citation Key field or in Extra.

## Daily use

Start Zotero before OpenCode. Start OpenCode in the vault (`cd ~/Notes && opencode`), so that it loads the vault rules.

Examples of requests:

- "Give me a library overview."
- "Clean the existing tags." The agent writes Inbox/Zotero tag mapping.md. Edit it, then say "Apply the mapping."
- "Tag the next batch of untagged items." The agent writes Inbox/Zotero tag review NN.md. Edit the proposed tags, then say "Apply batch NN."
- "Set citekeys for all items without one."
- "Make a literature note for jacinto2026." The note goes to Resources/Zotero/jacinto2026.md.
- "Make literature notes for all items tagged topic/feno and status/read." The agent asks first when there are more than 10.
- "What do I have on FeNO in children?" The agent answers in the chat with citekeys.
- "Write a synthesis of my references on spirometry reference equations in older adults." The agent shows the list first, you choose, and the note goes to Workshop/ with [@citekey] citations.
- "Show the history" and "Undo the last change."

## Review

- Items tagged by the agent have the tag _agent. In Zotero, click _agent in the tag selector to see them.
- When the review is done, ask: "Remove the _agent tag from all items."
- Automatic tags (MeSH) stay until the library is tagged. Then ask: "Remove all automatic tags."

## Limits

- The agent reads the text that Zotero indexed. If a PDF is only on the WebDAV server, open it once in Zotero, then right-click the attachment and select Reindex Item.
- Titles, abstracts and full texts go to the model provider through OpenRouter.
- Write tools ask for approval in OpenCode. Read tools do not.
