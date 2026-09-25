"""Task set for the Sub-Sub model test (subsub-bench). Read-only.

    zotero-bench prepare --out DIR [--seed 7] [--n 20]

Writes to DIR:
    tasks.json          the tasks, the same for every model
    library-index.json  key, citekey, DOI, PMID, title, year of every item (for the checks)
    reference.md        the tagging items that have no reviewed tags, without their
                        current tags, for a blind reference
Also saves the tagging sample where bakeoff_items reads it.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import random
import sys
from collections import Counter
from pathlib import Path

from .citekey import current_key, year_of
from .config import Settings
from .external import External, ExternalError
from .library import Library, label, manual
from .records import LibraryIndex, item_ids
from .vocab import facet_of

AGENT_MARK = "_agent"

# Well-known papers; prepare keeps those that resolve and are not in the library.
NEW_DOIS = [
    "10.1038/s41586-023-06291-2",   # Singhal 2023, LLMs encode clinical knowledge
    "10.1038/s41591-018-0300-7",    # Topol 2019, high-performance medicine
    "10.1164/rccm.202205-0963OC",   # Bowerman 2023, race-neutral GLI
    "10.1183/09031936.00080312",    # Quanjer 2012, GLI-2012
    "10.1183/13993003.00289-2020",  # Hall 2021, GLI static lung volumes
]
NEW_PMIDS = ["30617339", "31613151", "16055882"]


def scored_tags(tags: list[str]) -> list[str]:
    return sorted(t for t in tags if facet_of(t) in ("topic", "method", "type"))


async def prepare(lib: Library, ext: External, out: Path, seed: int = 7, n: int = 20) -> dict:
    rng = random.Random(seed)
    vocab = lib.vocab.require()
    items = await lib.regular_items()
    items.sort(key=lambda i: i["key"])
    index = LibraryIndex(items)
    out.mkdir(parents=True, exist_ok=True)

    # ---- library index for the checks
    rows = []
    for i in items:
        d = i["data"]
        ids = item_ids(d)
        rows.append({"key": d["key"], "citekey": current_key(d), "doi": ids["doi"], "pmid": ids["pmid"],
                     "title": d.get("title", ""), "year": year_of(d), "type": d.get("itemType")})
    (out / "library-index.json").write_text(json.dumps(rows, ensure_ascii=False, indent=0))

    # ---- tagging: reviewed items (no _agent) carry Tiago's tags as the reference
    def has_topic(i: dict) -> bool:
        return any(facet_of(t) == "topic" for t in manual(i["data"]))

    reviewed = [i for i in items if has_topic(i) and AGENT_MARK not in manual(i["data"])]
    agent = [i for i in items if AGENT_MARK in manual(i["data"]) and (i["data"].get("abstractNote") or "").strip()]
    rng.shuffle(reviewed)
    rng.shuffle(agent)
    pick_reviewed = reviewed[: n // 2]
    pick_agent = agent[: n - len(pick_reviewed)]
    chosen = sorted(pick_reviewed + pick_agent, key=lambda i: label(i["data"]).lower())
    sample = {"created": dt.date.today().isoformat(), "seed": seed, "keys": [i["key"] for i in chosen]}
    bdir = lib.s.state_dir / "bakeoff"
    bdir.mkdir(parents=True, exist_ok=True)
    (bdir / "sample.json").write_text(json.dumps(sample, indent=1))
    gold = {i["key"]: scored_tags(manual(i["data"])) for i in pick_reviewed}

    ref_lines = [
        "Blind reference for the tagging test. Current tags are not shown on purpose.",
        "For each item, give topic/, method/ and type/ tags from the vocabulary.", "",
    ]
    for i in sorted(pick_agent, key=lambda i: label(i["data"]).lower()):
        d = i["data"]
        ref_lines += [
            f"## {d['key']}",
            f"- {d.get('itemType')} | {year_of(d)} | {d.get('publicationTitle') or d.get('bookTitle') or d.get('publisher') or ''}",
            f"- Title: {d.get('title', '')}",
            f"- Abstract: {(d.get('abstractNote') or '').strip()}",
            "- Reference tags: ", "",
        ]
    (out / "reference.md").write_text("\n".join(ref_lines), encoding="utf-8")

    # ---- facet fix: the items the librarian should find
    required = vocab.required_facets or ["topic", "status"]
    facet_fix = {f"missing_{f}": sorted(i["key"] for i in items
                                        if not any(facet_of(t) == f for t in manual(i["data"])))
                 for f in required}

    # ---- literature note: a journal article with indexed full text
    articles = [i for i in items if i["data"].get("itemType") == "journalArticle" and current_key(i["data"])]
    rng.shuffle(articles)
    lit = None
    for i in articles[:40]:
        try:
            ft = await lib.get_fulltext(i["key"], max_chars=1000)
        except Exception:  # noqa: BLE001 - no full text for this one
            continue
        if 8000 <= ft["total_chars"] <= 250000:
            d = i["data"]
            lit = {"key": d["key"], "citekey": current_key(d), "title": d.get("title", ""),
                   "chars": ft["total_chars"]}
            break

    # ---- synthesis and search: a topic with 6 to 15 items
    counts = Counter(t for i in items for t in manual(i["data"]) if facet_of(t) == "topic")
    topics = sorted(t for t, c in counts.items() if 6 <= c <= 15)
    topic = rng.choice(topics) if topics else (counts.most_common(1)[0][0] if counts else None)
    synth = None
    if topic:
        members = [i for i in items if topic in manual(i["data"])]
        entry = vocab.entries.get(topic)
        synth = {"topic": topic, "description": entry.description if entry else "",
                 "keys": [i["key"] for i in members],
                 "citekeys": [current_key(i["data"]) for i in members]}

    # ---- import: one identifier already in the library, one new DOI, one new PMID
    with_doi = [i for i in items if item_ids(i["data"])["doi"] and current_key(i["data"])]
    have = rng.choice(with_doi) if with_doi else None
    in_library = ({"id": item_ids(have["data"])["doi"], "key": have["key"], "citekey": current_key(have["data"])}
                  if have else None)
    new: list[dict] = []
    problems: list[str] = []
    for doi in NEW_DOIS:
        if index.lookup(doi=doi):
            continue
        try:
            rec = await ext.crossref_work(doi)
        except ExternalError as exc:
            problems.append(f"{doi}: {exc}")
            continue
        if rec:
            new.append({"id": doi, "kind": "doi", "title": rec.get("title", "")})
            break
    for pmid in NEW_PMIDS:
        if index.lookup(pmid=pmid):
            continue
        try:
            recs = await ext.pubmed_fetch([pmid])
        except ExternalError as exc:
            problems.append(f"PMID {pmid}: {exc}")
            continue
        if recs and not index.match_record(recs[0]):
            new.append({"id": f"PMID:{pmid}", "kind": "pmid", "title": recs[0].get("title", "")})
            break

    tasks = {
        "created": dt.datetime.now().isoformat(timespec="seconds"),
        "seed": seed,
        "tagging": {"keys": sample["keys"], "gold_reviewed": gold,
                    "blind_reference_keys": [i["key"] for i in pick_agent]},
        "facet_fix": facet_fix,
        "import": {"in_library": in_library, "new": new},
        "lit_note": lit,
        "synthesis": synth,
        "search": {"topic": topic, "description": synth["description"] if synth else "",
                   "year_from": dt.date.today().year - 2},
        "problems": problems,
    }
    (out / "tasks.json").write_text(json.dumps(tasks, ensure_ascii=False, indent=1))
    return {
        "out": str(out),
        "tagging_items": len(sample["keys"]),
        "reviewed_reference": len(gold),
        "facet_fix": {k: len(v) for k, v in facet_fix.items()},
        "lit_note": lit and lit["citekey"],
        "synthesis_topic": topic,
        "import_ids": [x["id"] for x in ([in_library] if in_library else []) + new],
        "problems": problems,
    }


def main() -> None:
    ap = argparse.ArgumentParser(prog="zotero-bench", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("prepare")
    p.add_argument("--out", required=True)
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--n", type=int, default=20)
    args = ap.parse_args()
    s = Settings.from_env()

    async def go() -> dict:
        ext = External(s.email, s.ncbi_api_key, s.openalex_api_key)
        try:
            return await prepare(Library(s), ext, Path(args.out).expanduser(), args.seed, args.n)
        finally:
            await ext.aclose()

    print(json.dumps(asyncio.run(go()), indent=2, ensure_ascii=False))
    sys.exit(0)


if __name__ == "__main__":
    main()
