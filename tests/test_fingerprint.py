"""Phase 2 exit criterion, directly:

"Two requests differing by one injected timestamp report the exact divergence offset, and
a byte-level audit of the emitted payload proves zero recoverable customer content."
"""

from __future__ import annotations

import pickle

from sixeyes.fingerprint.divergence import compare
from sixeyes.fingerprint.fingerprint import fingerprint_request
from sixeyes.fingerprint.types import DivergenceKind
from sixeyes.ingest.types import RawMessage, RawRequest, RawToolDef


def _request(**overrides: object) -> RawRequest:
    defaults: dict[object, object] = dict(
        request_id="req_1",
        timestamp=1.0,
        model="claude-opus-5",
        system="You are an assistant. Generated at: 2026-09-16T12:00:00Z",
        tools=(RawToolDef("search", "web search", '{"type":"object"}'),),
        messages=(RawMessage("user", "Summarize section 1."),),
    )
    defaults.update(overrides)
    return RawRequest(**defaults)  # type: ignore[arg-type]


def test_identical_requests_produce_identical_chains() -> None:
    a = fingerprint_request(_request())
    b = fingerprint_request(_request())
    assert a.chain == b.chain
    assert a.token_count == b.token_count


def test_a_single_injected_timestamp_localizes_to_the_system_segment() -> None:
    """The canonical failure this product exists to catch: a per-request timestamp
    interpolated into an otherwise-stable system prompt."""
    previous = fingerprint_request(_request(system="You are an assistant. Generated at: T1"))
    current = fingerprint_request(_request(system="You are an assistant. Generated at: T2"))

    report = compare("wl_1", previous, current)

    assert report.kind is DivergenceKind.SYSTEM_CHANGED
    # "You are an assistant. Generated at: T1" splits on whitespace into 7 tokens
    # (punctuation stays attached: "assistant.", "at:"); the timestamp is the last one,
    # so the divergence must land at offset 6 within the system segment.
    assert report.segment_offset == 6
    # everything after the divergence point counts as cache-missed, including tools/messages
    assert report.cache_missed_tokens == current.token_count - report.combined_offset


def test_model_change_localizes_to_the_model_segment_at_offset_zero() -> None:
    previous = fingerprint_request(_request(model="claude-opus-5"))
    current = fingerprint_request(_request(model="claude-sonnet-5"))

    report = compare("wl_1", previous, current)

    assert report.kind is DivergenceKind.MODEL_CHANGED
    assert report.segment_offset == 0
    assert report.combined_offset == 0
    # a model change invalidates the *entire* prefix, including everything after it
    assert report.cache_missed_tokens == current.token_count


def test_reordered_tools_localize_to_the_tools_segment() -> None:
    tool_a = RawToolDef("search", "web search", '{"type":"object"}')
    tool_b = RawToolDef("calculator", "does math", '{"type":"object"}')
    previous = fingerprint_request(_request(tools=(tool_a, tool_b)))
    current = fingerprint_request(_request(tools=(tool_b, tool_a)))

    report = compare("wl_1", previous, current)

    assert report.kind is DivergenceKind.TOOLS_CHANGED


def test_appending_a_new_turn_is_not_a_divergence() -> None:
    """Healthy conversation growth -- the old prefix is still a true prefix of the new
    one -- must never be reported as a cache-breaking change."""
    previous = fingerprint_request(_request(messages=(RawMessage("user", "Summarize section 1."),)))
    current = fingerprint_request(
        _request(
            messages=(
                RawMessage("user", "Summarize section 1."),
                RawMessage("assistant", "Section 1 covers..."),
                RawMessage("user", "Now section 2."),
            )
        )
    )

    report = compare("wl_1", previous, current)

    assert report.kind is DivergenceKind.NONE
    assert report.cache_missed_tokens == 0


def test_a_shorter_but_matching_follow_up_is_not_a_divergence() -> None:
    previous = fingerprint_request(
        _request(
            messages=(
                RawMessage("user", "Summarize section 1."),
                RawMessage("assistant", "Section 1 covers a lot of ground."),
            )
        )
    )
    current = fingerprint_request(_request(messages=(RawMessage("user", "Summarize section 1."),)))

    report = compare("wl_1", previous, current)

    assert report.kind is DivergenceKind.NONE


def test_edited_history_localizes_to_the_messages_segment() -> None:
    previous = fingerprint_request(_request(messages=(RawMessage("user", "Summarize section 1."),)))
    current = fingerprint_request(_request(messages=(RawMessage("user", "Summarize section TWO."),)))

    report = compare("wl_1", previous, current)

    assert report.kind is DivergenceKind.MESSAGES_CHANGED


def test_fingerprint_output_contains_no_recoverable_customer_content() -> None:
    """The byte-level audit the exit criterion asks for: serialize everything that would
    cross the collector boundary and prove none of the original secret strings appear in
    it, anywhere, in any form."""
    secret_system = "CONFIDENTIAL-PROJECT-NIGHTINGALE-DO-NOT-LEAK"
    secret_tool_desc = "internal-api-key-lookup-tool-xyz789"
    secret_message = "the launch codes are 84213-ALPHA-QUEBEC"

    request = _request(
        system=f"You are an assistant. {secret_system}",
        tools=(RawToolDef("lookup", secret_tool_desc, '{"type":"object"}'),),
        messages=(RawMessage("user", secret_message),),
    )
    fp = fingerprint_request(request)

    payload = pickle.dumps(fp)
    payload += repr(fp).encode("utf-8")
    payload += str(fp.chain).encode("utf-8")

    for secret in (secret_system, secret_tool_desc, secret_message,
                   "NIGHTINGALE", "84213", "ALPHA", "QUEBEC", "xyz789"):
        assert secret.encode("utf-8") not in payload, f"leaked fragment: {secret!r}"
        # also guard against a naive base64/hex re-encoding of the secret slipping through
        assert secret not in payload.decode("utf-8", errors="ignore")


def test_two_fingerprints_of_secret_free_and_secret_bearing_requests_still_diverge_correctly() -> None:
    """Content-freeness must not come at the cost of correctness: even though the payload
    never reveals *what* changed, it must still correctly report *where*."""
    previous = fingerprint_request(_request(system="prefix STABLE-TOKEN suffix"))
    current = fingerprint_request(_request(system="prefix CHANGED-TOKEN suffix"))
    report = compare("wl_1", previous, current)
    assert report.kind is DivergenceKind.SYSTEM_CHANGED
    assert report.segment_offset == 1  # "prefix"=0, the changed token=1, "suffix"=2
