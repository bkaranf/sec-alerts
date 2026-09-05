"""Reviewed source context, valid only for the exact archived reporting package.

This small catalog complements verified table extraction. It is not a generic
extractor: new documents or quarters require fresh review or optional narrative.
Never carry a reviewed claim across an issuer, period, or content revision.
"""
import hashlib
import json
from pathlib import Path


def select_context(documents, *, cik, period, new_ids=None, catalog_path=None):
    catalog_path = catalog_path or Path(__file__).with_name("reviewed_context.json")
    if not Path(catalog_path).exists():
        return []
    catalog = json.loads(Path(catalog_path).read_text(encoding="utf-8"))
    eligible = [d for d in documents if d.cik.lstrip("0") == str(cik).lstrip("0")]
    selected = []
    for entry in catalog.get("entries", []):
        if entry["cik"].lstrip("0") != str(cik).lstrip("0") or entry["report_period"] != period:
            continue
        refs = []
        for proof in entry["sources"]:
            doc = next((d for d in eligible if d.content_hash == proof["content_hash"] and d.period == proof["period"] and d.kind == proof["kind"]), None)
            if not doc:
                break
            try:
                if hashlib.sha256(Path(doc.path).read_bytes()).hexdigest() != proof["content_hash"]:
                    break
                assets = doc.metadata.get("assets", [])
                for expected in proof.get("asset_hashes", []):
                    asset = next((a for a in assets if a.get("content_hash") == expected), None)
                    if not asset or hashlib.sha256(Path(asset["path"]).read_bytes()).hexdigest() != expected:
                        raise ValueError("Reviewed source image is missing or changed")
            except (OSError, ValueError):
                break
            refs.append({**proof, "document_id": doc.id, "source_url": doc.url})
        else:
            if new_ids is not None and not any(p["document_id"] in new_ids for p in refs):
                continue
            selected.append({**entry, "sources": refs})
    return selected
