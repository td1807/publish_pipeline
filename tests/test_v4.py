"""The claims this package makes, as assertions.

Runs on the fast `lexical` embedder by default so the suite needs no model
download and finishes in seconds. The two tests that are genuinely *about* the
real model are marked `semantic` and skip unless it is installed:

    .venv/bin/pytest tests/test_v4.py -q
    .venv/bin/pytest tests/test_v4.py -q -m semantic
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from ..beckn import validate
from ..beckn.catalog import build_catalog
from ..beckn.envelope import build_envelope
from ..config import DATA_DIR, EVIDENCE_DIR
from ..scenario1 import repair_encoding
from ..ingest.language import check_devanagari_encoding, detect, repair_devanagari, score_terms
from ..ingest.passages import UnknownState, _split_on_crop_change, detect_state, extract
from ..ingest.document_text import Document, Page, UnusableDocument, read_document
from ..network_node import NetworkNode
from ..publish import publish
from ..taxonomy.ids import point_id_for, resource_id_for
from ..taxonomy.vocab import load_vocabulary
from ..vectors.embeddings import get_embedder
from ..vectors.store import VectorIndex

KARNATAKA = DATA_DIR / "imd_karnataka_agromet.pdf"
UP = DATA_DIR / "imd_up_agromet.pdf"
RAJASTHAN = DATA_DIR / "imd_rajasthan_agromet.pdf"
SCANNED = DATA_DIR / "imd_karnataka_district_kannada.pdf"

# Real advisory sentences from the Karnataka bulletin. If any of these ever
# appear in a publish payload, the catalogue has started carrying prose.
ADVISORY_NEEDLES = ("NAA", "moisture stress", "ml/litre", "Spraying of 1%")


@pytest.fixture(scope="module")
def vocab():
    return load_vocabulary()


@pytest.fixture(scope="module")
def karnataka(vocab):
    doc = read_document(KARNATAKA)
    passages, report = extract(doc, vocab=vocab)
    return doc, passages, report


@pytest.fixture(scope="module")
def envelope(vocab):
    catalogs = []
    for path in (KARNATAKA, UP, RAJASTHAN):
        doc = read_document(path)
        passages, _ = extract(doc, vocab=vocab)
        code = detect_state(doc, vocab)
        catalogs.append(
            build_catalog(
                passages, state_code=code, state_name=vocab.state(code).name, vocab=vocab
            )
        )
    return build_envelope(catalogs)


@pytest.fixture(scope="module")
def index(tmp_path_factory, vocab):
    """A lexical index over all three bulletins — fast, no model needed."""
    idx = VectorIndex(
        get_embedder("lexical"),
        collection="test_v4",
        path=str(tmp_path_factory.mktemp("qdrant_lexical")),
    )
    idx.ensure_collection(recreate=True)
    all_passages = []
    for path in (KARNATAKA, UP, RAJASTHAN):
        passages, _ = extract(read_document(path), vocab=vocab)
        idx.index_passages(passages)
        all_passages.extend(passages)
    yield idx, all_passages
    idx.close()


# --- 1. envelope conformance to message_update.json --------------------------


def _shape(node, depth=0):
    """Structural skeleton: keys and container types, values discarded."""
    if isinstance(node, dict):
        return {k: _shape(v, depth + 1) for k, v in sorted(node.items())}
    if isinstance(node, list):
        return [_shape(node[0], depth + 1)] if node else []
    return type(node).__name__


def test_envelope_matches_message_update_shape(envelope):
    """Our payload has the same structure as the supplied message_update.json."""
    reference = json.loads(
        (EVIDENCE_DIR / "message_update.reference.json").read_text(encoding="utf-8")
    )
    ours = envelope.to_wire()

    assert set(ours) == set(reference) == {"context", "message"}
    # context: every key the reference carries, we carry too.
    assert set(reference["context"]) <= set(ours["context"]), (
        f"missing context keys: {set(reference['context']) - set(ours['context'])}"
    )
    assert ours["context"]["action"] == reference["context"]["action"] == "publish"
    assert ours["context"]["version"] == reference["context"]["version"] == "2.0.0"
    assert isinstance(ours["context"]["schemaContext"], list)

    assert set(reference["message"]) <= set(ours["message"])
    ref_cat, our_cat = reference["message"]["catalogs"][0], ours["message"]["catalogs"][0]
    # We omit provider.availableAt deliberately (no surveyed geometry); every
    # other catalogue-level key in the reference must be present.
    expected = set(ref_cat) - {"resources"}
    assert expected <= set(our_cat), f"missing catalog keys: {expected - set(our_cat)}"

    ref_res, our_res = ref_cat["resources"][0], our_cat["resources"][0]
    assert set(ref_res) <= set(our_res)
    ref_attrs, our_attrs = ref_res["resourceAttributes"], our_res["resourceAttributes"]
    for required in ("@context", "@type", "subjectCategories", "languages", "coverageAreas"):
        assert required in our_attrs, f"resourceAttributes missing {required}"
        assert required in ref_attrs

    ref_dir, our_dir = (
        reference["message"]["publishDirectives"][0],
        ours["message"]["publishDirectives"][0],
    )
    assert set(ref_dir) <= set(our_dir)


# --- 2. Beckn spec validation ------------------------------------------------


def test_payload_is_spec_valid(envelope, vocab):
    report = validate.validate_envelope(envelope, vocab=vocab)
    assert report.ok, report.errors[:10]
    assert report.resources > 0
    assert report.checked_attribute_objects == report.resources


def test_every_attributes_object_has_jsonld_keys(envelope):
    for cat in envelope.message.catalogs:
        for res in cat.resources:
            attrs = res.resourceAttributes
            assert attrs is not None
            assert attrs.context.startswith("https://")
            assert attrs.type.startswith("openagrinet:")


def test_capability_type_governs_attribute_groups(envelope):
    """A crop resource carries no weatherParameters, and vice versa."""
    for cat in envelope.message.catalogs:
        for res in cat.resources:
            payload = res.resourceAttributes.model_dump(by_alias=True, exclude_none=True)
            cap = res.resourceAttributes.type.split(":")[-1]
            if cap == "WeatherAdvisoryCapability":
                assert "agricultureSubjects" not in payload, res.id
            else:
                assert "weatherParameters" not in payload, res.id


def test_subject_categories_agree_with_subjects_carried(envelope):
    """A resource must declare every subject type it actually carries.

    Regression guard: livestock advisories in these bulletins also name fodder
    crops, so they carry Crop-typed subjects. Declaring only ["Livestock"] made
    the object self-contradictory and a consumer filtering on "Crop" would skip
    a resource that does contain crops.
    """
    for cat in envelope.message.catalogs:
        for res in cat.resources:
            payload = res.resourceAttributes.model_dump(by_alias=True, exclude_none=True)
            declared = set(payload.get("subjectCategories", []))
            present = {
                s["subjectType"] for s in payload.get("agricultureSubjects", [])
            }
            assert present <= declared, (
                f"{res.id} carries {sorted(present - declared)} but declares "
                f"{sorted(declared)}"
            )


def test_per_document_resources_json_is_self_contained(vocab):
    """Each per-PDF file must carry that document's resources and nothing else."""
    from ..scenario1 import Onboarding  # noqa: F401  (documents the shape)

    doc = read_document(RAJASTHAN)
    passages, report = extract(doc, vocab=vocab)
    code = detect_state(doc, vocab)
    catalog = build_catalog(
        passages, state_code=code, state_name=vocab.state(code).name, vocab=vocab
    )

    # Build the same payload run_scenario1 writes, without needing an index.
    payload = {
        "document": doc.path.name,
        "state": {"code": code, "name": vocab.state(code).name},
        "resources": [
            {"id": r.id, "resourceAttributes": r.resourceAttributes.model_dump(
                by_alias=True, exclude_none=True)}
            for r in catalog.resources
        ],
    }
    assert payload["document"] == "imd_rajasthan_agromet.pdf"
    assert payload["resources"]
    # Every resource in this file belongs to this state.
    for r in payload["resources"]:
        codes = {a["areaCode"] for a in r["resourceAttributes"]["coverageAreas"]}
        assert any(c.startswith(code) for c in codes), r["id"]
    # And carries no text from the source document.
    body = json.dumps(payload, ensure_ascii=False)
    for needle in ADVISORY_NEEDLES:
        assert needle not in body


def test_invalid_payload_is_refused(envelope, vocab):
    """Injecting prose into resourceAttributes must fail validation."""
    import copy

    broken = copy.deepcopy(envelope)
    attrs = broken.message.catalogs[0].resources[0].resourceAttributes
    setattr(attrs, "note", "Spraying of 1% KNO3 @ 4 ml/litre to overcome moisture stress")
    report = validate.validate_envelope(broken, vocab=vocab)
    assert not report.ok
    assert any("prose" in e for e in report.errors)

    with pytest.raises(validate.InvalidPayload):
        validate.assert_valid(broken, vocab=vocab)


# --- 3. the catalogue carries no advisory prose ------------------------------


def test_no_advisory_text_in_payload(envelope):
    body = json.dumps(envelope.to_wire(), ensure_ascii=False)
    for needle in ADVISORY_NEEDLES:
        assert needle not in body, f"advisory text {needle!r} leaked into the catalogue"


def test_network_layer_holds_attributes_but_not_text(envelope):
    result, node = publish(envelope)
    assert result.ack.status == "ACCEPTED"
    assert result.ack.resources_indexed == sum(
        len(c.resources) for c in envelope.message.catalogs
    )
    for needle in ADVISORY_NEEDLES:
        assert not node.contains_text(needle)
    facets = node.facet_index()
    assert facets["subject_uris"] > 0 and facets["area_codes"] > 0


# --- 4. facet parity: 2a and 2b cannot drift ---------------------------------


def test_facet_parity(index, envelope):
    """What 2b stored per passage is exactly what 2a published for it."""
    idx, passages = index
    published = {
        res.id: res.resourceAttributes.model_dump(by_alias=True, exclude_none=True)
        for cat in envelope.message.catalogs
        for res in cat.resources
    }

    checked = 0
    for passage in passages[:120]:
        stored = idx.get_payload(passage.point_id)
        assert stored is not None, passage.point_id
        # the stored facets are literally Passage.facets()
        for key, value in passage.facets().items():
            assert stored[key] == value, f"{key} drifted for {passage.citation}"

        attrs = published[passage.resource_id]
        area_codes = {a["areaCode"] for a in attrs["coverageAreas"]}
        assert stored["area_code"] in area_codes
        assert stored["language"] in attrs["languages"]
        for uri in stored["subject_uris"]:
            if "agricultureSubjects" in attrs:
                assert uri in {s["subjectId"] for s in attrs["agricultureSubjects"]}
        checked += 1
    assert checked > 0


# --- 5. id stability ---------------------------------------------------------


def test_resource_id_is_stable_and_document_independent():
    a = resource_id_for("prov-imd-agromet", "agriculture", "Crop", "IN-KA-KOPPAL")
    b = resource_id_for("prov-imd-agromet", "agriculture", "Crop", "IN-KA-KOPPAL")
    assert a == b == "res-imd-agromet-agriculture-crop-in-ka-koppal"
    # Nothing about the bulletin issue appears in the id, so a reissue updates.
    assert "69" not in a and "2026" not in a


def test_reingest_is_idempotent(index, vocab):
    idx, passages = index
    before = idx.count()
    idx.index_passages(passages)
    assert idx.count() == before, "re-onboarding duplicated points"


def test_point_id_is_deterministic():
    a = point_id_for("prov-imd-agromet", "x.pdf", 14, 3)
    b = point_id_for("prov-imd-agromet", "x.pdf", 14, 3)
    assert a == b
    assert a != point_id_for("prov-imd-agromet", "x.pdf", 14, 4)


def test_extraction_is_deterministic(vocab):
    doc = read_document(RAJASTHAN)
    first, _ = extract(doc, vocab=vocab)
    second, _ = extract(doc, vocab=vocab)
    assert [p.resource_id for p in first] == [p.resource_id for p in second]
    assert [p.point_id for p in first] == [p.point_id for p in second]


# --- 6. vector count and dimensions -----------------------------------------


def test_every_passage_has_one_vector_of_the_right_size(index):
    idx, passages = index
    assert idx.count() == len(passages)
    vec = idx.vector_of(passages[0].point_id)
    assert vec is not None and len(vec) == idx.embedder.dim == 1024


# --- 7. retrieval actually works --------------------------------------------


def test_retrieval_returns_provenance(index):
    idx, _ = index
    hits = idx.search("pigeon pea flowering", limit=5)
    assert hits
    top = hits[0]
    assert top.document.endswith(".pdf")
    assert top.page >= 1
    assert top.resource_id.startswith("res-")


def test_retrieval_can_be_scoped_to_advertised_resources(index, envelope):
    """The follow-up leg searches only inside what discovery handed back."""
    idx, _ = index
    node = NetworkNode()
    node.publish(envelope)
    vocab = load_vocabulary()

    matches = node.discover(
        subject_uri=vocab.subject_by_slug["red-gram"].uri,
        area_code="IN-KA-KOPPAL",
    )
    assert matches, "discovery found no resource for red gram in Koppal"
    ids = [m.resource_id for m in matches]

    hits = idx.search("flowering", resource_ids=ids, limit=5)
    assert hits
    assert all(h.resource_id in ids for h in hits)


@pytest.mark.semantic
def test_real_model_matches_across_language_and_paraphrase(tmp_path):
    """The claim that this is semantic, not lexical. Needs the real model."""
    pytest.importorskip("sentence_transformers")

    vocab = load_vocabulary()
    passages, _ = extract(read_document(KARNATAKA), vocab=vocab)
    idx = VectorIndex(
        get_embedder("local"),
        collection="semantic_test",
        path=str(tmp_path / "qdrant_semantic"),
    )
    idx.ensure_collection(recreate=True)
    idx.index_passages(passages)
    try:
        # "tur" never appears; the bulletin says "Redgram"/"Pigeon pea".
        hits = idx.search("my tur crop flowers are dropping", limit=5)
        assert hits and hits[0].semantic
        joined = " ".join(h.text.lower() for h in hits)
        assert "pigeon pea" in joined or "redgram" in joined or "red gram" in joined
    finally:
        idx.close()


# --- 8. language detection ---------------------------------------------------


def test_language_detection_per_script():
    assert detect("Light to moderate rain likely over Belagavi district").language == "en"
    assert detect("धान की फसल में सिंचाई करें और खरपतवार निकालें").language == "hi"
    assert detect("ಭತ್ತದ ಬೆಳೆಗೆ ನೀರಾವರಿ ಮಾಡಿ").language == "kn"
    # A stray glyph must not flip an English page.
    assert detect("Rainfall warning for Koppal ॥ 30-40 Kmph winds expected").language == "en"


def test_devanagari_ambiguity_is_flagged_not_guessed():
    reading = detect("धान की फसल में सिंचाई करें")
    assert reading.language == "hi"
    assert reading.ambiguous, "Devanagari is shared with Marathi; must be flagged"
    assert "mr" in reading.siblings


def test_state_language_profiles_differ(vocab):
    """The three bulletins are genuinely different language mixes."""
    profiles = {}
    for path in (KARNATAKA, UP, RAJASTHAN):
        passages, report = extract(read_document(path), vocab=vocab)
        profiles[path.name] = report.languages

    ka = profiles["imd_karnataka_agromet.pdf"]
    up = profiles["imd_up_agromet.pdf"]
    rj = profiles["imd_rajasthan_agromet.pdf"]
    assert ka.get("en", 0) > 10 * ka.get("hi", 1)      # Karnataka: English
    assert up.get("hi", 0) > 0 and up.get("en", 0) > 0  # UP: genuinely bilingual
    assert rj.get("hi", 0) > rj.get("en", 0)            # Rajasthan: Hindi-dominant


def test_devanagari_encoding_is_measured_not_assumed():
    doc = read_document(UP)
    full = "\n".join(p.text for p in doc.pages)
    reading = check_devanagari_encoding(full)
    assert reading.devanagari_chars > 1000
    # Proper Unicode fonts (Nirmala UI / Mangal), so function-word density is
    # high. This test is what would catch a genuinely legacy-font file.
    assert reading.healthy
    assert reading.markers_per_1k > 10


# --- 9. the refusal path -----------------------------------------------------


def test_scanned_pdf_is_refused(vocab):
    with pytest.raises(UnusableDocument) as exc:
        read_document(SCANNED)
    assert "unusable" in str(exc.value).lower()
    assert "ocr" in str(exc.value).lower()


def test_refused_document_publishes_nothing(vocab):
    """A refusal must stop before the catalogue, not produce an empty one."""
    with pytest.raises(UnusableDocument):
        doc = read_document(SCANNED)
        extract(doc, vocab=vocab)


def test_document_from_an_uncovered_state_is_refused_not_crashed(vocab):
    """An out-of-scope state must refuse the same way a scan does.

    The vocabulary covers three states. A bulletin from anywhere else cannot be
    given an area code without inventing a coverage claim, so it is refused --
    but as a refusal the caller can catch and narrate, not as an exception that
    ends a multi-document run partway through.
    """
    doc = Document(
        path=Path("kerala_prices.pdf"),
        pages=(Page(number=1, text="Government of Kerala PRICE BULLETIN"),),
        extractor="test",
    )
    with pytest.raises(UnknownState) as exc:
        detect_state(doc, vocab)
    assert "coverage" in str(exc.value)
    assert isinstance(exc.value, UnusableDocument)


# --- 8b. chunking on crop boundaries -----------------------------------------


def test_advice_for_two_crops_is_not_one_passage(vocab):
    """An advisory table runs one crop into the next with no blank line."""
    block = (
        "भिंडी की फसल में पीला मोजेक रोग के प्रकोप की संभावना रहती है। "
        "रोकथाम के लिए मैलाथियान 50 ईसी 1.0 मिलीलीटर प्रति लीटर पानी की दर से छिड़काव करें।\n"
        "टमाटर, मिर्च एवं बैंगन के फलों की नियमित तुड़ाई करें। "
        "फल छेदक एवं फल मक्खी के प्रकोप की नियमित निगरानी करें।"
    )
    runs = _split_on_crop_change(block, vocab)
    assert len(runs) == 2, "okra advice and tomato advice must not share a passage"
    assert "भिंडी" in runs[0] and "टमाटर" not in runs[0]
    assert "टमाटर" in runs[1]


def test_a_line_naming_no_crop_stays_with_its_run(vocab):
    """Table continuation lines rarely repeat the crop, so they must not split."""
    block = (
        "बाजरा की फसल को अरगट रोग से बचाने के लिए जाइनेब 2.5 किलोग्राम प्रति हेक्टेयर का प्रयोग करें।\n"
        "छिड़काव सुबह या शाम के समय करें तथा वर्षा की संभावना होने पर न करें।"
    )
    assert len(_split_on_crop_change(block, vocab)) == 1


def test_splitting_never_drops_text(vocab):
    """Every character of a block survives the split, minus line breaks."""
    doc = read_document(DATA_DIR / "imd_rajasthan_agromet.pdf")
    for page in doc.pages[:6]:
        for block in [b.strip() for b in page.text.split("\n\n") if b.strip()]:
            joined = "".join(_split_on_crop_change(block, vocab)).replace("\n", "")
            assert joined == "".join(l.strip() for l in block.split("\n") if l.strip())


# --- 9a. repairing a mis-mapped Devanagari font ------------------------------


def test_repair_recovers_crop_and_district_names():
    """The defect, at word level: two glyphs swapped, the i-matra drawn first."""
    assert repair_devanagari("बसरोही") == "सिरोही"      # Sirohi, a district
    assert repair_devanagari("िैंगन") == "बैंगन"        # brinjal
    assert repair_devanagari("बभींड्ी") == "भिंडी"      # okra


def test_repair_is_applied_to_the_document_that_needs_it(vocab):
    doc = read_document(DATA_DIR / "imd_rajasthan_agromet.pdf")
    repaired, reading = repair_encoding(doc, vocab)
    assert reading.applied
    assert reading.recovered > 0
    text = "\n".join(p.text for p in repaired.pages)
    for term in ("भिंडी", "बैंगन", "सिरोही", "बाजरा"):
        assert term in text, f"{term} should be readable after the repair"


def test_repair_is_refused_on_a_document_it_would_damage(vocab):
    """The UP bulletin is broken differently. The same transform loses 43 of its
    121 recognisable terms, so the scoring gate must reject it and leave the
    document untouched."""
    doc = read_document(DATA_DIR / "imd_up_agromet.pdf")
    repaired, reading = repair_encoding(doc, vocab)
    assert not reading.applied
    assert reading.terms_after < reading.terms_before
    assert repaired is doc, "a rejected repair must not alter the document"


def test_repair_widens_what_the_catalogue_advertises(vocab):
    """The point of the repair: coverage the provider always had, now claimable."""
    doc = read_document(DATA_DIR / "imd_rajasthan_agromet.pdf")
    before, _ = extract(doc, vocab=vocab)
    repaired, _ = repair_encoding(doc, vocab)
    after, _ = extract(repaired, vocab=vocab)

    crops_before = {s.name for p in before for s in p.subjects}
    crops_after = {s.name for p in after for s in p.subjects}
    assert crops_before < crops_after, "the repair must not lose a subject"
    assert {"Okra", "Brinjal"} <= crops_after


# --- 9b. formats other than PDF ----------------------------------------------
#
# Nothing downstream of ingest knows what the file was: it consumes pages of
# text with numbers on them. These two tests pin both ends of that -- a format
# MuPDF reads goes all the way through, and a format nothing reads is refused
# the same way a scan is, rather than raising a library exception that would
# end a multi-document run.


def _docx(path: Path, paragraphs: list[str]) -> Path:
    """Write a minimal but valid .docx. Built rather than checked in, so the
    fixture cannot drift from what the test claims it contains."""
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-'
        'officedocument.wordprocessingml.document.main+xml"/></Types>'
    )
    rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
        'relationships/officeDocument" Target="word/document.xml"/></Relationships>'
    )
    doc_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>'
    )
    body = "".join(f"<w:p><w:r><w:t>{t}</w:t></w:r></w:p>" for t in paragraphs)
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{body}</w:body></w:document>"
    )
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", content_types)
        z.writestr("_rels/.rels", rels)
        z.writestr("word/_rels/document.xml.rels", doc_rels)
        z.writestr("word/document.xml", document)
    return path


def test_a_docx_bulletin_reads_and_extracts(tmp_path, vocab):
    """A Word bulletin is readable, and reaches the catalogue like a PDF does."""
    path = _docx(
        tmp_path / "rajasthan_advisory.docx",
        [
            "Agromet Advisory Bulletin for Rajasthan",
            "District: Jaipur. Wheat at tillering stage. Apply first irrigation "
            "21 days after sowing.",
            "Spray Mancozeb 75 WP at 2 g/litre if yellow rust appears on wheat leaves.",
        ],
    )
    doc = read_document(path)
    assert doc.page_count >= 1
    assert "Rajasthan" in doc.pages[0].text

    assert detect_state(doc, vocab) == "IN-RJ"
    passages, _ = extract(doc, vocab=vocab)
    assert passages, "a readable docx must produce passages like any other document"


def test_an_unreadable_format_is_refused_not_crashed(tmp_path):
    """The legacy .doc binary opens with nothing we ship, so it must refuse.

    Before this, whatever pymupdf raised propagated out of ingest and ended the
    run, so one bad file in a batch lost every document after it.
    """
    path = tmp_path / "bulletin.doc"
    path.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1legacy word binary")
    with pytest.raises(UnusableDocument) as exc:
        read_document(path)
    message = str(exc.value)
    assert "could not be opened" in message
    assert "DOCX" in message


def test_a_missing_file_is_still_an_error_not_a_refusal(tmp_path):
    """Absent is not unreadable. A typo in a filename should say so."""
    with pytest.raises(FileNotFoundError):
        read_document(tmp_path / "no_such_bulletin.pdf")


# --- 10. round trip ----------------------------------------------------------


def test_resource_attributes_survive_transit_unchanged(envelope):
    result, node = publish(envelope)
    assert result.round_trip_ok, result.round_trip_problems
    ok, problems = node.verify_round_trip(envelope)
    assert ok and not problems


def test_network_node_returns_attributes_verbatim(envelope):
    _, node = publish(envelope)
    res = envelope.message.catalogs[0].resources[0]
    held = node.resource_attributes(res.id)
    sent = res.resourceAttributes.model_dump(by_alias=True, exclude_none=True)
    assert held == sent


# --- structural guards -------------------------------------------------------


def test_no_coordinates_anywhere_in_payload(envelope):
    """Areas are governed codes; we never publish a polygon we did not survey."""
    body = json.dumps(envelope.to_wire())
    assert '"coordinates"' not in body
    assert '"Polygon"' not in body


def test_provider_omits_availableat(envelope):
    for cat in envelope.message.catalogs:
        dumped = cat.provider.model_dump(exclude_none=True)
        assert "availableAt" not in dumped


def test_subject_uris_are_all_taxonomy_minted(envelope, vocab):
    minted = {s.uri for s in vocab.subject_by_slug.values()}
    for cat in envelope.message.catalogs:
        for res in cat.resources:
            payload = res.resourceAttributes.model_dump(by_alias=True, exclude_none=True)
            for subj in payload.get("agricultureSubjects", []):
                assert subj["subjectId"] in minted


def test_all_districts_resolve_for_each_state(vocab):
    """Every district in the table is findable in its bulletin."""
    expected = {"IN-KA": 31, "IN-UP": 75, "IN-RJ": 32}
    for path in (KARNATAKA, UP, RAJASTHAN):
        doc = read_document(path)
        code = detect_state(doc, vocab)
        _, report = extract(doc, vocab=vocab)
        assert report.districts == expected[code], (
            f"{code}: resolved {report.districts} districts, expected {expected[code]}"
        )
