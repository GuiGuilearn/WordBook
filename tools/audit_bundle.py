"""Reject DLLs accidentally discovered through unrelated Codex native tools."""
import ast
import json
import sys
from pathlib import Path


def audit(toc_path):
    toc = ast.literal_eval(Path(toc_path).read_text(encoding="utf-8"))
    binaries = []

    def visit(item):
        if isinstance(item, (list, tuple)):
            if len(item) == 3 and all(isinstance(x, str) for x in item) and item[2] in ("BINARY", "EXTENSION"):
                binaries.append({"name": item[0], "source": item[1]})
            else:
                for child in item:
                    visit(child)

    visit(toc)
    forbidden = [item for item in binaries if "codex-runtimes" in item["source"].lower()]
    return {"ok": not forbidden, "binary_count": len(binaries), "forbidden_sources": forbidden,
            "system_aliases": [item for item in binaries if Path(item["name"]).name.lower().startswith("api-ms-win-")],
            "icu_libraries": [item for item in binaries if Path(item["name"]).name.lower().startswith("icu")]}


if __name__ == "__main__":
    result = audit(sys.argv[1])
    output = Path(sys.argv[2])
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Bundle dependency audit: {'PASS' if result['ok'] else 'FAIL'} ({result['binary_count']} binaries)")
    raise SystemExit(0 if result["ok"] else 1)
