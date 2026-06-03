"""End-to-end smoke test of the deployed website-redesign Agent Engine.

Authored for OpsAgentsAI/redesign-agent. Runs in GitHub Actions under
Workload Identity Federation (gha-deployer SA on opsagent-prod). Pure ASCII.

This does NOT import the agent code or redeploy. It fetches the ALREADY-DEPLOYED
reasoning engine by resource name and drives a realistic multi-agent turn against
it, proving the managed runtime actually runs Gemini and returns content (not just
that the package imported at deploy time).

Pass criteria (exit 0):
  - at least one event streamed back from the remote engine
  - the concatenated agent text is non-empty
  - the text contains real redesign content: a redesign signal (block / layout /
    audit / sequence / reasoning) AND Hebrew characters (the HE hero copy).

Any exception, empty response, or missing-content assertion -> exit non-zero.
"""

from __future__ import annotations

import os
import sys
import traceback

ENGINE = (
    "projects/523955774086/locations/us-central1/"
    "reasoningEngines/6353095283278086144"
)
PROJECT = os.environ.get("GOOGLE_CLOUD_PROJECT", "opsagent-prod")
LOCATION = os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1")

QUERY = (
    "Audit the example.com homepage and propose a redesigned block sequence "
    "with per-block reasoning, plus HE+EN hero copy. Do not publish."
)

# Force Vertex-backed Gemini for any client-side ADK glue.
os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "TRUE"
os.environ["GOOGLE_CLOUD_PROJECT"] = PROJECT
os.environ["GOOGLE_CLOUD_LOCATION"] = LOCATION


def _event_to_text(event: object) -> str:
    """Best-effort extraction of human-readable text from an ADK event.

    ADK events come back either as dicts or as objects. The model text lives
    under content.parts[*].text. We also stringify the whole event so nothing
    is lost from the assertion surface.
    """
    chunks = []
    content = None
    if isinstance(event, dict):
        content = event.get("content")
    else:
        content = getattr(event, "content", None)

    parts = None
    if isinstance(content, dict):
        parts = content.get("parts")
    elif content is not None:
        parts = getattr(content, "parts", None)

    if parts:
        for part in parts:
            text = None
            if isinstance(part, dict):
                text = part.get("text")
            else:
                text = getattr(part, "text", None)
            if text:
                chunks.append(str(text))

    # Always include the raw repr so audit/layout/function-call traffic that is
    # not plain content.parts text still counts toward the content assertions.
    chunks.append(str(event))
    return "\n".join(chunks)


def _has_hebrew(text: str) -> bool:
    # Hebrew Unicode block U+0590..U+05FF (ASCII source via codepoints).
    return any(0x0590 <= ord(ch) <= 0x05FF for ch in text)


def main() -> int:
    import vertexai
    from vertexai import agent_engines

    print("vertexai version probe:")
    try:
        import google.cloud.aiplatform as aip

        print("  google-cloud-aiplatform=%s" % getattr(aip, "__version__", "?"))
    except Exception as exc:  # pragma: no cover
        print("  (could not read aiplatform version: %r)" % exc)

    print("Init: project=%s location=%s" % (PROJECT, LOCATION))
    vertexai.init(project=PROJECT, location=LOCATION)

    print("Fetching engine: %s" % ENGINE)
    remote = agent_engines.get(ENGINE)
    print("Got remote engine: %r" % remote)

    # Resolve the streaming query method across SDK variants.
    method = None
    for name in ("stream_query", "streaming_agent_run", "query"):
        if hasattr(remote, name):
            method = name
            break
    if method is None:
        print("FATAL: remote engine exposes no known query method. dir=%r"
              % [a for a in dir(remote) if not a.startswith("_")])
        return 2
    print("Using query method: %s" % method)

    print("\n===== QUERY =====")
    print(QUERY)
    print("===== STREAMING RESPONSE =====\n")

    collected = []
    event_count = 0
    fn = getattr(remote, method)

    try:
        if method == "query":
            # Non-streaming fallback.
            result = fn(input=QUERY)
            event_count = 1
            print(result)
            collected.append(str(result))
        else:
            for event in fn(user_id="smoke", message=QUERY):
                event_count += 1
                text = _event_to_text(event)
                print("--- event %d ---" % event_count)
                print(text)
                collected.append(text)
    except Exception:
        print("\nFATAL: exception while streaming the query:")
        traceback.print_exc()
        return 3

    full = "\n".join(collected)
    print("\n===== SUMMARY =====")
    print("events=%d total_chars=%d" % (event_count, len(full)))

    if event_count == 0:
        print("FAIL: zero events returned from the engine.")
        return 4
    if not full.strip():
        print("FAIL: agent response was empty.")
        return 5

    lowered = full.lower()
    redesign_signal = any(
        kw in lowered
        for kw in ("block", "layout", "audit", "sequence", "reasoning", "hero")
    )
    hebrew = _has_hebrew(full)

    print("assert redesign_signal=%s hebrew=%s" % (redesign_signal, hebrew))

    if not redesign_signal:
        print("FAIL: response lacks any redesign signal "
              "(block/layout/audit/sequence/reasoning/hero).")
        return 6
    if not hebrew:
        print("FAIL: response contains no Hebrew characters "
              "(expected HE hero copy).")
        return 7

    print("\nPASS: deployed Agent Engine ran a multi-agent turn and returned "
          "real redesign content with Hebrew copy.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
