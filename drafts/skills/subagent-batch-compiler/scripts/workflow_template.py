"""Reusable run_workflow wave runner template with sliding concurrency & flush.

Adapt `_build_prompt` and `SCHEMAS` for your target domain, then pass this script
to `run_workflow(ScriptPath=..., Args={...})`.
"""

import asyncio
import json
from pathlib import Path


def _build_prompt(item: dict, workspace_root: Path) -> str:
    """Build a strictly isolated per-item prompt. Customize per domain."""
    tid = item["task_id"]
    img_note = ""
    if item.get("has_images") and item.get("image_paths"):
        paths = [
            (workspace_root / p).resolve().as_posix()
            for p in item["image_paths"]
        ]
        img_note = (
            "Read these media files first via `read_file` in exact order:\n"
            + "\n".join(f"- Image {i + 1}: {p}" for i, p in enumerate(paths))
        )
    return (
        f"Compile item `{tid}` strictly from the sanitized payload below.\n"
        "FORBIDDEN: Do NOT read source directories, md/ docs, src/, DBs, or task_map.json.\n"
        f"{img_note}\n\n"
        f"```json\n{json.dumps(item, ensure_ascii=False, indent=2)}\n```\n"
        f"Submit structured JSON via `finish` with `task_id=\"{tid}\"`."
    )


async def run_wave(
    *,
    batch_files: list[str],
    out_file: str,
    schemas_by_mode: dict[str, dict],
    max_concurrency: int = 10,
) -> dict:
    workspace_root = Path(WORKSPACES[0]) if WORKSPACES else Path(".")
    out_path = workspace_root / out_file
    err_path = out_path.with_suffix(".errors.json")

    items: list[dict] = []
    for bf in batch_files:
        for line in (workspace_root / bf).read_text(encoding="utf-8").splitlines():
            if line.strip():
                items.append(json.loads(line))

    done_ids: set[str] = set()
    if out_path.exists():
        for line in out_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    done_ids.add(json.loads(line)["task_id"])
                except Exception:
                    pass

    pending = [it for it in items if it["task_id"] not in done_ids]
    log(
        f"Wave total={len(items)}, resumed_skip={len(done_ids)}, "
        f"pending={len(pending)}, concurrency={max_concurrency}"
    )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    sem = asyncio.Semaphore(max(1, max_concurrency))
    errors: list[dict] = []
    ok_new = 0

    async def _process_one(item: dict) -> dict:
        nonlocal ok_new
        tid = item["task_id"]
        mode = item.get("mode", "DEFAULT")
        schema = schemas_by_mode[mode]
        prompt = _build_prompt(item, workspace_root)
        async with sem:
            try:
                res = await agent(
                    prompt=prompt,
                    schema=schema,
                    role=f"{tid} ({mode})",
                    model="inherit",
                )
                if isinstance(res, str):
                    res = json.loads(res)
                res["task_id"] = tid
                with open(out_path, "a", encoding="utf-8", newline="\n") as f:
                    f.write(json.dumps(res, ensure_ascii=False) + "\n")
                    f.flush()
                ok_new += 1
                return {"ok": True, "task_id": tid}
            except Exception as exc:
                err = {"task_id": tid, "mode": mode, "error": str(exc)}
                errors.append(err)
                err_path.write_text(
                    json.dumps(errors, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                log(f"ERROR on {tid}: {exc}")
                return {"ok": False, "task_id": tid, "error": str(exc)}

    if pending:
        await parallel(pending, _process_one)

    return {
        "out_file": out_file,
        "total": len(items),
        "already_done": len(done_ids),
        "new_succeeded": ok_new,
        "failed": len(errors),
        "failed_tasks": [e["task_id"] for e in errors],
    }
