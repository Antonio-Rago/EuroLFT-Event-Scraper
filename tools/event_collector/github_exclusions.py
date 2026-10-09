"""Remember collector event PRs closed without merging as editorial rejections."""

import base64
import json
import re

from .moderate import decide
from .registry import validator
from .reconcile import validate_record

RESET_LABEL = "event-review-reset"


def pages(client, path, limit=20):
    separator = "&" if "?" in path else "?"
    for page in range(1, limit + 1):
        items = client.request(path + separator + "per_page=100&page=" + str(page))
        yield from items
        if len(items) < 100:
            return
    raise ValueError("GitHub review history exceeded the scan bound; increase it explicitly.")


def closed_rejections(client, root):
    result = {}
    for pull in pages(client, "/pulls?state=closed&base=main&sort=updated&direction=desc"):
        branch = pull.get("head", {}).get("ref", "")
        if pull.get("merged_at") or not branch.startswith("automation/community-events"):
            continue
        if any(label.get("name") == RESET_LABEL for label in pull.get("labels", [])):
            # Administrative resets preserve the queue; they are not editorial decisions.
            continue
        head = pull.get("head", {})
        if (head.get("repo") or {}).get("full_name") != "EuroLFT/eurolft.github.io":
            continue
        if not re.fullmatch(r"[a-f0-9]{40}", head.get("sha", "")):
            raise ValueError("Closed proposal has an invalid Git revision.")
        number = str(pull["number"])
        events = list(pages(client, "/issues/" + number + "/events"))
        closures = [item for item in events if item.get("event") == "closed" and item.get("actor", {}).get("type") == "User"]
        if not closures:
            # Automated cleanup is not an editorial rejection.
            continue
        closed = closures[-1]
        files = pages(client, "/pulls/" + number + "/files")
        for item in files:
            filename = item["filename"]
            if not re.fullmatch(r"_event_collector/records/event-[a-z0-9_-]+\.json", filename):
                continue
            if item["status"] not in ("added", "modified"):
                continue
            data = client.request("/contents/" + filename + "?ref=" + head["sha"])
            if data.get("encoding") != "base64":
                raise ValueError("Closed event proposal cannot be read as JSON.")
            record = json.loads(base64.b64decode(data["content"]).decode("utf-8"))
            validate_record(record, validator(root))
            if record["decision"]["status"] not in ("pending", "draft"):
                continue
            rejected = decide(record, "reject", closed["actor"]["login"],
                              "Event proposal PR #" + number + " closed without merging.")
            rejected["history"][-1]["at"] = closed["created_at"]
            # Most recently updated closure wins for the same stable identity.
            result.setdefault(rejected["id"], rejected)
    return result
