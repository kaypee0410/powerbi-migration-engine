"""End to end on a synthetic .pbit, plus an optional run on any real file you drop into samples/."""
import json
from pathlib import Path

import pytest
from openpyxl import load_workbook

from pbi_insights.pipeline import Options, run

CORE_SHEETS = ["Plan", "Tabs", "Insights", "Insight Lineage", "Sources", "Columns", "Relationships", "Measures",
               "Build Plan", "Target Views", "Insight SQL"]


def test_pipeline_writes_workbook_and_json(pbit, tmp_path):
    res = run(pbit, tmp_path / "out", Options(dialect="spark"))
    wb = load_workbook(res.workbook)
    assert wb.sheetnames == CORE_SHEETS
    s = res.summary
    assert (s["tabs"], s["data_insights"], s["tables"], s["measures"]) == (1, 6, 2, 9)
    assert s["measures_review"] == 1 and s["insights_review"] == 1
    measures = {r[0].value: r for r in wb["Measures"].iter_rows(min_row=2)}
    assert measures["Unused"][2].value == "Not used"
    assert measures["Total"][2].value == "Building block"
    views = {r[0].value: r[4].value for r in wb["Target Views"].iter_rows(min_row=2)}
    assert "CASE WHEN src.Area > 100 THEN 'Big' ELSE 'Small' END" in views["vw_store"]
    data = json.loads(Path(res.json_path).read_text(encoding="utf-8"))
    assert {"summary", "report", "model", "measures", "plans", "views", "findings"} <= set(data)


def test_formula_like_text_is_stored_as_text(pbit, tmp_path):
    res = run(pbit, tmp_path / "out")
    for ws in load_workbook(res.workbook).worksheets:
        for row in ws.iter_rows():
            for c in row:
                assert c.data_type != "f", f"{ws.title}!{c.coordinate} became a formula"


SAMPLES = sorted(p for ext in ("*.pbix", "*.pbit") for p in (Path(__file__).parents[1] / "samples").glob(ext))


@pytest.mark.skipif(not SAMPLES, reason="put a .pbix in samples/ to run this")
@pytest.mark.parametrize("sample", SAMPLES, ids=[p.name for p in SAMPLES])
def test_real_sample(sample, tmp_path):
    res = run(sample, tmp_path, Options(read_data=True))
    assert res.workbook.is_file()
    errors = res.summary.get("sql_test_errors")
    assert not errors, f"{errors} insight queries failed on the saved data: see the Insight SQL sheet"
