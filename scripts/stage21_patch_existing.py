# ruff: noqa: E501
"""Small, idempotent edits to files from earlier stages (run once from the project root, before the tests).

1. llm/client.py        - report ``usage["cached_tokens"]`` when the provider returns a positive int
2. sandbox/backends.py  - ``# nosec`` on three reviewed bandit findings (taskkill via PATH, /tmp as a tmpfs target)
3. scripts/stage16_docs.py - ``# ruff: noqa: E501`` (a long line failed ``ruff check .``)
4. data/make_recurrences.py - one over-long line wrapped the way ``ruff format`` wants it
Each edit states what it did; a file that no longer matches is reported and left alone (nothing is guessed).
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

CLIENT_OLD = """            usage = {"prompt_tokens": resp.usage.prompt_tokens, "completion_tokens": resp.usage.completion_tokens}
"""
CLIENT_NEW = (
    CLIENT_OLD
    + """            details = getattr(resp.usage, "prompt_tokens_details", None)
            cached = getattr(details, "cached_tokens", None)
            if isinstance(cached, int) and not isinstance(cached, bool) and cached > 0:
                usage["cached_tokens"] = cached  # provider-side prompt cache hit (Groq gpt-oss reports this)
"""
)


def edit(rel: str, fn, done_marker: str) -> None:
    path = ROOT / rel
    if not path.exists():
        print(f"skip {rel}: not found")
        return
    text = path.read_text(encoding="utf-8")
    if done_marker in text:
        print(f"skip {rel}: already done")
        return
    new = fn(text)
    if new is None:
        print(
            f"WARNING {rel}: expected text not found, left unchanged (apply by hand, see docs/STAGE21_OBSERVABILITY.md)"
        )
        return
    path.write_text(new, encoding="utf-8", newline="")
    print(f"updated {rel}")


def client(text: str) -> str | None:
    return text.replace(CLIENT_OLD, CLIENT_NEW, 1) if CLIENT_OLD in text else None


def backends(text: str) -> str | None:
    out, n = [], 0
    for line in text.split("\n"):
        has_cr = line.endswith("\r")
        body = line.rstrip("\r")
        if "nosec" not in body:
            if '"taskkill"' in body and "# noqa: S603, S607" in body:
                body, n = body + "  # nosec B607", n + 1
            elif ("tmpfs = f" in body and "/tmp:rw" in body) or body.strip() == '"/tmp",  # noqa: S108':
                body, n = body + "  # nosec B108", n + 1
        out.append(body + ("\r" if has_cr else ""))
    return "\n".join(out) if n else None


def prepend(marker_line: str):
    def fn(text: str) -> str | None:
        return marker_line + "\n" + text

    return fn


def long_line(text: str) -> str | None:
    old = (
        '    GOLD_OUT.write_text(json.dumps({"_note": "Stage 12 recurrence set; see data/make_recurrences.py", '
        '"incidents": gold}, indent=2) + "\\n")\n'
    )
    new = (
        "    GOLD_OUT.write_text(\n"
        '        json.dumps({"_note": "Stage 12 recurrence set; see data/make_recurrences.py", "incidents": gold}, indent=2)\n'
        '        + "\\n"\n'
        "    )\n"
    )
    return text.replace(old, new, 1) if old in text else None


edit("src/incident_agent/llm/client.py", client, "prompt_tokens_details")
edit("src/incident_agent/sandbox/backends.py", backends, "nosec B607")
edit("scripts/stage16_docs.py", prepend("# ruff: noqa: E501"), "ruff: noqa: E501")
edit("data/make_recurrences.py", long_line, '        + "\\n"\n    )')
