from app.generation import ABSTAIN_TEXT, verify_citations
from app.ingest import chunk_document
from app.retrieval import BM25, Hit, HashingEmbedder, tokenize


def test_chunker_respects_headings_and_size():
    body = "# Doc\n\n## Alpha\n\n" + " ".join(f"Sentence number {i} is here." for i in range(80)) + \
           "\n\n## Beta\n\nShort section."
    chunks = chunk_document(body, "Doc", max_words=60, overlap_words=10)
    assert all(len(c.text.split()) <= 70 for c in chunks)
    assert {c.heading for c in chunks} == {"Doc > Alpha", "Doc > Beta"}
    assert chunks[-1].text == "Short section."
    # consecutive chunks in a section share a sentence-aligned overlap
    a, b = chunks[0].text, chunks[1].text
    last_sentence = a.rsplit(". ", 1)[-1]
    assert b.startswith(" ".join(a.split()[-10:])) and last_sentence in b


def test_bm25_prefers_rare_matching_terms():
    docs = [tokenize(t) for t in ["the parental leave policy is generous",
                                  "the travel policy covers hotels",
                                  "policy policy policy"]]
    scores = BM25(docs).scores(tokenize("parental leave"))
    assert scores.argmax() == 0 and scores[2] == 0


def test_hashing_embedder_is_normalised_and_similarity_ordered():
    e = HashingEmbedder()
    v = e.embed(["hotel nightly cap in London", "nightly hotel limit London", "parental leave weeks"], "document")
    assert abs(float((v[0] ** 2).sum()) - 1) < 1e-5
    assert v[0] @ v[1] > v[0] @ v[2]


def _hit(i, text):
    return Hit(chunk_id=i, doc_id=i, title=f"T{i}", heading="H", text=text, classification="internal", score=1)


def test_citation_verification_scores_support():
    hits = [_hit(1, "Employees receive 25 days of paid time off."), _hit(2, "Hotels are capped at $250.")]
    cites, g = verify_citations("Employees receive 25 days of paid time off [S1].", hits)
    assert [c["ref"] for c in cites] == ["S1"] and g == 1.0
    _, g_bad = verify_citations("Employees receive 25 days of paid time off [S2].", hits)
    assert g_bad < 0.3
    _, g_uncited = verify_citations("Employees get unlimited vacation.", hits)
    assert g_uncited == 0.0
    cites, g_none = verify_citations(ABSTAIN_TEXT, hits)
    assert cites == [] and g_none is None


def test_out_of_range_citations_are_dropped():
    cites, _ = verify_citations("Fact here [S7].", [_hit(1, "Fact here.")])
    assert cites == []
