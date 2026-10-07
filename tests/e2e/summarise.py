"""Print the tool calls, cost and final answer from a `claude -p --output-format stream-json` log."""

import json
import sys
from pathlib import Path

for line in Path(sys.argv[1]).read_text().splitlines():
    try:
        m = json.loads(line)
    except json.JSONDecodeError:
        continue
    if m.get("type") == "assistant":
        for c in m["message"]["content"]:
            if c["type"] == "tool_use":
                print("TOOL", c["name"], json.dumps(c["input"])[:240])
    elif m.get("type") == "result":
        print(f"\nCOST ${m.get('total_cost_usd', 0):.2f}, {m.get('num_turns')} turns\n")
        print(m.get("result"))
