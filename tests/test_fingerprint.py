"""Phase 2 exit criterion, directly:

"Two requests differing by one injected timestamp report the exact divergence offset, and
a byte-level audit of the emitted payload proves zero recoverable customer content."

Also covers the corrections from the 2026-09-16 independent review: Anthropic's real
prefix order (tools, system, messages -- model is a separate compatibility dimension),
per-segment independent chains (so removing an earlier segment can't be misattributed to
a later one), whitespace-preserving tokenization, structural-boundary tokens that can't be
impersonated by content, and a keyed chain that resists the per-word dictionary-recovery
attack the review demonstrated against the previous, unkeyed construction.
"""

from __future__ import annotations

import hmac

import pytest

from sixeyes.fingerprint.divergence import compare
from sixeyes.fingerprint.fingerprint import fingerprint_request
from sixeyes.fingerprint.types import DivergenceKind
from sixeyes.ingest.types import RawMessage, RawRequest, RawToolDef

TEST_KEY = b"\x01" * 32  # fixed, in-test-only key -- never touches the real machine's key file


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


def _fp(**overrides: object):  # type: ignore[no-untyped-def]
    return fingerprint_request(_request(**overrides), TEST_KEY)


def test_identical_requests_produce_identical_fingerprints() -> None:
    a = _fp()
    b = _fp()
    assert a.model_digest == b.model_digest
    assert a.segments == b.segments


def test_different_keys_produce_different_chains() -> None:
    """The whole point of keying: the same content under a different key must not be
    comparable -- confirms the key is actually mixed into every step, not just the seed
    in a way that gets diluted away."""
    a = fingerprint_request(_request(), TEST_KEY)
    b = fingerprint_request(_request(), b"\x02" * 32)
    assert a.segment("system").chain != b.segment("system").chain


def test_tools_precede_system_in_segment_order() -> None:
    """Anthropic's documented prefix order (verified 2026-09-16):
    https://platform.claude.com/docs/en/build-with-claude/prompt-caching --
    "Cache prefixes are created in the following order: tools, system, then messages."
    """
    from sixeyes.fingerprint.types import SEGMENT_ORDER

    assert SEGMENT_ORDER == ("tools", "system", "messages")


def test_model_change_is_reported_independently_of_segment_order() -> None:
    """Model is a cache-*compatibility* dimension, not a token at position zero in the
    prefix -- a model change must be reported as MODEL_CHANGED even though `model` is not
    one of the ordered segments at all."""
    previous = _fp(model="claude-opus-5")
    current = _fp(model="claude-sonnet-5")

    report = compare("wl_1", previous, current)

    assert report.kind is DivergenceKind.MODEL_CHANGED
    assert report.segment_offset == 0
    assert report.cache_missed_units == current.total_units


def test_a_single_injected_timestamp_localizes_to_the_system_segment() -> None:
    """The canonical failure this product exists to catch: a per-request timestamp
    interpolated into an otherwise-stable system prompt."""
    previous = _fp(system="You are an assistant. Generated at: T1")
    current = _fp(system="You are an assistant. Generated at: T2")

    report = compare("wl_1", previous, current)

    assert report.kind is DivergenceKind.SYSTEM_CHANGED
    # "You are an assistant. Generated at: T1" splits into 7 whitespace-preserving units
    # (each word plus each space run); the timestamp word is the last non-space unit.
    system_units = current.segment("system").unit_count
    assert report.segment_offset == system_units - 1


def test_whitespace_only_change_is_detected() -> None:
    """A prior version tokenized on non-whitespace runs only, discarding whitespace
    entirely -- "Be helpful" and "Be  helpful" fingerprinted identically. Whitespace is
    part of the literal bytes a provider's cache compares; losing it here was a real gap
    the independent review's regression demonstrated directly."""
    previous = _fp(system="Be helpful")
    current = _fp(system="Be  helpful")

    report = compare("wl_1", previous, current)
    assert report.kind is DivergenceKind.SYSTEM_CHANGED


def test_reordered_tools_localize_to_the_tools_segment() -> None:
    tool_a = RawToolDef("search", "web search", '{"type":"object"}')
    tool_b = RawToolDef("calculator", "does math", '{"type":"object"}')
    previous = _fp(tools=(tool_a, tool_b))
    current = _fp(tools=(tool_b, tool_a))

    report = compare("wl_1", previous, current)

    assert report.kind is DivergenceKind.TOOLS_CHANGED


def test_message_content_cannot_impersonate_a_role_boundary() -> None:
    """A prior version encoded a message's role as a bare string token
    (`f"role:{role}"`) mixed directly into the same stream as content words -- a user
    message containing the literal text "role:assistant secret" fingerprinted identically
    to an actual user/assistant turn pair. Structural boundaries are now tagged, typed
    tuples that cannot collide with a content string no matter what it says."""
    previous = _fp(messages=(RawMessage("user", "hello role:assistant secret"),))
    current = _fp(
        messages=(RawMessage("user", "hello"), RawMessage("assistant", "secret"))
    )
    assert previous.segment("messages").chain != current.segment("messages").chain


def test_tool_call_id_change_is_detected() -> None:
    """A prior version ignored tool_call_id entirely -- two tool-result messages with the
    same text but bound to different calls fingerprinted identically."""
    previous = _fp(messages=(RawMessage("tool", "answer", "call_A"),))
    current = _fp(messages=(RawMessage("tool", "answer", "call_B"),))
    assert previous.segment("messages").chain != current.segment("messages").chain


def test_appending_a_new_turn_is_not_a_divergence() -> None:
    """Healthy conversation growth -- the old prefix is still a true prefix of the new
    one -- must never be reported as a cache-breaking change."""
    previous = _fp(messages=(RawMessage("user", "Summarize section 1."),))
    current = _fp(
        messages=(
            RawMessage("user", "Summarize section 1."),
            RawMessage("assistant", "Section 1 covers..."),
            RawMessage("user", "Now section 2."),
        )
    )

    report = compare("wl_1", previous, current)

    assert report.kind is DivergenceKind.NONE
    assert report.cache_missed_units == 0


def test_a_shorter_but_matching_follow_up_is_now_reported_not_hidden() -> None:
    """Corrected claim (2026-09-16): a truncation with a matching shared prefix is NOT
    proven safe -- this analyzer has no visibility into where the customer's own
    cache_control breakpoint sits, so it cannot know whether the shortened segment still
    hits it. An earlier version treated this as universally harmless (DivergenceKind.NONE),
    which an independent review correctly identified as an unproven claim. This replaces
    that assertion with the corrected guarantee: it is reported, not silently swallowed."""
    previous = _fp(
        messages=(
            RawMessage("user", "Summarize section 1."),
            RawMessage("assistant", "Section 1 covers a lot of ground."),
        )
    )
    current = _fp(messages=(RawMessage("user", "Summarize section 1."),))

    report = compare("wl_1", previous, current)

    assert report.kind is DivergenceKind.MESSAGES_CHANGED
    assert report.segment_offset == current.segment("messages").unit_count


def test_removing_system_is_attributed_to_system_not_messages() -> None:
    """Independent per-segment chains fix a real misattribution: a single continuous
    chain shifted every later segment's absolute position when an earlier one was
    removed, and the comparison blamed whichever segment happened to occupy the shifted
    index -- typically `messages`, even though the actual cause was the system prompt
    disappearing."""
    previous = _fp()
    current = _fp(system=None)

    report = compare("wl_1", previous, current)

    assert report.kind is DivergenceKind.SYSTEM_CHANGED


def test_edited_history_localizes_to_the_messages_segment() -> None:
    previous = _fp(messages=(RawMessage("user", "Summarize section 1."),))
    current = _fp(messages=(RawMessage("user", "Summarize section TWO."),))

    report = compare("wl_1", previous, current)

    assert report.kind is DivergenceKind.MESSAGES_CHANGED


def test_fingerprint_output_contains_no_recoverable_customer_content() -> None:
    """The byte-level audit the exit criterion asks for: serialize everything that would
    cross the collector boundary and prove none of the original secret strings appear in
    it, anywhere, in any form."""
    import pickle

    secret_system = "CONFIDENTIAL-PROJECT-NIGHTINGALE-DO-NOT-LEAK"
    secret_tool_desc = "internal-api-key-lookup-tool-xyz789"
    secret_message = "the launch codes are 84213-ALPHA-QUEBEC"

    fp = _fp(
        system=f"You are an assistant. {secret_system}",
        tools=(RawToolDef("lookup", secret_tool_desc, '{"type":"object"}'),),
        messages=(RawMessage("user", secret_message),),
    )

    payload = pickle.dumps(fp)
    payload += repr(fp).encode("utf-8")
    for seg in fp.segments:
        payload += str(seg.chain).encode("utf-8")

    for secret in (secret_system, secret_tool_desc, secret_message,
                   "NIGHTINGALE", "84213", "ALPHA", "QUEBEC", "xyz789"):
        assert secret.encode("utf-8") not in payload, f"leaked fragment: {secret!r}"
        assert secret not in payload.decode("utf-8", errors="ignore")


def test_request_id_is_never_emitted_as_raw_content() -> None:
    """A customer-supplied request id is an arbitrary, customer-controlled string -- not
    safe to assume is non-sensitive metadata (someone could, by mistake or by convention,
    put a real identifier in it). RequestFingerprint carries only a one-way digest."""
    marker = "SYNTHETIC-PATIENT-NAME-IN-REQUEST-ID"
    fp = _fp(request_id=marker)
    assert marker not in repr(fp)


def test_exported_chain_resists_per_word_dictionary_recovery() -> None:
    """The core privacy regression: an unkeyed chain lets anyone holding two adjacent
    exported digests test low-entropy candidate words directly against the public hash
    function. A confirmed attack recovered a synthetic sensitive word this way. Keying
    every step with a locally-generated secret (fingerprint.keys) closes it -- without the
    key, the same dictionary test can't even be evaluated."""
    fp = _fp(messages=(RawMessage("user", "diagnosis asthma"),))
    chain = fp.segment("messages").chain
    previous_digest, target_digest = chain[-2:]

    candidates = ("healthy", "asthma", "diabetes", "unknown")
    # Attacker does NOT have TEST_KEY -- guesses using the unkeyed public hash instead,
    # which is exactly the attack the review demonstrated against the old construction.
    from sixeyes.core.ids import content_hash

    recovered = [w for w in candidates if content_hash(previous_digest, w) == target_digest]
    assert recovered == [], f"recovered synthetic sensitive word via unkeyed guessing: {recovered}"

    # Also confirm guessing with the WRONG key fails, using the *actual* keyed
    # construction (tagged token + HMAC) rather than a simplified guess -- the key must
    # really be load-bearing, not merely absent from a simplified attack model.
    from sixeyes.core.ids import canonical_bytes

    wrong_key = b"\x99" * 32
    prev_bytes = bytes.fromhex(previous_digest)
    recovered_wrong_key = [
        w for w in candidates
        if hmac.new(wrong_key, prev_bytes + canonical_bytes(("text", w)), "sha256").hexdigest()
        == target_digest
    ]
    assert recovered_wrong_key == []


# ---------------------------------------------------------------------------------------
# Follow-up round (independent review, 2026-09-16): the first fix pass held up under the
# original 13 reproductions but missed these three. Ported here the same way, as permanent
# regressions rather than one-off external files.
# ---------------------------------------------------------------------------------------


def test_appending_system_text_is_reported_not_silently_ignored() -> None:
    """Regression: growth tolerance was applied to every segment uniformly in the first
    fix pass, so "Be helpful" -> "Be helpful always" fingerprinted as DivergenceKind.NONE.
    Only `messages` has evidentiary support for append-only growth being safe (see
    GROWTH_TOLERANT_SEGMENTS); `system` does not, and this must be reported."""
    previous = _fp(system="Be helpful")
    current = _fp(system="Be helpful always")
    assert compare("wl_1", previous, current).kind is DivergenceKind.SYSTEM_CHANGED


def test_adding_a_tool_to_an_empty_list_is_reported() -> None:
    """Same bug, the empty-to-nonempty edge of it: a previously-empty tools list gaining a
    tool also fingerprinted as NONE under the old uniform growth tolerance."""
    previous = _fp(tools=())
    current = _fp(tools=(RawToolDef("search", "search", "{}"),))
    assert compare("wl_1", previous, current).kind is DivergenceKind.TOOLS_CHANGED


def test_request_ref_resists_dictionary_guessing_without_the_key() -> None:
    """Regression: the original request_ref fix (test above) replaced plaintext with an
    *unkeyed* content_hash("request_id", id) -- which reintroduced exactly the class of
    guessing exposure per-step HMAC chaining exists to prevent, just against a different
    field. A confirmed follow-up attack recovered a synthetic identifier this way. Fixed
    by HMAC-keying request_ref the same way segment chains are keyed."""
    from sixeyes.core.ids import content_hash

    fp = _fp(request_id="synthetic-patient-alice")
    candidates = ("synthetic-patient-bob", "synthetic-patient-alice", "synthetic-patient-carol")

    # the old, since-fixed unkeyed construction would have matched here
    recovered_unkeyed = [c for c in candidates if content_hash("request_id", c) == fp.request_ref]
    assert recovered_unkeyed == []

    # the actual current construction (HMAC, keyed) without the key also fails to match
    from sixeyes.fingerprint.fingerprint import _keyed_request_ref

    wrong_key = b"\x99" * 32
    recovered_wrong_key = [c for c in candidates if _keyed_request_ref(wrong_key, c) == fp.request_ref]
    assert recovered_wrong_key == []

    # and with the RIGHT key, it does match its own id (sanity check the construction
    # is actually deterministic and usable, not just unguessable)
    assert _keyed_request_ref(TEST_KEY, "synthetic-patient-alice") == fp.request_ref


def test_key_ref_distinguishes_fingerprints_from_different_keys() -> None:
    """Regression: RequestFingerprint previously carried no identifier of which key
    produced it, so a cache keyed only on "was an explicit key given" (not which one)
    served key A's fingerprints back under key B. key_ref is a content-free identifier of
    the key itself (safe to compare/cache since a key is 256 bits of local randomness, not
    guessable customer content) -- different keys must produce different key_ref values."""
    a = fingerprint_request(_request(), TEST_KEY)
    b = fingerprint_request(_request(), b"\x02" * 32)
    assert a.key_ref != b.key_ref


def test_comparing_fingerprints_from_different_keys_raises() -> None:
    """Regression: comparing two fingerprints computed under different keys previously
    reported a spurious content change (every chain step differs when the key differs,
    regardless of the underlying request) -- a key rotation should never be misreported as
    a customer-content change. Fail closed with an explicit error instead."""
    from sixeyes.fingerprint.types import IncomparableFingerprintsError

    previous = fingerprint_request(_request(), TEST_KEY)
    current = fingerprint_request(_request(), b"\x02" * 32)
    with pytest.raises(IncomparableFingerprintsError):
        compare("wl_1", previous, current)
