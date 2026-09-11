"""Efficiency v2, criterion 3 — owned-output budget helpers: field selection before serialization,
a combined batch budget with a truthful truncated flag and resume cursor, single-record tail
trimming, and bounded offset/limit retrieval of a retained file.

Run: python3 -m pytest coordination/tests/test_views_budget.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mycelium_coord import views  # noqa: E402


def test_select_fields_omits_absent_keys():
    assert views.select_fields({"a": 1, "b": None}, ("a", "b", "c")) == {"a": 1, "b": None}


def test_fit_batch_keeps_leading_items_and_flags_truncation():
    items = [{"job_id": f"j{i:03d}", "pad": "x" * 300} for i in range(40)]
    out = views.fit_batch(items, budget=4096, cursor_key="job_id")
    assert out["truncated"] is True and out["omitted"] == 40 - out["returned"]
    assert 0 < out["returned"] < 40
    assert out["next_cursor"] == out["items"][-1]["job_id"]
    assert views.encoded_size(out) <= 4096  # the whole response, as emitted
    small = views.fit_batch(items[:3], budget=4096, cursor_key="job_id")
    assert small == {"items": items[:3], "returned": 3, "omitted": 0, "truncated": False,
                     "next_cursor": None, "budget_bytes": 4096}


def test_fit_batch_stubs_a_single_oversized_record_instead_of_emitting_it():
    big = [{"job_id": "big", "blob": "y" * 10_000}]
    out = views.fit_batch(big, budget=1024, cursor_key="job_id")
    assert out["truncated"] is True and out["returned"] == 1 and out["omitted"] == 0
    stub = out["items"][0]
    assert stub["_truncated_record"] is True and stub["job_id"] == "big"
    assert views.encoded_size(out["items"]) <= 1024


def test_shrink_tails_trims_inline_text_keeps_references_and_flags():
    rec = {"job_id": "j", "outputs": {"stdout": {"path": "/p/stdout.log", "tail": "a" * 3000},
                                      "stderr": {"path": "/p/stderr.log", "tail": "b" * 3000}},
           "exit_code": 0}
    out = views.shrink_tails(rec, budget=2048)
    assert views.encoded_size(out) <= 2048
    assert out["truncated"] is True and out["truncation"]["trimmed_fields"] == ["tail"]
    assert out["outputs"]["stdout"]["path"] == "/p/stdout.log"
    assert out["outputs"]["stdout"]["tail_truncated"] is True
    assert out["outputs"]["stdout"]["tail"].endswith("a")  # the END of a tail is kept
    assert "over_budget" not in out
    fits = views.shrink_tails({"job_id": "j", "outputs": {"stdout": {"tail": "ok"}}}, budget=2048)
    assert fits["truncated"] is False


def test_shrink_trims_long_metadata_with_a_marker_and_never_references():
    rec = {"job_id": "j" * 100, "reason": "r" * 5000, "stdout_sha256": "s" * 64}
    out = views.shrink_tails(rec, budget=512)
    assert views.encoded_size(out) <= 512 and out["truncated"] is True
    assert out["reason_truncated"] is True and out["reason"].startswith("rrr") and len(out["reason"]) < 5000
    assert out["job_id"] == rec["job_id"] and out["stdout_sha256"] == rec["stdout_sha256"]
    assert out["truncation"]["trimmed_fields"] == ["reason"] and "over_budget" not in out
    hopeless = views.shrink_tails({"job_id": "j" * 2000}, budget=512)
    assert hopeless["over_budget"] is True and hopeless["job_id"] == "j" * 2000  # never silently cut


def test_read_bounded_pages_cut_on_character_boundaries_and_fit_a_wire_budget(tmp_path):
    p = tmp_path / "cjk.log"
    data = ("\u754c" * 2000).encode("utf-8")
    p.write_bytes(data)
    got, offset, pages = "", 0, 0
    while offset is not None:
        chunk = views.read_bounded(p, offset=offset, limit=4096)
        assert chunk["returned"] % 3 == 0 and chunk["lossy"] is False
        got += chunk["text"]
        offset = chunk["next_offset"]
        pages += 1
    assert got == "\u754c" * 2000 and pages == 2
    fitted = views.read_bounded(p, offset=0, limit=4096, budget=4096,
                                envelope={"job_id": "cjk", "stream": "stdout"})
    assert views.encoded_size(fitted) <= 4096 and fitted["job_id"] == "cjk" and fitted["lossy"] is False
    assert 0 < fitted["returned"] < 4096 and fitted["next_offset"] == fitted["returned"]
    tail = views.read_bounded(p, limit=4, tail=True)
    assert tail["text"] == "\u754c" and tail["offset"] == len(data) - 3 and tail["lossy"] is False
    odd = views.read_bounded(p, offset=1, limit=6)
    assert odd["lossy"] is True and odd["returned"] == 5
    binary = tmp_path / "bin.log"
    binary.write_bytes(b"\xff\xfe" * 10)
    b = views.read_bounded(binary)
    assert b["lossy"] is True and b["truncated"] is False


def test_read_bounded_offset_limit_tail_and_full_retrieval(tmp_path):
    p = tmp_path / "out.log"
    data = "".join(f"line{i:05d}\n" for i in range(5000)).encode()  # 50 KB
    p.write_bytes(data)
    first = views.read_bounded(p, offset=0, limit=4096)
    assert first["bytes_total"] == len(data) and first["returned"] == 4096
    assert first["truncated"] is True and first["next_offset"] == 4096
    # full retrieval through a bounded batch of reads, each within the budget
    got, offset, reads = b"", 0, 0
    while offset is not None:
        chunk = views.read_bounded(p, offset=offset, limit=4096)
        got += chunk["text"].encode()
        offset = chunk["next_offset"]
        reads += 1
    assert got == data and reads == 13
    last = views.read_bounded(p, offset=len(data) - 5, limit=4096)
    assert last["returned"] == 5 and last["next_offset"] is None and last["truncated"] is True
    tail = views.read_bounded(p, limit=12, tail=True)
    assert tail["text"] == "line04999\n"[-12:] or tail["text"].endswith("line04999\n")
    assert tail["offset"] == len(data) - 12
    whole = views.read_bounded(p, offset=0, limit=65536)
    assert whole["truncated"] is False and whole["next_offset"] is None
    assert whole["text"].encode() == data
    missing = views.read_bounded(tmp_path / "none.log")
    assert missing["exists"] is False and missing["text"] is None and missing["truncated"] is False
    capped = views.read_bounded(p, limit=10**9)
    assert capped["returned"] == min(len(data), views.MAX_READ_BYTES) == len(data)
    big = tmp_path / "big.log"
    big.write_bytes(b"z" * (views.MAX_READ_BYTES + 10))
    assert views.read_bounded(big, limit=10**9)["returned"] == views.MAX_READ_BYTES == 65536
