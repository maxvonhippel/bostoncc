#!/usr/bin/env python3
"""Bump the talk schedule by one slot.

Promotes the first "Future Meetings" entry into the "Next Meeting" slot, and
demotes the current "Next Meeting" to the top of "Prior Meetings". The newly
promoted talk gets an emoji picked by Grok (xAI) and downloaded as an SVG from
OpenMoji into emojis/.

Usage:
    python3 bump.py            # do it
    python3 bump.py --dry-run  # parse and print the plan, touch nothing
"""

import datetime
import hashlib
import json
import os
import re
import sys
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
INDEX = os.path.join(ROOT, "index.html")
EMOJI_DIR = os.path.join(ROOT, "emojis")
ENV = os.path.join(ROOT, ".env")

OPENMOJI_URL = "https://openmoji.org/data/color/svg/{code}.svg"
XAI_URL = "https://api.x.ai/v1/chat/completions"
MAX_EMOJI_TRIES = 6


def load_env():
    """Read .env into os.environ without clobbering anything already set."""
    if not os.path.exists(ENV):
        return
    with open(ENV) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            os.environ.setdefault(key.strip(), val.strip())


def strip_tags(html):
    return re.sub(r"<[^>]+>", "", html).strip()


# --- date helpers -----------------------------------------------------------
# Header carries "July 28, 2026 at 5PM EST"; the queue carries "07/31/26".

def header_date_to_slash(text):
    """'July 28, 2026 ...' -> ('07/28/26', ' at 5PM EST')."""
    m = re.match(r"([A-Za-z]+ \d{1,2}, \d{4})(.*)", text)
    if not m:
        raise ValueError(f"cannot parse next-meeting date from {text!r}")
    d = datetime.datetime.strptime(m.group(1), "%B %d, %Y")
    return d.strftime("%m/%d/%y"), m.group(2)


def slash_to_long(slash):
    """'07/31/26' -> 'July 31, 2026'."""
    d = datetime.datetime.strptime(slash, "%m/%d/%y")
    return f"{d.strftime('%B')} {d.day}, {d.year}"


# --- emoji ------------------------------------------------------------------

def pick_emoji(topic, speaker, existing_names, avoid_emojis):
    """Ask Grok for an emoji + short name that fits the talk."""
    key = os.environ.get("XAI_API_KEY")
    if not key:
        raise SystemExit("XAI_API_KEY not set (check .env)")
    model = os.environ.get("XAI_MODEL", "grok-4.3")

    system = (
        "You choose a single emoji to illustrate an academic talk on a seminar "
        "webpage. Reply with ONLY a compact JSON object of the form "
        '{"emoji": "<one emoji>", "name": "<short-kebab-case-name>"}. '
        "The name is a filename stem: lowercase, a-z 0-9 and hyphens only, no "
        "extension. Pick an emoji that evokes the talk's subject. Avoid the "
        "names already in use, listed below."
    )
    user = (
        f"Talk: {topic}\nSpeaker: {speaker}\n\n"
        f"Names already used: {', '.join(sorted(existing_names))}"
    )
    if avoid_emojis:
        user += (
            "\n\nThese emojis are already taken; pick a visibly different one, "
            f"not any of: {' '.join(avoid_emojis)}"
        )
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    effort = os.environ.get("XAI_REASONING_EFFORT")
    if effort:
        body["reasoning_effort"] = effort

    req = urllib.request.Request(
        XAI_URL,
        data=json.dumps(body).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        content = json.load(resp)["choices"][0]["message"]["content"]

    m = re.search(r"\{.*\}", content, re.DOTALL)
    if not m:
        raise SystemExit(f"Grok did not return JSON:\n{content}")
    data = json.loads(m.group(0))
    emoji = data["emoji"].strip()
    name = data.get("name", "").strip().lower()
    if not re.fullmatch(r"[a-z0-9-]+", name or ""):
        name = "-".join(f"{ord(c):x}" for c in emoji)
    return emoji, name


def openmoji_candidates(emoji):
    """Filenames OpenMoji might use, most specific first."""
    codes = [f"{ord(c):04X}" for c in emoji]
    no_vs = [f"{ord(c):04X}" for c in emoji if c != "️"]
    cands = ["-".join(codes)]
    if no_vs != codes:
        cands.append("-".join(no_vs))
    if len(codes) > 1:
        cands.append(codes[0])
    return cands


def fetch_emoji_svg(emoji):
    """Return OpenMoji SVG bytes for an emoji, or None if none resolve."""
    for code in openmoji_candidates(emoji):
        try:
            with urllib.request.urlopen(OPENMOJI_URL.format(code=code), timeout=30) as r:
                data = r.read()
        except urllib.error.HTTPError:
            continue
        head = data.lstrip()
        if head.startswith(b"<?xml") or head.startswith(b"<svg"):
            return data
    return None


def save_emoji(svg, name, emoji):
    """Write SVG bytes to emojis/<name>.svg, suffixing on a name clash."""
    fname = f"{name}.svg"
    if os.path.exists(os.path.join(EMOJI_DIR, fname)):
        fname = f"{name}-{openmoji_candidates(emoji)[0].lower()}.svg"
    with open(os.path.join(EMOJI_DIR, fname), "wb") as f:
        f.write(svg)
    return fname


def existing_svg_hashes():
    """Map sha256(content) -> filename for every emoji SVG already on disk."""
    hashes = {}
    for f in os.listdir(EMOJI_DIR):
        if f.endswith(".svg"):
            with open(os.path.join(EMOJI_DIR, f), "rb") as fh:
                hashes[hashlib.sha256(fh.read()).hexdigest()] = f
    return hashes


# --- parsing ----------------------------------------------------------------

NEXT_RE = re.compile(
    r'(?P<pre><h3>Next Meeting:\s*)(?P<date>.*?)'
    r'(?P<mid></h3>\s*<table>\s*<tr>\s*<td>\s*<img src="emojis/)'
    r'(?P<emoji>[^"]+)(?P<mid2>"[^>]*/>\s*<td>\s*)'
    r'(?P<content>.*?)(?P<post>\s*</td>\s*</tr>\s*</table>)',
    re.DOTALL,
)

FUTURE_FIRST_RE = re.compile(
    r'(?P<pre><h3>Future Meetings</h3>\s*<ul>\s*)'
    r'<li><span>\[(?P<date>\d{2}/\d{2}/\d{2}),\s*(?P<topic>.*?)\]</span>\s*'
    r'(?P<speaker>.*?)</li>\s*',
    re.DOTALL,
)

PRIOR_OPEN_RE = re.compile(r'(<h3>Prior Meetings</h3>\s*<table>)')


def main():
    dry = "--dry-run" in sys.argv
    load_env()

    with open(INDEX, encoding="utf-8") as f:
        html = f.read()

    nxt = NEXT_RE.search(html)
    if not nxt:
        raise SystemExit("could not find the Next Meeting block")
    fut = FUTURE_FIRST_RE.search(html)
    if not fut:
        raise SystemExit("Future Meetings queue is empty; nothing to promote")

    # Current next meeting -> becomes a prior meeting.
    old_slash, tail = header_date_to_slash(nxt.group("date"))
    old_emoji = nxt.group("emoji")
    old_content = nxt.group("content").strip()

    # First future meeting -> becomes the next meeting.
    new_slash = fut.group("date")
    topic = fut.group("topic").strip()
    speaker = fut.group("speaker").strip()
    new_content = f"{topic}, {speaker}"
    new_header = f"{slash_to_long(new_slash)}{tail}"

    existing = {os.path.splitext(f)[0] for f in os.listdir(EMOJI_DIR)
                if f.endswith(".svg")}
    used_hashes = existing_svg_hashes()

    print("Promoting to Next Meeting:")
    print(f"  {new_header}")
    print(f"  {strip_tags(topic)}  |  {strip_tags(speaker)}")
    print("Demoting to Prior Meetings:")
    print(f"  {old_slash}: {strip_tags(old_content)}  (emojis/{old_emoji})")

    if dry:
        print("\n[dry run] would ask Grok for an emoji and rewrite index.html")
        return

    # Ask Grok for an emoji, but reject any whose SVG we already have on disk
    # (matched by content hash) and try again with that emoji ruled out.
    new_emoji_file = None
    avoid = []
    for _ in range(MAX_EMOJI_TRIES):
        emoji, name = pick_emoji(strip_tags(topic), strip_tags(speaker),
                                 existing, avoid)
        svg = fetch_emoji_svg(emoji)
        if svg is None:
            print(f"  {emoji}: no OpenMoji SVG, retrying")
            avoid.append(emoji)
            continue
        clash = used_hashes.get(hashlib.sha256(svg).hexdigest())
        if clash:
            print(f"  {emoji}: already used ({clash}), retrying")
            avoid.append(emoji)
            continue
        new_emoji_file = save_emoji(svg, name, emoji)
        print(f"\nGrok picked {emoji}  -> emojis/{new_emoji_file}")
        break
    if new_emoji_file is None:
        raise SystemExit(f"No unused emoji after {MAX_EMOJI_TRIES} tries "
                         f"(rejected: {' '.join(avoid)})")

    # 1. Rewrite the Next Meeting block (header date, emoji, content).
    new_block = (
        f"{nxt.group('pre')}{new_header}{nxt.group('mid')}"
        f"{new_emoji_file}{nxt.group('mid2')}{new_content}{nxt.group('post')}"
    )
    html = html[:nxt.start()] + new_block + html[nxt.end():]

    # 2. Insert the demoted meeting as the top row of Prior Meetings.
    prior_row = (
        "\n            <tr>\n                <td>\n"
        f'                    <img src="emojis/{old_emoji}" class="icon" />\n'
        "                <td>\n"
        f"                    {old_slash}: {old_content}\n"
        "                </td>\n            </tr>"
    )
    html = PRIOR_OPEN_RE.sub(lambda m: m.group(1) + prior_row, html, count=1)

    # 3. Drop the promoted entry from the Future Meetings queue.
    fut2 = FUTURE_FIRST_RE.search(html)
    html = html[:fut2.start()] + fut2.group("pre") + html[fut2.end():]

    with open(INDEX, "w", encoding="utf-8") as f:
        f.write(html)
    print("\nindex.html updated.")


if __name__ == "__main__":
    main()
