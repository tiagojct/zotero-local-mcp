Two AI agents in OpenCode that work on the Zotero library on [[ahab]]: a librarian that changes the library and a researcher that searches the literature and writes in the vault. They use the Zotero 10 local API (no zotero.org key; WebDAV file sync is not affected), plus PubMed, OpenAlex, Crossref, Unpaywall and Open Library.

- Code: ~/Projects/zotero-local-mcp (git, see README.md)
- Shared rules: [[Zotero agent]]. Role prompts: [[Zotero librarian]], [[Zotero researcher]]
- Tag vocabulary: [[Zotero tags]]. Saved searches for alerts: [[Literature alerts]]
- Settings (email, API keys, paths): ~/.config/opencode/zotero.env
- OpenCode config: ~/.config/opencode/opencode.jsonc (model, servers, agents, permissions). In the vault: Notes/.opencode/opencode.jsonc
- Journal of all library changes: ~/.local/share/zotero-local-mcp/journal/
- Models (OpenCode Go): librarian glm-5.3-flash, researcher mimo-v2.6-pro; deepseek-v4-pro for the hardest syntheses. Change with /models. OpenRouter is the fallback. Do not use Grok, GPT Luna or Muse Spark (data retention or training).

## Roles

| | Librarian | Researcher |
|---|---|---|
| Changes the Zotero library | yes, after approval | no |
| Web and outside search | no (only metadata inside its tools) | yes |
| Writes notes in the vault | review files in Inbox | literature notes, syntheses, import queue |

Hand-off: the researcher adds works to Inbox/Zotero import queue.md. Tick the lines you want. Ask the librarian to import them.

## First setup

1. Quit Zotero.
2. Copy the folder ~/Zotero to a backup location.
3. Start Zotero.
4. In Zotero, open Settings > Advanced. Select "Allow other applications on this computer to communicate with Zotero".
5. Open ~/.config/opencode/zotero.env. Make sure that ZOTERO_CONTACT_EMAIL is you@example.org. If you have an NCBI API key, type it after NCBI_API_KEY=.
6. Open a terminal. Type `cd ~/Projects/zotero-local-mcp && uv sync && uv run pytest -q`. Make sure that all tests pass.
7. Type `ZOTERO_MCP_ENV=~/.config/opencode/zotero.env uv run zotero-local-mcp --check`. Make sure that the result shows `"zotero": "reachable"` and no vocabulary problems.
8. Type `opencode auth login`. Make sure that OpenCode Go is signed in. OpenRouter is optional (fallback).
9. If you use OpenRouter: open Settings > Privacy and turn on zero data retention.

## Weekly alerts (optional)

1. Edit the searches in [[Literature alerts]].
2. In the terminal, type `ZOTERO_MCP_ENV=~/.config/opencode/zotero.env uv run zotero-alerts` in ~/Projects/zotero-local-mcp. Make sure that a note appears in Inbox/.
3. Type `cp ~/Projects/zotero-local-mcp/examples/launchd/local.zotero-alerts.plist ~/Library/LaunchAgents/`.
4. Type `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/local.zotero-alerts.plist`.
5. The script runs every Monday at 07:30. The log is ~/Library/Logs/zotero-alerts.log.

To stop the alerts, type `launchctl bootout gui/$(id -u)/local.zotero-alerts`.

## First test

1. Start Zotero.
2. Type `cd ~/Notes && opencode`. OpenCode starts with the researcher. Press Tab to switch between the researcher and the librarian.
3. Librarian: ask "Run status and library_overview."
4. Librarian: ask it to add topic/spirometry to 3 items. Approve the preview. When Zotero shows the authorization dialog, click Always Allow.
5. In Zotero, make sure that the 3 items have topic/spirometry and _agent. Then ask "Undo the last change."
6. Make a test item in Zotero. Ask the librarian to move it to the trash, then to undo. Make sure that the item comes back.
7. Librarian: ask it to import one DOI. Make sure that the new item has an abstract, a citekey in Extra (or in the Citation Key field) and no MeSH tags.
8. Librarian: ask it to attach an open-access PDF to that item. Open the PDF in Zotero.
9. Researcher: ask "What do I have on FeNO?" and "Search PubMed for recent FeNO studies in children and tell me what I am missing."

## Model test

Use this test to choose the tagging model. It uses the same 25 items for every model and compares the proposals with your own tags. Nothing is written to the Zotero library.

1. Start Zotero.
2. In a terminal, type `cd ~/Projects/zotero-local-mcp`.
3. Type `ZOTERO_MCP_ENV=~/.config/opencode/zotero.env uv run zotero-bakeoff sample`. The script writes Inbox/Model test reference.md.
4. Open Inbox/Model test reference.md. In the last column, type the topic/, method/ and type/ tags that you would give, separated by commas. Do this before you run the models.
5. Type `ZOTERO_MCP_ENV=~/.config/opencode/zotero.env uv run zotero-bakeoff run --model opencode-go/glm-5.3-flash --model opencode-go/mimo-v2.6-pro`. Each model runs once with the librarian agent. This takes some minutes.
6. Type `ZOTERO_MCP_ENV=~/.config/opencode/zotero.env uv run zotero-bakeoff score`. The script writes Inbox/Model test results.md.
7. Read the results. "Edits" is the number of tags that you would have to add or remove. Compare it with the cost and the time.

To test another model, repeat step 5 with that model and then step 6. The results note shows all tested models. To make a new sample, type `... zotero-bakeoff sample --seed 2 --force`.

## Requests

Librarian:
- "Give me a library overview."
- "Clean the existing tags." Edit Inbox/Zotero tag mapping.md, then say "Apply the mapping."
- "Tag the next batch of untagged items." Edit Inbox/Zotero tag review NN.md, then say "Apply batch NN."
- "Set citekeys for all items without one."
- "Import 10.1183/13993003.00001-2026 and pmid:39000001."
- "Import the ticked items from the queue." or "... from Inbox/Literature alerts 2026-09-28.md."
- "Audit the metadata. Start with journal articles without a DOI." Then "Repair these."
- "Find duplicates." Merge them in Zotero (Duplicate Items).
- "Check the library for retractions."
- "Which items have no PDF? Attach the open-access ones."
- "Show the history." and "Undo the last change."

Researcher:
- "What do I have on spirometry in older adults?"
- "Search PubMed and OpenAlex for LLM-based clinical decision support since 2024. What am I missing?" Then "Queue the first five."
- "Show the citation graph of jacinto2026."
- "Make a literature note for jacinto2026." The note goes to Resources/Zotero/jacinto2026.md. Then "Link it in Zotero": a short Zotero note with an Obsidian link.
- "Write a synthesis of my references on FeNO in children." The note goes to Workshop/.
- "Check the citations in ~/Papers/draft.qmd and write the bibliography."
- "Summarise this week's literature alert."

## Review

- Items that the librarian changed or imported have the tag _agent. In Zotero, click _agent in the tag selector to see them.
- When the review is done, ask the librarian: "Remove the _agent tag from all items."
- Automatic tags (MeSH) stay until the library is tagged. Then ask the librarian: "Remove all automatic tags."

## Limits

- The agents read the text that Zotero indexed. If a PDF is only on the WebDAV server, open it once in Zotero, then right-click the attachment and select Reindex Item.
- Titles, abstracts and full texts go to the model provider through OpenRouter.
- The librarian asks before each write. The researcher asks before it edits a file or writes a bibliography.
- Duplicates are merged by you in Zotero; the librarian only finds them.
