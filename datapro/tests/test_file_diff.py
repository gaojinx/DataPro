"""文件版本对比（单元格级 diff）单测。"""
from app.services.file_diff import diff_datasets


def D(name, header, rows):
    return (name, header, rows)


def test_modified_cell_and_added_row():
    before = [D("Sheet1", ["名称", "数量"], [["A", "1"], ["B", "2"]])]
    after = [D("Sheet1", ["名称", "数量"], [["A", "1"], ["B", "3"], ["C", "4"]])]
    result = diff_datasets(before, after)
    summary = result["summary"]
    assert summary["changed_cells"] == 1
    assert summary["added_rows"] == 1 and summary["removed_rows"] == 0
    changes = result["sheets"][0]["changes"]
    assert changes[0] == {"row": 2, "col": "B", "before": "2", "after": "3"}


def test_removed_row_and_header_change():
    before = [D("Sheet1", ["名称", "数量"], [["A", "1"], ["B", "2"]])]
    after = [D("Sheet1", ["名称", "销量"], [["A", "1"]])]
    result = diff_datasets(before, after)
    sheet = result["sheets"][0]
    assert sheet["removed_rows"] == 1
    assert sheet["header_changes"] == [{"col": "B", "before": "数量", "after": "销量"}]
    assert result["summary"]["header_changes"] == 1


def test_added_and_removed_sheet():
    before = [D("Sheet1", ["a"], [["1"]]), D("旧表", ["a"], [["1"], ["2"]])]
    after = [D("Sheet1", ["a"], [["1"]]), D("新表", ["a"], [["1"], ["2"], ["3"]])]
    result = diff_datasets(before, after)
    by_name = {s["name"]: s for s in result["sheets"]}
    assert by_name["Sheet1"]["status"] == "same"
    assert by_name["新表"]["status"] == "added"
    assert by_name["旧表"]["status"] == "removed"
    assert result["summary"]["added_rows"] == 3
    assert result["summary"]["removed_rows"] == 2


def test_same_dataset_has_no_changes():
    data = [D("Sheet1", ["a", "b"], [["1", "2"]])]
    result = diff_datasets(data, data)
    assert result["summary"] == {"sheets": 1, "added_rows": 0, "removed_rows": 0,
                                 "changed_cells": 0, "header_changes": 0,
                                 "truncated": False}
    assert result["sheets"][0]["status"] == "same"


def test_none_and_numbers_normalized():
    before = [D("Sheet1", ["a"], [[None], [1]])]
    after = [D("Sheet1", ["a"], [[""], ["1"]])]
    result = diff_datasets(before, after)
    assert result["summary"]["changed_cells"] == 0
