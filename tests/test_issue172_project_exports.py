"""Issue #172 unit 1 (stub and helper): the monitor's project.ts stub declares every name the hooks import from './project'.

The seam is ``tools/agentops/project_exports.py``: four functions over the hooks folder and the stub text. ROW 2 reads
the real stub file (placeholders left as they are) against the imports of every monitor hooks folder under ``mods/``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tools.agentops import project_exports as pe

REPO = Path(__file__).resolve().parents[1]
HOOKS = sorted(p.parent for p in (REPO / "mods").glob("*/hooks/project.ts"))
STUB = REPO / "config" / "core_templates" / "monitor_project.ts.stub"
COLLECTOR_FAST_BLOCK = (
    "// the fast payload an always-running collector writes ({ at_ms, refresh_s, data }); the pane's fast pull reads it first\n"
    "export const COLLECTOR_FAST = `${MOD_DIR}/bin/.cache/collector-fast.json`\n"
)


def _hooks(tmp_path: Path, files: dict[str, str]) -> Path:
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    for name, text in files.items():
        (hooks / name).write_text(text, encoding="utf-8")
    return hooks


def test_row2_stub_declares_every_name_the_home_hooks_import():
    assert HOOKS, "no monitor hooks folder under mods/"
    names = set().union(*(pe.imported_names(h) for h in HOOKS))
    assert "COLLECTOR_FAST" in names, sorted(names)
    stub_names = pe.exported_names(STUB.read_text(encoding="utf-8"))
    assert sorted(names - stub_names) == []


def test_imported_names_takes_multiline_double_quote_alias_and_import_type(tmp_path):
    hooks = _hooks(tmp_path, {
        "a.tsx": 'import {\n  ALPHA,\n  BETA as B,\n} from "./project"\n',
        "b.ts": "import type { GAMMA } from './project'\nimport { DELTA } from './other'\n",
    })
    assert pe.imported_names(hooks) == {"ALPHA", "BETA", "GAMMA"}


def test_imported_names_reads_only_files_directly_in_the_folder(tmp_path):
    hooks = _hooks(tmp_path, {"a.ts": "import { ONE } from './project'\n"})
    sub = hooks / "sub"
    sub.mkdir()
    (sub / "c.ts").write_text("import { ZETA } from './project'\n", encoding="utf-8")
    assert pe.imported_names(hooks) == {"ONE"}


def test_exported_names_takes_top_level_declarations_only():
    text = (
        "export const A = 1\n"
        "export function f() {}\n"
        "export type T = string\n"
        "export interface I {}\n"
        "export class C {}\n"
        "export let L = 2\n"
        "const hidden = 3\n"
        "  export const indented = 4\n"
    )
    assert pe.exported_names(text) == {"A", "f", "T", "I", "C", "L"}


@pytest.mark.parametrize("line, name", [
    ("export const A = 1", "A"),
    ("export let A = 1", "A"),
    ("export var A = 1", "A"),
    ("export declare const A: number", "A"),
    ("export function A() {}", "A"),
    ("export async function A() {}", "A"),
    ("export declare function A(): void", "A"),
    ("export class A {}", "A"),
    ("export abstract class A {}", "A"),
    ("export declare abstract class A {}", "A"),
    ("export enum A {}", "A"),
    ("export const enum A {}", "A"),
    ("export declare enum A {}", "A"),
    ("export type A = string", "A"),
    ("export interface A {}", "A"),
    ("export declare interface A {}", "A"),
])
def test_exported_names_recognises_every_declaration_form(line, name):
    assert pe.exported_names(line + "\n") == {name}


def test_exported_names_takes_the_alias_of_an_export_list():
    assert pe.exported_names("export { A, B as C }\n") == {"A", "C"}


def test_exported_names_takes_a_re_export_list_and_type_only_list():
    assert pe.exported_names("export { x } from './y'\n") == {"x"}
    assert pe.exported_names("export type { T as U } from './t'\n") == {"U"}


def test_exported_names_takes_an_export_list_spread_over_lines():
    assert pe.exported_names("export {\n  A,\n  B as C,\n}\n") == {"A", "C"}


def test_missing_names_counts_an_export_list_alias_as_exported(tmp_path):
    hooks = _hooks(tmp_path, {
        "a.ts": "import { C } from './project'\n",
        "project.ts": "const B = 1\nexport { B as C }\n",
    })
    assert pe.missing_names(hooks) == []


def test_lines_to_append_reads_the_stub_from_the_given_home_root(tmp_path):
    home = tmp_path / "home"
    (home / "config" / "core_templates").mkdir(parents=True)
    (home / "config" / "core_templates" / "monitor_project.ts.stub").write_text(
        "// a fake stub comment\nexport const X = '{mod_dir}'\n", encoding="utf-8")
    hooks = _hooks(tmp_path, {"a.ts": "import { X } from './project'\n"})
    assert pe.lines_to_append(hooks, {"{mod_dir}": "M"}, home_root=home) == (
        "// a fake stub comment\nexport const X = 'M'\n")


def test_missing_names_gives_the_one_name_the_project_lacks(tmp_path):
    hooks = _hooks(tmp_path, {
        "a.ts": "import { ONE, TWO } from './project'\n",
        "project.ts": "export const ONE = 1\n",
    })
    assert pe.missing_names(hooks) == ["TWO"]


def test_missing_names_gives_all_names_when_project_is_absent(tmp_path):
    hooks = _hooks(tmp_path, {"a.ts": "import { ONE, TWO } from './project'\n"})
    assert pe.missing_names(hooks) == ["ONE", "TWO"]


def test_lines_to_append_appends_the_missing_declaration_with_its_comment(tmp_path):
    hooks = _hooks(tmp_path, {
        "register.tsx": "import { COLLECTOR_FAST } from './project'\n",
        "project.ts": "export const MOD_DIR = 'x'\n",
    })
    assert pe.lines_to_append(hooks, {}) == COLLECTOR_FAST_BLOCK


def test_lines_to_append_fills_the_placeholders_with_the_given_values(tmp_path):
    hooks = _hooks(tmp_path, {"a.ts": "import { MOD_DIR } from './project'\n"})
    text = pe.lines_to_append(hooks, {"{mod_dir}": "C:/mods/x"})
    assert text.endswith("export const MOD_DIR = 'C:/mods/x'\n"), text
    assert "{mod_dir}" not in text


def test_lines_to_append_keeps_stub_order(tmp_path):
    hooks = _hooks(tmp_path, {"a.ts": "import { OPENED_TEXT, SCRIPT } from './project'\n"})
    assert pe.lines_to_append(hooks, {"{mod_dir}": "M"}) == (
        "export const SCRIPT = `${MOD_DIR}/bin/collect.py`\n"
        "export const OPENED_TEXT = `${TITLE} monitor pane opened.`\n"
    )


def test_lines_to_append_is_empty_when_nothing_is_missing(tmp_path):
    hooks = _hooks(tmp_path, {
        "a.ts": "import { SCRIPT } from './project'\n",
        "project.ts": "export const SCRIPT = 'x'\n",
    })
    assert pe.lines_to_append(hooks, {}) == ""


def test_lines_to_append_raises_naming_a_name_the_stub_does_not_declare(tmp_path):
    hooks = _hooks(tmp_path, {"a.ts": "import { NOT_IN_STUB } from './project'\n"})
    with pytest.raises(ValueError) as err:
        pe.lines_to_append(hooks, {})
    assert "NOT_IN_STUB" in str(err.value)
