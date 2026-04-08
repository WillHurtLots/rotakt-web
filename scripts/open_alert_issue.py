"""Create or update the daily catalog audit GitHub Issue."""
import json
import subprocess
import sys
from pathlib import Path

ALERTS = Path("data/alerts.json")
DASHBOARD = "https://willhurtlots.github.io/rotakt-web/"


def main() -> int:
    if not ALERTS.exists():
        print("alerts.json missing — nothing to do.")
        return 0

    a = json.loads(ALERTS.read_text(encoding="utf-8"))
    total = a.get("total_alerts", 0)
    date = a.get("date")

    if total == 0:
        print("No alerts — skipping issue.")
        return 0

    title = f"[{date}] RotaktWeb · {total} alerte de catalog"
    lines = [f"## Raport zilnic {date}", "", f"**Total alerte:** {total}", ""]
    for site, counts in a["sites"].items():
        lines.append(f"### {site}")
        lines.append("")
        for k, v in counts.items():
            if v:
                lines.append(f"- **{k}**: {v}")
        lines.append("")
    lines.append("---")
    lines.append(f"Dashboard: {DASHBOARD}")
    body = "\n".join(lines)

    # De-dup: search for an existing open issue with this exact title
    out = subprocess.run(
        ["gh", "issue", "list", "--state", "open", "--search", title, "--json", "number,title"],
        capture_output=True, text=True, check=True,
    ).stdout
    existing = [i for i in json.loads(out) if i["title"] == title]

    if existing:
        num = existing[0]["number"]
        subprocess.run(["gh", "issue", "comment", str(num), "--body", body], check=True)
        print(f"Updated existing issue #{num}")
    else:
        subprocess.run(
            ["gh", "issue", "create", "--title", title, "--body", body],
            check=True,
        )
        print("Created new issue.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
