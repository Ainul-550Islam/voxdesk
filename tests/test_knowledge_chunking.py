"""
Chunking behaviour.

Determinism is the headline property here. Reindexing an unchanged document
must produce byte-identical chunks, because that is what makes a retried job
idempotent instead of a source of duplicate embeddings.
"""
from __future__ import annotations

from app.knowledge.chunking import ChunkingConfig, chunk_sections, chunk_text
from app.knowledge.extractors import Section

SMALL = ChunkingConfig(size=300, overlap=60, min_size=40)


def _body(sentence: str, times: int) -> str:
    return (sentence + " ") * times


def test_short_text_is_a_single_chunk():
    chunks = chunk_text("We open at nine.", SMALL)
    assert len(chunks) == 1
    assert chunks[0].index == 0
    assert chunks[0].char_count == len("We open at nine.")


def test_long_text_is_split_and_indexed_from_zero():
    chunks = chunk_text(_body("Refunds are issued within fourteen days.", 40), SMALL)
    assert len(chunks) > 1
    assert [c.index for c in chunks] == list(range(len(chunks)))
    assert all(c.char_count <= SMALL.size for c in chunks)


def test_chunking_is_deterministic():
    text = _body("A standard cleaning costs one hundred twenty dollars.", 30)
    first = [c.text for c in chunk_text(text, SMALL)]
    second = [c.text for c in chunk_text(text, SMALL)]
    assert first == second


def test_consecutive_chunks_overlap():
    """
    Overlap keeps a fact that straddles a boundary retrievable from at least
    one chunk. Without it, "we refund within | fourteen days" answers nothing.
    """
    chunks = chunk_text(_body("Refunds are issued within fourteen days.", 40), SMALL)
    tail = chunks[0].text[-30:].strip()
    assert tail and tail in chunks[1].text


def test_zero_overlap_is_honoured():
    """
    With no overlap the chunks partition the text: total length back out is
    the length that went in, give or take stripped whitespace. Repeated
    filler would make a substring check meaningless here, so this measures
    duplication directly.
    """
    config = ChunkingConfig(size=300, overlap=0, min_size=40)
    text = " ".join(f"Sentence number {n} about refunds." for n in range(60))
    chunks = chunk_text(text, config)

    assert len(chunks) > 1
    total = sum(c.char_count for c in chunks)
    assert total <= len(text)
    # Every sentence appears exactly once across the whole set.
    joined = " ".join(c.text for c in chunks)
    assert joined.count("Sentence number 30 ") == 1


def test_overlap_cannot_exceed_half_the_chunk_size():
    """A pathological config must degrade, not loop forever."""
    config = ChunkingConfig(size=300, overlap=9999, min_size=40).validated()
    assert config.overlap == 150
    assert len(chunk_text(_body("Some text here.", 50), config)) > 1


def test_section_metadata_is_carried_onto_every_chunk():
    sections = [
        Section(text=_body("Refund details.", 40), metadata={"page": 7}),
        Section(text="Parking is free behind the building.", metadata={"page": 8}),
    ]
    chunks = chunk_sections(sections, SMALL)
    assert {c.metadata["page"] for c in chunks} == {7, 8}


def test_split_sections_record_which_part_they_are():
    sections = [Section(text=_body("Refund details here.", 40), metadata={"page": 2})]
    chunks = chunk_sections(sections, SMALL)
    assert chunks[0].metadata["part"] == 1
    assert chunks[0].metadata["part_count"] == len(chunks)


def test_chunks_never_span_two_sections():
    """
    A chunk must have one honest answer to "where is this from?".

    If page 3 and page 4 could share a chunk, its citation would be a lie.
    """
    sections = [
        Section(text="Alpha content on page one.", metadata={"page": 1}),
        Section(text="Beta content on page two.", metadata={"page": 2}),
    ]
    for chunk in chunk_sections(sections, SMALL):
        assert not ("Alpha" in chunk.text and "Beta" in chunk.text)


def test_a_heading_is_never_stranded_at_the_end_of_a_chunk():
    """
    A chunk ending "## Pricing" embeds the title away from the prices, so the
    heading is pulled forward to travel with its body.
    """
    text = "A" * 280 + "\n\n## Pricing\n\n" + _body("Cleaning costs 120 dollars.", 10)
    chunks = chunk_sections([Section(text=text, metadata={})], SMALL)
    assert not any(c.text.rstrip().endswith("## Pricing") for c in chunks)
    assert any("## Pricing" in c.text and "120 dollars" in c.text for c in chunks)


def test_short_real_content_is_not_dropped():
    """
    Regression: an over-eager "drop tiny sections" rule silently discarded a
    PDF page reading "Parking is free in the lot behind our building." Short
    is not the same as worthless -- that is exactly the fact callers ring for.
    """
    sections = [
        Section(text=_body("Long refund explanation.", 30), metadata={"page": 1}),
        Section(text="Parking is free behind the building.", metadata={"page": 2}),
    ]
    chunks = chunk_sections(sections, SMALL)
    assert any("Parking is free" in c.text for c in chunks)


def test_a_bare_heading_section_is_dropped():
    sections = [
        Section(text="## Contents", metadata={"page": 1}),
        Section(text=_body("Actual policy text.", 20), metadata={"page": 2}),
    ]
    chunks = chunk_sections(sections, SMALL)
    assert all(c.text.strip() != "## Contents" for c in chunks)


def test_empty_and_whitespace_input_produce_no_chunks():
    assert chunk_text("", SMALL) == []
    assert chunk_text("   \n\n  ", SMALL) == []


def test_text_with_no_whitespace_still_terminates():
    """A minified blob has no clean boundary; it must still chunk, not hang."""
    chunks = chunk_text("x" * 2000, SMALL)
    assert len(chunks) > 1
    assert "".join(c.text for c in chunks).count("x") >= 2000


def test_token_estimate_is_recorded():
    chunk = chunk_text("Cleaning costs one hundred twenty dollars.", SMALL)[0]
    assert chunk.token_estimate > 0
    assert chunk.token_estimate <= chunk.char_count


def test_config_from_settings_reads_the_real_setting_names():
    """
    Guards a silent misconfiguration: a typo here means every deployment
    quietly uses the dataclass defaults instead of its own tuning.
    """
    from app.core.config import settings
    from app.knowledge.chunking import config_from_settings

    config = config_from_settings(settings)
    assert config.size == settings.knowledge_chunk_chars
    assert config.overlap == settings.knowledge_chunk_overlap_chars
    assert config.min_size == settings.knowledge_min_chunk_chars
