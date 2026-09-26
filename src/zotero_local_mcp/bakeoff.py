"""Model test for tagging: the same sample of items, tagged by several models,
scored against your own tags (the reference).

    zotero-bakeoff sample [--n 25] [--seed 1] [--force]
        Picks items without a topic/ tag, saves the sample, writes
        Inbox/Model test reference.md for you to fill in blind.
    zotero-bakeoff run --model opencode-go/glm-5.3-flash --model opencode-go/mimo-v2.6-pro
        Runs each model once through `opencode run` with the librarian agent. The
        model reads the sample (bakeoff_items) and saves proposals (bakeoff_submit).
        Nothing is written to the Zotero library.
    zotero-bakeoff score
        Compares every model's proposals with the reference and writes
        Inbox/Model test results.md.

Only topic/, method/ and type/ tags are scored. status/ is your call.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import random
import re
import subprocess
import sys
import time
from pathlib import Path

from .citekey import current_key
from .config import Settings
from .library import Library, label, manual
from .vocab import facet_of

SCORED = ("topic", "method", "type")
REF_NOTE = "Model test reference.md"
RESULTS_NOTE = "Model test results.md"
PROMPT = (
    "Model test (no changes to the library). Call bakeoff_items to get the items and the tag "
    "vocabulary. For every item, propose topic/, method/ and type/ tags following the tag rules "
    "(1 to 4 topic tags, method and type only when they apply, vocabulary tags only). Do not "
    "propose status/ tags. Then call bakeoff_submit once with label \"{label}\" and all proposals. "
    "Do not call any other write tool and do not write files."
)


def safe_label(model: str) -> str:
    return re.sub(r"[^A-Za-z0-9.\-]+", "-", model.split("/")[-1]).strip("-")


class Bakeoff:
    def __init__(self, lib: Library) -> None:
        self.lib = lib
        self.s = lib.s
        self.dir = self.s.state_dir / "bakeoff"

    @property
    def inbox(self) -> Path:
        if self.s.vault is None:
            raise SystemExit("Set ZOTERO_VAULT (in the settings file) first.")
        return self.s.vault / "Inbox"

    def _sample(self) -> dict:
        path = self.dir / "sample.json"
        if not path.exists():
            raise LookupError("No model test sample. Run: zotero-bakeoff sample")
        return json.loads(path.read_text(encoding="utf-8"))

    # ------------------------------------------------------------ sample

    async def make_sample(self, n: int = 25, seed: int = 1, force: bool = False) -> dict:
        ref = self.inbox / REF_NOTE
        if ref.exists() and not force and any(parse_reference(ref.read_text(encoding="utf-8")).values()):
            raise SystemExit(f"{ref} already has your tags. Use --force to replace the sample.")
        items = await self.lib.regular_items()
        pool = [i for i in items if not any(facet_of(t) == "topic" for t in manual(i["data"]))]
        if len(pool) < n:
            pool = items
        pool.sort(key=lambda i: i["key"])
        chosen = random.Random(seed).sample(pool, min(n, len(pool)))
        chosen.sort(key=lambda i: label(i["data"]).lower())
        sample = {"created": dt.date.today().isoformat(), "seed": seed,
                  "keys": [i["key"] for i in chosen]}
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "sample.json").write_text(json.dumps(sample, indent=1), encoding="utf-8")
        for old in self.dir.glob("proposals-*.json"):
            old.rename(old.with_suffix(".old"))
        rows = "\n".join(f"| {i['key']} | {current_key(i['data']) or ''} | {label(i['data']).replace('|', '/')} |  |"
                         for i in chosen)
        self.inbox.mkdir(parents=True, exist_ok=True)
        ref.write_text(
            f"---\ncreated: {sample['created']}\n---\n"
            "Reference tags for the model test (see [[Zotero MCP]]). Fill the last column with the topic/, "
            "method/ and type/ tags you would give, separated by commas. Use only tags from [[Zotero tags]]. "
            "Do this before you look at the model files, so the test stays fair. Leave a row empty to skip it.\n\n"
            "| key | citekey | item | your tags |\n|---|---|---|---|\n" + rows + "\n",
            encoding="utf-8")
        return {"sample": len(chosen), "reference_note": str(ref)}

    # ------------------------------------------------------------ MCP side

    async def items(self) -> dict:
        sample = self._sample()
        found = {i["key"]: i for i in await self.lib.z.items_by_keys(sample["keys"])}
        vocab = self.lib.vocab.require()
        return {
            # Current tags are hidden, so a model cannot copy an earlier model's tags.
            "items": [{k2: v for k2, v in self.lib.summarize(found[k], detail=True).items()
                       if k2 not in ("tags", "automatic_tags")}
                      for k in sample["keys"] if k in found],
            "vocabulary": vocab.as_dict(),
            "rules": "topic/: 1 to 4 per item, most specific first. method/ and type/: only when they apply. "
                     "No status/ tags. Vocabulary tags only.",
        }

    def submit(self, label_: str, proposals: list[dict]) -> dict:
        sample = self._sample()
        vocab = self.lib.vocab.require()
        wanted = set(sample["keys"])
        clean, invalid, extra = {}, [], []
        for p in proposals:
            key = p.get("key", "")
            tags = [t.strip() for t in p.get("tags") or [] if t.strip()]
            if key not in wanted:
                extra.append(key)
                continue
            invalid += [f"{key}: {t}" for t in tags if not vocab.allows(t)]
            clean[key] = tags
        name = safe_label(label_)
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / f"proposals-{name}.json").write_text(json.dumps(
            {"label": label_, "time": dt.datetime.now().isoformat(timespec="seconds"), "proposals": clean},
            indent=1, ensure_ascii=False), encoding="utf-8")
        missing = [k for k in sample["keys"] if k not in clean]
        return {"saved": len(clean), "missing_items": missing, "not_in_sample": extra,
                "tags_not_in_vocabulary": invalid}

    # ------------------------------------------------------------ run

    def run(self, models: list[str], vault_cwd: Path | None = None, timeout: int = 1800) -> dict:
        self._sample()
        out = {}
        for model in models:
            name = safe_label(model)
            prop = self.dir / f"proposals-{name}.json"
            if prop.exists():
                prop.rename(prop.with_suffix(".old"))
            log = self.dir / f"run-{name}.jsonl"
            cmd = ["opencode", "run", "--agent", "librarian", "--model", model, "--format", "json",
                   PROMPT.format(label=model)]
            t0 = time.time()
            with log.open("w", encoding="utf-8") as fh:
                try:
                    proc = subprocess.run(cmd, cwd=vault_cwd or self.s.vault, stdout=fh,
                                          stderr=subprocess.STDOUT, timeout=timeout)
                    code = proc.returncode
                except subprocess.TimeoutExpired:
                    code = "timeout"
                except FileNotFoundError:
                    raise SystemExit("The opencode command was not found. Run this in your terminal.")
            usage = usage_from_log(log)
            usage.update(seconds=round(time.time() - t0), exit=code, submitted=prop.exists())
            (self.dir / f"usage-{name}.json").write_text(json.dumps(usage), encoding="utf-8")
            out[model] = usage
        return out

    # ------------------------------------------------------------ score

    def score(self) -> dict:
        sample = self._sample()
        ref_path = self.inbox / REF_NOTE
        reference = {k: v for k, v in parse_reference(ref_path.read_text(encoding="utf-8")).items() if v and k in sample["keys"]}
        if not reference:
            raise SystemExit(f"No reference tags yet. Fill the last column of {ref_path}.")
        vocab = self.lib.vocab.require()
        results = []
        for path in sorted(self.dir.glob("proposals-*.json")):
            data = json.loads(path.read_text(encoding="utf-8"))
            name = path.stem[len("proposals-"):]
            usage_path = self.dir / f"usage-{name}.json"
            usage = json.loads(usage_path.read_text(encoding="utf-8")) if usage_path.exists() else {}
            res = score_one(data["proposals"], reference, vocab.allows)
            res.update(model=data["label"], usage=usage)
            results.append(res)
        results.sort(key=lambda r: r["edits"])
        note = self.inbox / RESULTS_NOTE
        note.write_text(render_results(results, len(reference), sample), encoding="utf-8")
        return {"items_scored": len(reference), "results": results, "note": str(note)}


# ---------------------------------------------------------------- helpers

def parse_reference(text: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for line in text.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 4 or not re.fullmatch(r"[0-9A-Z]{8}", cells[0]):
            continue
        tags = [t.strip().strip("`") for t in re.split(r"[,;]\s*|\s+", cells[-1]) if t.strip().strip("`")]
        out[cells[0]] = tags
    return out


def scored(tags: list[str]) -> set[str]:
    return {t for t in tags if facet_of(t) in SCORED}


def score_one(proposals: dict[str, list[str]], reference: dict[str, list[str]], allows) -> dict:
    tp = fp = fn = exact = 0
    facet = {f: {"tp": 0, "fp": 0, "fn": 0} for f in SCORED}
    invalid = status = missing = no_topic = 0
    worst = []
    for key, gold_list in reference.items():
        gold = scored(gold_list)
        if key not in proposals:
            missing += 1
            fn += len(gold)
            for t in gold:
                facet[facet_of(t)]["fn"] += 1
            continue
        raw = proposals[key]
        invalid += sum(1 for t in raw if not allows(t))
        status += sum(1 for t in raw if facet_of(t) == "status")
        pred = scored(raw)
        if not any(facet_of(t) == "topic" for t in pred):
            no_topic += 1
        tp += len(pred & gold)
        fp += len(pred - gold)
        fn += len(gold - pred)
        exact += pred == gold
        for t in pred & gold:
            facet[facet_of(t)]["tp"] += 1
        for t in pred - gold:
            facet[facet_of(t)]["fp"] += 1
        for t in gold - pred:
            facet[facet_of(t)]["fn"] += 1
        edits = len(pred ^ gold)
        if edits:
            worst.append({"key": key, "edits": edits, "extra": sorted(pred - gold), "missed": sorted(gold - pred)})

    def prf(a: int, b: int, c: int) -> tuple[float, float, float]:
        p = a / (a + b) if a + b else 0.0
        r = a / (a + c) if a + c else 0.0
        return round(p, 3), round(r, 3), round(2 * p * r / (p + r), 3) if p + r else 0.0

    p, r, f1 = prf(tp, fp, fn)
    return {
        "edits": fp + fn, "precision": p, "recall": r, "f1": f1, "exact_items": exact,
        "by_facet": {f: dict(zip(("precision", "recall", "f1"), prf(v["tp"], v["fp"], v["fn"])))
                     for f, v in facet.items()},
        "invalid_tags": invalid, "status_tags": status, "missing_items": missing,
        "items_without_topic": no_topic,
        "worst": sorted(worst, key=lambda w: -w["edits"])[:5],
    }


def usage_from_log(path: Path) -> dict:
    """Best effort: sum tokens and cost from `opencode run --format json` step-finish events."""
    tokens_in = tokens_out = 0
    cost = 0.0
    steps = 0

    def walk(obj):
        nonlocal tokens_in, tokens_out, cost, steps
        if isinstance(obj, dict):
            if str(obj.get("type", "")).replace("_", "-") == "step-finish" and "tokens" in obj:
                steps += 1
                tok = obj.get("tokens") or {}
                tokens_in += int(tok.get("input") or 0) + int((tok.get("cache") or {}).get("read") or 0)
                tokens_out += int(tok.get("output") or 0) + int(tok.get("reasoning") or 0)
                cost += float(obj.get("cost") or 0)
                return
            for v in obj.values():
                walk(v)
        elif isinstance(obj, list):
            for v in obj:
                walk(v)

    try:
        for line in path.read_text(errors="replace", encoding="utf-8").splitlines():
            try:
                walk(json.loads(line))
            except ValueError:
                continue
    except OSError:
        pass
    if not steps:
        return {"steps": None}
    return {"steps": steps, "tokens_in": tokens_in, "tokens_out": tokens_out, "cost_usd": round(cost, 4)}


def render_results(results: list[dict], n: int, sample: dict) -> str:
    lines = [
        f"---\ncreated: {dt.date.today().isoformat()}\n---",
        f"Model test on {n} items (sample of {sample['created']}), scored against [[Model test reference]]. "
        "Edits = tags you would have to add or remove. Only topic/, method/ and type/ tags count.",
        "",
        "| model | edits | per item | precision | recall | F1 | exact items | invalid tags | status tags | "
        "missing items | steps | tokens in/out | cost (USD) | minutes |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        u = r["usage"]
        tok = f"{u.get('tokens_in', 'n/a')}/{u.get('tokens_out', 'n/a')}" if u.get("steps") else "n/a"
        mins = round(u["seconds"] / 60, 1) if u.get("seconds") is not None else "n/a"
        lines.append(
            f"| {r['model']} | {r['edits']} | {r['edits'] / n:.1f} | {r['precision']} | {r['recall']} | "
            f"{r['f1']} | {r['exact_items']}/{n} | {r['invalid_tags']} | {r['status_tags']} | "
            f"{r['missing_items']} | {u.get('steps') or 'n/a'} | {tok} | {u.get('cost_usd', 'n/a')} | {mins} |")
    lines += ["", "## By facet (F1)", "", "| model | topic | method | type |", "|---|---|---|---|"]
    for r in results:
        bf = r["by_facet"]
        lines.append(f"| {r['model']} | {bf['topic']['f1']} | {bf['method']['f1']} | {bf['type']['f1']} |")
    for r in results:
        if r["worst"]:
            lines += ["", f"## Largest differences: {r['model']}", ""]
            for w in r["worst"]:
                lines.append(f"- {w['key']}: extra {', '.join(w['extra']) or 'none'}; missed "
                             f"{', '.join(w['missed']) or 'none'}")
    return "\n".join(lines) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser(prog="zotero-bakeoff", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sample")
    s.add_argument("--n", type=int, default=25)
    s.add_argument("--seed", type=int, default=1)
    s.add_argument("--force", action="store_true")
    r = sub.add_parser("run")
    r.add_argument("--model", action="append", required=True)
    sub.add_parser("score")
    args = ap.parse_args()
    b = Bakeoff(Library(Settings.from_env()))
    if args.cmd == "sample":
        res = asyncio.run(b.make_sample(args.n, args.seed, args.force))
    elif args.cmd == "run":
        res = b.run(args.model)
    else:
        res = b.score()
    print(json.dumps(res, indent=2, ensure_ascii=False))
    sys.exit(0)


if __name__ == "__main__":
    main()
