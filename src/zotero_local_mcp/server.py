"""MCP server entry point. Run with: zotero-local-mcp (stdio)."""

from __future__ import annotations

import functools
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field

from . import __version__
from .client import ZoteroError
from .config import Settings
from .external import External, ExternalError
from .library import Library
from .bakeoff import Bakeoff
from .manage import Librarian

INSTRUCTIONS = """\
Local Zotero library (Zotero 10 local API). Tags follow a controlled vocabulary
file with facets topic/, method/, type/, status/. Workflow rules:
- Every write tool defaults to dry_run=true. Show the preview to the user and
  call again with dry_run=false only after explicit approval.
- Only vocabulary tags can be added. To propose a new tag, ask the user to add
  it to the vocabulary file; never invent tags.
- Items tagged by the agent get the review marker tag (default _agent).
- Every applied write is journaled; use history and undo to revert.
- The first write opens a Zotero dialog; the user should choose 'Always Allow'.
- Imports, repairs and PDFs use Crossref, PubMed, Open Library and Unpaywall
  metadata. Keywords and MeSH headings are never imported as tags.
"""

READ = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True,
                        openWorldHint=False)

mcp = MCPServer("zotero", instructions=INSTRUCTIONS, version=__version__)
_lib: Library | None = None
_librarian: Librarian | None = None


def lib() -> Library:
    global _lib
    if _lib is None:
        _lib = Library(Settings.from_env())
    return _lib


def librarian() -> Librarian:
    global _librarian
    if _librarian is None:
        s = lib().s
        _librarian = Librarian(lib(), External(s.email, s.ncbi_api_key, s.openalex_api_key))
    return _librarian


class TagChange(BaseModel):
    # Unknown fields are refused, so a model that writes "topics" instead of "add" gets an error it can fix.
    model_config = ConfigDict(extra="forbid")
    key: str = Field(description="Zotero item key, e.g. ABCD2345")
    add: list[str] = Field(default_factory=list, description="Vocabulary tags to add")
    remove: list[str] = Field(default_factory=list, description="Tags to remove (any tag)")


def safe(fn):
    """Turn expected failures into tool errors whose message reaches the agent."""

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except (ZoteroError, ExternalError, LookupError, ValueError) as exc:
            raise ToolError(str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 - the agent must see what went wrong
            raise ToolError(f"Unexpected {type(exc).__name__}: {exc}") from exc

    return wrapper


# ---------------------------------------------------------------- read tools

@mcp.tool(annotations=READ)
@safe
async def status() -> dict:
    """Check the connection to Zotero, write authorization and the vocabulary file."""
    return await lib().status()


@mcp.tool(annotations=READ)
@safe
async def library_overview() -> dict:
    """Counts for the whole library: items by type, items missing each facet, items without
    tags, tags outside the vocabulary, items without citekeys, items awaiting review."""
    return await lib().overview()


@mcp.tool(annotations=READ)
@safe
async def find_items(
    query: str | None = None,
    fulltext: bool = False,
    collection: str | None = None,
    tags: list[str] | None = None,
    item_type: str | None = None,
    missing_facet: str | None = None,
    untagged: bool = False,
    outside_vocabulary: bool = False,
    detail: bool = False,
    limit: int = 50,
    offset: int = 0,
) -> dict:
    """Search top-level items (newest first). Filters combine.
    query: title/creator/year search (fulltext=true searches all fields and PDFs).
    collection: collection key. tags: items must have all of these tags. item_type: e.g. journalArticle, book.
    missing_facet: items with no tag of that facet, e.g. 'topic'.
    untagged: items with no manual tags. outside_vocabulary: items with tags not in the vocabulary.
    detail=true adds abstract, venue, DOI and automatic tags (use it when proposing tags).
    For missing_facet or untagged batches, keep offset=0: tagged items drop out of the results."""
    return await lib().find(query, fulltext, collection, tags, item_type, missing_facet,
                            untagged, outside_vocabulary, detail, limit, offset)


@mcp.tool(annotations=READ)
@safe
async def get_item(key: str) -> dict:
    """Full record of one item: fields, all tags, collections, child notes (as text),
    attachments and a zotero:// link."""
    return await lib().get_item(key)


@mcp.tool(annotations=READ)
@safe
async def get_fulltext(key: str, offset: int = 0, max_chars: int = 30000) -> dict:
    """Full text that Zotero indexed from the item's PDF (key of the item or the attachment).
    Long texts come in parts: call again with offset=next_offset until next_offset is null.
    Use it for literature notes and syntheses; it costs many tokens, so read only what is needed."""
    return await lib().get_fulltext(key, offset, max_chars)


@mcp.tool(annotations=READ)
@safe
async def list_tags(
    facet: str | None = None,
    outside_vocabulary: bool = False,
    include_automatic: bool = False,
    min_items: int = 1,
) -> dict:
    """Tags used on top-level items with item counts, most used first.
    facet: only tags like 'topic/...'. outside_vocabulary: only tags missing from the vocabulary.
    include_automatic: also list automatic tags (e.g. MeSH imported from PubMed)."""
    return await lib().list_tags(facet, outside_vocabulary, include_automatic, min_items)


@mcp.tool(annotations=READ)
@safe
async def get_vocabulary() -> dict:
    """The allowed tags grouped by facet, with descriptions, aliases, required facets and
    single-value facets. Read it before proposing tags."""
    return lib().get_vocabulary()


@mcp.tool(annotations=READ)
@safe
async def list_collections() -> list[dict]:
    """All collections with key, name, parent name and item count."""
    return await lib().list_collections()


@mcp.tool(annotations=READ)
@safe
async def history(limit: int = 10) -> list[dict]:
    """Recent applied writes from the journal (newest first), with ids for undo."""
    return await lib().history(limit)


# ---------------------------------------------------------------- write tools

@mcp.tool(annotations=WRITE)
@safe
async def tag_items(changes: list[TagChange], dry_run: bool = True) -> dict:
    """Add and remove tags on items. Added tags must be in the vocabulary. For single-value
    facets (e.g. status/) the new tag replaces the old one. Items that gain tags also get the
    review marker. dry_run=true (default) returns a preview and writes nothing."""
    return await lib().tag_items([c.model_dump() for c in changes], dry_run)


@mcp.tool(annotations=WRITE)
@safe
async def rename_tags(mapping: dict[str, str], dry_run: bool = True) -> dict:
    """Rename or merge tags on every item: {'old tag': 'topic/new', 'Old2': 'topic/new'}.
    Targets must be in the vocabulary. Sources can be manual or automatic tags."""
    return await lib().rename_tags(mapping, dry_run)


@mcp.tool(annotations=WRITE)
@safe
async def remove_tags(tags: list[str], dry_run: bool = True) -> dict:
    """Remove these tags from every item (e.g. the review marker after review). Journaled."""
    return await lib().remove_tags(tags, dry_run)


@mcp.tool(annotations=WRITE)
@safe
async def remove_automatic_tags(keys: list[str] | None = None, dry_run: bool = True) -> dict:
    """Remove automatic tags (MeSH and other imported keywords). keys=None means every item.
    Use them as hints for tagging first; after removal they are only in the journal."""
    return await lib().remove_automatic_tags(keys, dry_run)


@mcp.tool(annotations=WRITE)
@safe
async def set_citekeys(keys: list[str] | None = None, force: bool = False,
                       dry_run: bool = True) -> dict:
    """Give items citekeys like jacinto2026 (a, b, ... on collisions). keys=None means all
    items without a key. Existing keys are kept (pinned) unless force=true."""
    return await lib().set_citekeys(keys, force, dry_run)


@mcp.tool(annotations=WRITE)
@safe
async def update_fields(key: str, fields: dict[str, Any], dry_run: bool = True) -> dict:
    """Edit bibliographic fields of one item, e.g. {'title': ..., 'date': '2021', 'DOI': ...}.
    creators is a list of {creatorType, firstName, lastName} or {creatorType, name}.
    Tags, collections, trash and citekeys have their own tools."""
    return await lib().update_fields(key, fields, dry_run)


@mcp.tool(annotations=WRITE)
@safe
async def file_items(keys: list[str], collection_key: str, remove: bool = False,
                     dry_run: bool = True) -> dict:
    """Add items to a collection, or remove them from it with remove=true."""
    return await lib().file_items(keys, collection_key, remove, dry_run)


@mcp.tool(annotations=WRITE)
@safe
async def create_collection(name: str, parent_key: str | None = None,
                            dry_run: bool = True) -> dict:
    """Create a collection, optionally inside a parent collection."""
    return await lib().create_collection(name, parent_key, dry_run)


@mcp.tool(annotations=WRITE)
@safe
async def create_note(parent_key: str, markdown: str, dry_run: bool = True) -> dict:
    """Attach a child note (written in Markdown) to an item."""
    return await lib().create_note(parent_key, markdown, dry_run)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False))
@safe
async def trash_items(keys: list[str], dry_run: bool = True) -> dict:
    """Move items to the Zotero trash (never deletes permanently; undo restores them)."""
    return await lib().trash_items(keys, dry_run)


@mcp.tool(annotations=WRITE)
@safe
async def undo(journal_id: str | None = None, dry_run: bool = True) -> dict:
    """Revert a journaled write (default: the most recent one not yet undone). Items edited
    after that write are skipped and reported."""
    return await lib().undo(journal_id, dry_run)


# ---------------------------------------------------------------- librarian tools

@mcp.tool(annotations=WRITE)
@safe
async def import_identifiers(identifiers: list[str], collection_key: str | None = None,
                             tags: list[str] | None = None, dry_run: bool = True) -> dict:
    """Add items by DOI, PMID (pmid:123) or ISBN (isbn:978...). Metadata comes from Crossref,
    PubMed and Open Library. Items already in the library are skipped (matched by DOI, PMID,
    ISBN or title and year). New items get a citekey, the review marker and the given
    vocabulary tags, optionally a collection. No keywords or MeSH tags are imported."""
    return await librarian().import_identifiers(identifiers, collection_key, tags, dry_run)


@mcp.tool(annotations=WRITE)
@safe
async def import_queue(path: str | None = None, collection_key: str | None = None,
                       tags: list[str] | None = None, dry_run: bool = True) -> dict:
    """Import the ticked lines (- [x]) of the import queue note, or of another note such as a
    literature alert (path). Lines must contain doi:, pmid: or isbn:. After the import the
    lines are marked (imported: citekey)."""
    return await librarian().import_queue(path, collection_key, tags, dry_run)


@mcp.tool(annotations=READ)
@safe
async def audit_metadata(item_type: str | None = None, problem: str | None = None,
                         limit: int = 50, offset: int = 0) -> dict:
    """Find items with incomplete metadata: no DOI, abstract, journal, year, authors, ISBN,
    publisher; malformed DOI; title in capitals. problem filters by one problem name."""
    return await librarian().audit(item_type, problem, limit, offset)


@mcp.tool(annotations=WRITE)
@safe
async def repair_metadata(keys: list[str], overwrite: bool = False, min_confidence: float = 0.9,
                          dry_run: bool = True) -> dict:
    """Fill empty fields from Crossref/PubMed/Open Library. Items with a DOI, PMID or ISBN match
    exactly; others are matched by title search with a confidence score (0-1) and changed only
    at or above min_confidence. overwrite=true also replaces differing values (never title or
    authors). Returns matches with confidence for review."""
    return await librarian().repair(keys, overwrite, min_confidence, dry_run)


@mcp.tool(annotations=READ)
@safe
async def find_duplicates() -> dict:
    """Groups of likely duplicate items (same DOI, PMID, ISBN, or title and year).
    Merging is done by the user in Zotero's Duplicate Items view."""
    return await librarian().duplicates()


@mcp.tool(annotations=READ)
@safe
async def check_retractions(keys: list[str] | None = None, refresh: bool = False,
                            limit: int = 200) -> dict:
    """Check items with a DOI for retractions, expressions of concern and corrections
    (Crossref, including Retraction Watch data). Results are cached for 30 days; call again
    while still_unchecked > 0."""
    return await librarian().retractions(keys, refresh, limit)


@mcp.tool(annotations=READ)
@safe
async def missing_pdfs(item_type: str | None = None, with_doi_only: bool = False,
                       limit: int = 50, offset: int = 0) -> dict:
    """Items without a PDF attachment, with counts by item type."""
    return await librarian().missing_pdfs(item_type, with_doi_only, limit, offset)


@mcp.tool(annotations=WRITE)
@safe
async def attach_oa_pdfs(keys: list[str], dry_run: bool = True) -> dict:
    """Find legal open-access PDFs through Unpaywall and attach them (up to 25 items per call).
    The dry run shows source, version (published or accepted manuscript) and licence."""
    return await librarian().attach_oa_pdfs(keys, dry_run)


# ---------------------------------------------------------------- model test

class Proposal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str
    tags: list[str] = Field(min_length=1, description="All proposed tags for this item, e.g. [\"topic/asthma\", \"type/cohort\"]")


@mcp.tool(annotations=READ)
@safe
async def bakeoff_items() -> dict:
    """Model test: the fixed sample of items (with abstracts) and the tag vocabulary."""
    return await Bakeoff(lib()).items()


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False))
@safe
async def bakeoff_submit(label: str, proposals: list[Proposal]) -> dict:
    """Model test: save proposed tags for the sample (a local file, not the library)."""
    return Bakeoff(lib()).submit(label, [p.model_dump() for p in proposals])


def main() -> None:
    import asyncio
    import json
    import sys

    if "--check" in sys.argv[1:]:
        # Connection check for the terminal: zotero-local-mcp --check
        print(json.dumps(asyncio.run(lib().status()), indent=2, ensure_ascii=False))
        return
    mcp.run("stdio")


if __name__ == "__main__":
    main()
