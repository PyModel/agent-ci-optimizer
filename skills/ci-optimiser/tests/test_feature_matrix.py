"""Keep recommendation coverage and verification claims inspectable and honest."""
from __future__ import annotations

import ast
import copy
import json
import re
import shutil
import tempfile
import unittest
from datetime import date
from functools import lru_cache
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parent.parent
MATRIX = ROOT / "references/feature-matrix.json"
EXPECTED_RECOMMENDATIONS = {
    *(f"M{i}" for i in range(1, 15)),
    *(f"G{i}" for i in range(1, 11)),
    *(f"T{i}" for i in range(1, 13)),
}
PATH_FIELDS = ("implementation_paths", "documentation_paths", "test_paths")
STATUSES = {"implemented", "implemented_with_limits", "conditional"}


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def nonempty(value):
    return isinstance(value, str) and bool(value.strip())


def string_list(value, *, empty=True):
    return (isinstance(value, list) and (empty or bool(value))
            and all(nonempty(item) for item in value)
            and len(value) == len(set(value)))


def valid_date(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


@lru_cache(maxsize=256)
def source_info(source):
    """Cache by exact bytes decoded as text, so changed files never reuse stale facts."""
    tree = ast.parse(source)
    nodes = list(ast.walk(tree))
    imports = set()
    for node in nodes:
        if isinstance(node, ast.Import):
            imports.update(item.name.removeprefix("scripts.") for item in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module == "scripts":
                imports.update(item.name for item in node.names)
            elif node.module:
                imports.add(node.module.removeprefix("scripts."))
    strings = frozenset(node.value for node in nodes
                        if isinstance(node, ast.Constant) and isinstance(node.value, str))
    main_guard = any(isinstance(node, ast.If) and isinstance(node.test, ast.Compare)
                     and isinstance(node.test.left, ast.Name) and node.test.left.id == "__name__"
                     and len(node.test.ops) == 1 and isinstance(node.test.ops[0], ast.Eq)
                     and len(node.test.comparators) == 1
                     and isinstance(node.test.comparators[0], ast.Constant)
                     and node.test.comparators[0].value == "__main__" for node in tree.body)
    has_tests = any(isinstance(node, ast.FunctionDef) and node.name.startswith("test_") for node in nodes)
    return frozenset(imports), strings, main_guard, has_tests


def helper_inventory_problems(inventory, features, root, reviewed):
    """Compare declared helper ownership with files, interfaces and static references."""
    problems = []

    def require(condition, message):
        if not condition:
            problems.append(message)

    if not isinstance(inventory, dict):
        return ["helper registry missing"]
    require(set(inventory) == {"schema", "reviewed_on", "documentation", "helpers"},
            "helper registry fields invalid")
    require(inventory.get("schema") == "ci-helper-inventory/1", "helper registry schema invalid")
    require(inventory.get("reviewed_on") == reviewed, "helper inventory review date inconsistent")
    require(inventory.get("documentation") == "references/helper-registry.md"
            and (root / "references/helper-registry.md").is_file(), "helper registry documentation missing")
    entries = inventory.get("helpers")
    if not isinstance(entries, list) or not entries:
        return problems + ["helper entries missing"]
    actual = {p.relative_to(root).as_posix(): p for p in (root / "scripts").rglob("*.py")}
    paths = [e.get("path") for e in entries if isinstance(e, dict)]
    require(string_list(paths) and len(paths) == len(entries), "duplicate or invalid helper paths")
    require(set(p for p in paths if isinstance(p, str)) == set(actual), "helper file inventory mismatch")
    trees = {}

    def tree(path):
        if path not in trees:
            try:
                trees[path] = source_info((root / path).read_text(encoding="utf-8"))
            except (OSError, SyntaxError, UnicodeError):
                trees[path] = (frozenset(), frozenset(), False, False)
                problems.append("unreadable helper/test source: " + path)
        return trees[path]

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        path = entry.get("path")
        if not isinstance(path, str) or path not in actual:
            continue
        visibility, interface = entry.get("visibility"), entry.get("interface")
        require(visibility in ("public", "internal"), path + ": visibility invalid")
        require(interface in ("cli", "library"), path + ": interface invalid")
        fields = {"path", "visibility", "interface", "feature_ids"}
        if visibility == "internal":
            fields |= {"consumed_by", "reason"}
        require(set(entry) == fields, path + ": registry fields invalid")
        main_guard = tree(path)[2]
        require(main_guard == (interface == "cli"), path + ": declared interface differs from entrypoint")
        require(not main_guard or visibility == "public", path + ": CLI cannot be internal")
        expected = {ident for ident, feature in features.items() if feature.get("domain") == "repository"
                    and path in feature.get("implementation_paths", [])}
        linked = entry.get("feature_ids")
        require(bool(expected) and string_list(linked, empty=False) and set(linked) == expected,
                path + ": exact capability mapping missing or stale")
        docs = {p for ident in expected for p in features[ident].get("documentation_paths", [])}
        tests = {p for ident in expected for p in features[ident].get("test_paths", [])}
        module = actual[path].stem
        if visibility == "internal":
            consumers = {other for other in actual if other != path and module in tree(other)[0]}
            require(bool(consumers) and string_list(entry.get("consumed_by"), empty=False)
                    and set(entry["consumed_by"]) == consumers, path + ": internal consumers missing or stale")
            require(nonempty(entry.get("reason")), path + ": internal purpose missing")
            continue
        documented = False
        for doc in docs:
            doc_path = root / doc
            if not doc_path.is_file():
                continue
            for target in re.findall(r"\]\(([^)#\s]+)(?:#[^)]*)?\)", doc_path.read_text(encoding="utf-8")):
                if (doc_path.parent / target).resolve() == actual[path].resolve():
                    documented = True
        require(documented, path + ": public documentation link missing or stale")
        referenced = False
        for test in tests:
            if not (root / test).is_file():
                continue
            test_tree = tree(test)
            command_path = bool(test_tree[1] & {module, actual[path].name, path})
            if module in test_tree[0] or command_path:
                referenced = True
        require(referenced, path + ": public test reference missing or stale")
    return problems


def matrix_problems(matrix, root=ROOT):
    """Validate local traceability; never treat a source URL as runtime proof."""
    problems = []

    def require(condition, message):
        if not condition:
            problems.append(message)

    if not isinstance(matrix, dict):
        return ["matrix must be an object"]
    require(matrix.get("schema") == "ci-feature-matrix/1", "unsupported matrix schema")
    reviewed = matrix.get("reviewed_on")
    require(valid_date(reviewed), "invalid review date")
    require(matrix.get("review_timezone") == "America/New_York", "review timezone missing")
    register = matrix.get("source_register")
    require(register == "references/06-official-sources.md", "unexpected source register")
    source_text = (root / "references/06-official-sources.md").read_text(encoding="utf-8")
    known_urls = set(re.findall(r"https://[^\s|)<>]+", source_text))
    if valid_date(reviewed):
        require(reviewed in source_text, "review date missing from source register")
    require(set(matrix.get("status_definitions", {})) == STATUSES, "status definitions incomplete")
    policy = matrix.get("verification_policy", {})
    require(isinstance(policy, dict), "verification policy missing")
    if isinstance(policy, dict):
        for field in ("provider_runtime_tests_executed", "stack_runtime_tests_executed",
                      "external_configuration_changes_applied"):
            require(policy.get(field) is False, f"unsupported execution claim: {field}")
        for field in ("downstream_runtime_compatibility", "measured_performance_improvement"):
            require(policy.get(field) == "unverified", f"unsupported outcome claim: {field}")

    checked_paths = set()

    def paths_and_sources(record, label):
        for field in PATH_FIELDS:
            entries = record.get(field)
            require(string_list(entries), f"{label}: invalid {field}")
            if not string_list(entries):
                continue
            for entry in entries:
                key = (field, entry)
                if key in checked_paths:
                    continue
                checked_paths.add(key)
                path = PurePosixPath(entry)
                lexical = (not path.is_absolute() and ".." not in path.parts
                           and path.as_posix() == entry and "\\" not in entry)
                require(lexical, f"unsafe evidence path: {entry}")
                if not lexical:
                    continue
                destination = root.joinpath(*path.parts)
                try:
                    destination.resolve().relative_to(root.resolve())
                except ValueError:
                    problems.append(f"evidence path leaves repository: {entry}")
                    continue
                require(destination.is_file(), f"missing evidence file: {entry}")
                if field == "test_paths" and destination.is_file():
                    require(entry.startswith("tests/test_") and entry.endswith(".py"),
                            f"not a test file: {entry}")
                    try:
                        info = source_info(destination.read_text(encoding="utf-8"))
                    except (SyntaxError, UnicodeError):
                        problems.append(f"unparseable test file: {entry}")
                    else:
                        require(info[3], f"test file has no tests: {entry}")
        require(bool(record.get("documentation_paths")), f"{label}: documentation evidence missing")
        urls = record.get("source_urls")
        require(string_list(urls, empty=False), f"{label}: source URLs missing or duplicated")
        if string_list(urls, empty=False):
            for url in urls:
                parsed = urlsplit(url)
                require(parsed.scheme == "https" and bool(parsed.netloc) and not parsed.username
                        and not parsed.password, f"{label}: invalid source URL")
                require(url in known_urls, f"{label}: source absent from dated register: {url}")

    features = matrix.get("features")
    if not isinstance(features, list) or not features:
        return problems + ["feature inventory missing"]
    feature_ids = [f.get("id") for f in features if isinstance(f, dict)]
    require(all(nonempty(item) for item in feature_ids), "feature id missing")
    require(len(feature_ids) == len(features) == len(set(feature_ids)), "duplicate or invalid feature id")
    by_id = {f["id"]: f for f in features if isinstance(f, dict) and nonempty(f.get("id"))}
    helpers = set()
    for feature in features:
        if not isinstance(feature, dict):
            continue
        label = feature.get("id", "unknown feature")
        require(nonempty(feature.get("title")), f"{label}: title missing")
        require(feature.get("status") in STATUSES, f"{label}: unknown status")
        require(valid_date(feature.get("reviewed_on")) and valid_date(reviewed)
                and feature.get("reviewed_on") <= reviewed,
                f"{label}: invalid or inconsistent review date")
        scope = feature.get("version_scope", {})
        require(isinstance(scope, dict) and string_list(scope.get("reviewed"), empty=False)
                and nonempty(scope.get("target")), f"{label}: version scope missing")
        require(string_list(feature.get("support_constraints"), empty=False), f"{label}: constraints missing")
        paths_and_sources(feature, label)
        verification = feature.get("verification", {})
        require(isinstance(verification, dict), f"{label}: verification missing")
        if not isinstance(verification, dict):
            continue
        require(verification.get("downstream_runtime_compatibility") == "unverified",
                f"{label}: downstream compatibility is not proven")
        if feature.get("domain") == "repository":
            helpers.add(label)
            require(feature.get("status") == "implemented", f"{label}: local helper status")
            require(verification.get("kind") == "python_fixtures"
                    and verification.get("runtime_execution") == "local_passed",
                    f"{label}: missing local fixture verification")
            require(bool(feature.get("implementation_paths")) and bool(feature.get("test_paths")),
                    f"{label}: implementation or test evidence missing")
        else:
            require(feature.get("domain") in {"platform", "stack"}, f"{label}: invalid feature domain")
            require(feature.get("status") == "conditional", f"{label}: downstream adoption must be conditional")
            require(verification.get("kind") == "source_review"
                    and verification.get("runtime_execution") == "not_executed",
                    f"{label}: local fixtures cannot prove downstream execution")
    problems.extend(helper_inventory_problems(matrix.get("helper_inventory"), by_id, root, reviewed))

    coverage = matrix.get("recommendation_coverage")
    if not isinstance(coverage, list):
        return problems + ["recommendation coverage missing"]
    ids = [item.get("id") for item in coverage if isinstance(item, dict)]
    require(len(ids) == len(coverage) == len(set(ids)), "duplicate or invalid recommendation id")
    require(set(ids) == EXPECTED_RECOMMENDATIONS, "36 recommendation inventory incomplete")
    for record in coverage:
        if not isinstance(record, dict):
            continue
        label = record.get("id", "unknown recommendation")
        require(nonempty(record.get("title")) and nonempty(record.get("implementation_scope")),
                f"{label}: concrete scope missing")
        require(record.get("status") in STATUSES, f"{label}: invalid status")
        require(string_list(record.get("remaining_target_validation")), f"{label}: target limitations missing")
        paths_and_sources(record, label)
        linked = record.get("feature_ids")
        require(string_list(linked, empty=False) and all(item in by_id for item in linked),
                f"{label}: feature mapping missing or unresolved")
        if string_list(linked, empty=False) and all(item in by_id for item in linked):
            for field in (*PATH_FIELDS, "source_urls"):
                expected = {value for key in linked for value in by_id[key].get(field, [])}
                require(set(record.get(field, [])) == expected, f"{label}: stale {field} mapping")

    local = matrix.get("local_verification", {})
    require(isinstance(local, dict), "local verification missing")
    if isinstance(local, dict):
        require(local.get("result") == "passed", "local test result not recorded")
        expected = {path for key in helpers for path in by_id[key].get("test_paths", [])}
        require(set(local.get("test_paths", [])) == expected, "local test inventory incomplete")
        require(local.get("command") == ["python3", "-m", "unittest", "discover", "-s", "tests", "-v"],
                "local verification command missing")
        environment = local.get("environment", {})
        require(isinstance(environment, dict) and nonempty(environment.get("python"))
                and nonempty(environment.get("platform")), "local test environment missing")
    return problems


class FeatureMatrixTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.matrix = json.loads(MATRIX.read_text(encoding="utf-8"), object_pairs_hook=unique_object)

    def test_register_links_versions_scopes_and_complete_inventory(self):
        self.assertEqual(matrix_problems(self.matrix), [])
        self.assertEqual(len(self.matrix["recommendation_coverage"]), 36)

    def test_missing_or_duplicate_recommendation_is_detected(self):
        for change in (lambda rows: rows.pop(), lambda rows: rows.append(copy.deepcopy(rows[0]))):
            altered = copy.deepcopy(self.matrix)
            change(altered["recommendation_coverage"])
            self.assertTrue(any("recommendation" in problem for problem in matrix_problems(altered)))

    def test_duplicate_feature_and_duplicate_json_keys_are_rejected(self):
        altered = copy.deepcopy(self.matrix)
        altered["features"].append(copy.deepcopy(altered["features"][0]))
        self.assertIn("duplicate or invalid feature id", matrix_problems(altered))
        with self.assertRaises(ValueError):
            json.loads('{"schema":"one","schema":"two"}', object_pairs_hook=unique_object)

    def test_invalid_dates_and_missing_version_scope_are_detected(self):
        for field, value in (("reviewed_on", "2026-02-30"), ("version_scope", {})):
            altered = copy.deepcopy(self.matrix)
            altered["features"][0][field] = value
            self.assertTrue(matrix_problems(altered))

    def test_unregistered_source_and_missing_or_escaping_files_are_detected(self):
        for field, value in (("source_urls", ["https://example.invalid/unreviewed"]),
                             ("implementation_paths", ["scripts/not-present.py"]),
                             ("implementation_paths", ["../outside.py"])):
            altered = copy.deepcopy(self.matrix)
            altered["features"][0][field] = value
            self.assertTrue(matrix_problems(altered))

    def test_python_fixtures_cannot_be_relabelled_as_downstream_runtime_proof(self):
        altered = copy.deepcopy(self.matrix)
        external = next(item for item in altered["features"] if item["domain"] == "stack")
        external["verification"]["runtime_execution"] = "local_passed"
        self.assertTrue(any("downstream execution" in problem for problem in matrix_problems(altered)))
        altered = copy.deepcopy(self.matrix)
        altered["verification_policy"]["stack_runtime_tests_executed"] = True
        self.assertTrue(any("unsupported execution claim" in problem for problem in matrix_problems(altered)))

    def test_local_implementation_needs_real_tests_and_resolved_feature_mapping(self):
        altered = copy.deepcopy(self.matrix)
        altered["features"][0]["test_paths"] = []
        self.assertTrue(any("test evidence missing" in problem for problem in matrix_problems(altered)))
        altered = copy.deepcopy(self.matrix)
        altered["recommendation_coverage"][0]["feature_ids"] = ["unimplemented-feature"]
        self.assertTrue(any("feature mapping" in problem for problem in matrix_problems(altered)))


    def test_missing_extra_and_duplicate_registry_entries_are_detected(self):
        for change in (lambda rows: rows.pop(),
                       lambda rows: rows.append(copy.deepcopy(rows[0])),
                       lambda rows: rows[0].update(path="scripts/removed_helper.py")):
            altered = copy.deepcopy(self.matrix)
            change(altered["helper_inventory"]["helpers"])
            self.assertTrue(any("helper" in problem for problem in matrix_problems(altered)))

    def test_new_python_file_cannot_escape_registry_discovery(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "skill"
            shutil.copytree(ROOT, root, ignore=shutil.ignore_patterns("__pycache__"))
            (root / "scripts/unregistered.py").write_text(
                'if __name__ == "__main__":\n    print("new command")\n')
            self.assertIn("helper file inventory mismatch", matrix_problems(self.matrix, root))

    def test_public_cli_cannot_be_reclassified_as_internal(self):
        altered = copy.deepcopy(self.matrix)
        entry = next(e for e in altered["helper_inventory"]["helpers"]
                     if e["path"] == "scripts/collect_gh.py")
        entry.update(visibility="internal", interface="library", consumed_by=[], reason="claimed internal")
        problems = matrix_problems(altered)
        self.assertIn("scripts/collect_gh.py: CLI cannot be internal", problems)
        self.assertIn("scripts/collect_gh.py: declared interface differs from entrypoint", problems)

    def test_public_library_remains_mapped_and_internal_consumers_are_exact(self):
        entries = self.matrix["helper_inventory"]["helpers"]
        graph = next(e for e in entries if e["path"] == "scripts/graph_metrics.py")
        self.assertEqual((graph["visibility"], graph["interface"]), ("public", "library"))
        altered = copy.deepcopy(self.matrix)
        module = next(e for e in altered["helper_inventory"]["helpers"]
                      if e["path"] == "scripts/phase_map.py")
        module["consumed_by"] = ["scripts/collect_gh.py"]
        self.assertIn("scripts/phase_map.py: internal consumers missing or stale", matrix_problems(altered))

    def test_capability_mapping_detects_missing_and_extra_feature_ids(self):
        for feature_ids in ([], ["collector"], ["job_graph", "unregistered"]):
            altered = copy.deepcopy(self.matrix)
            entry = next(e for e in altered["helper_inventory"]["helpers"]
                         if e["path"] == "scripts/graph_metrics.py")
            entry["feature_ids"] = feature_ids
            self.assertIn("scripts/graph_metrics.py: exact capability mapping missing or stale",
                          matrix_problems(altered))

    def test_existing_but_unrelated_docs_and_tests_do_not_satisfy_mapping(self):
        for field, value, message in (
                ("documentation_paths", ["references/03-security-correctness.md"], "public documentation link"),
                ("test_paths", ["tests/test_skill_docs.py"], "public test reference")):
            altered = copy.deepcopy(self.matrix)
            feature = next(f for f in altered["features"] if f["id"] == "capacity_model")
            feature[field] = value
            self.assertIn("scripts/capacity_model.py: " + message + " missing or stale",
                          matrix_problems(altered))

    def test_changed_test_contents_invalidate_cached_source_facts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "skill"
            shutil.copytree(ROOT, root, ignore=shutil.ignore_patterns("__pycache__"))
            self.assertEqual(matrix_problems(self.matrix, root), [])
            test = root / "tests/test_capacity_model.py"
            test.write_text("def test_placeholder():\n    pass\n")
            self.assertIn("scripts/capacity_model.py: public test reference missing or stale",
                          matrix_problems(self.matrix, root))
            test.write_text("# No executable tests remain.\n")
            self.assertIn("test file has no tests: tests/test_capacity_model.py",
                          matrix_problems(self.matrix, root))

    def test_individual_review_dates_are_preserved_and_future_dates_rejected(self):
        self.assertTrue(any(f["reviewed_on"] < self.matrix["reviewed_on"] for f in self.matrix["features"]))
        altered = copy.deepcopy(self.matrix)
        altered["features"][0]["reviewed_on"] = "2099-01-01"
        self.assertTrue(any("review date" in p for p in matrix_problems(altered)))


if __name__ == "__main__":
    unittest.main()
