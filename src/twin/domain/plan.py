from __future__ import annotations

import copy
from collections import defaultdict
from pathlib import PurePosixPath
from typing import Callable


ITEM_STATUSES = frozenset({"pending", "in_progress", "completed", "blocked", "deferred"})
_REQUIRED_ITEM_FIELDS = (
    "id", "deliverable", "scope", "covers_ac", "evidence_plan", "actual_evidence",
    "depends_on", "status", "next_action",
)


def materialize_run_evidence(
    plan: dict[str, object], item_id: str, run_id: str
) -> dict[str, object]:
    """Bind the current item's evidence templates to one immutable worker run."""
    materialized = copy.deepcopy(plan)
    items = materialized.get("items")
    if not isinstance(items, list):
        return materialized
    for item in items:
        if not isinstance(item, dict) or item.get("id") != item_id:
            continue
        evidence = item.get("evidence_plan")
        if isinstance(evidence, list):
            item["evidence_plan"] = [
                value.replace("{run_id}", run_id) if isinstance(value, str) else value
                for value in evidence
            ]
        break
    return materialized


def validate_ready_plan(goal: dict[str, object], plan: dict[str, object]) -> list[str]:
    """Validate the stricter semantic boundary between editable drafts and runnable work."""
    errors: list[str] = []
    criteria = goal.get("acceptance_criteria")
    criterion_ids: set[str] = set()
    if not isinstance(criteria, list) or not criteria:
        errors.append("goal requires at least one acceptance criterion")
    else:
        for index, criterion in enumerate(criteria):
            if not isinstance(criterion, dict):
                errors.append(f"acceptance_criteria[{index}] must be object")
                continue
            criterion_id = criterion.get("id")
            statement = criterion.get("statement")
            evidence_type = criterion.get("evidence_type")
            if not isinstance(criterion_id, str) or not criterion_id.strip():
                errors.append(f"acceptance_criteria[{index}].id is required")
                continue
            if criterion_id in criterion_ids:
                errors.append(f"duplicate acceptance criterion id: {criterion_id}")
            criterion_ids.add(criterion_id)
            if not isinstance(statement, str) or not statement.strip():
                errors.append(f"{criterion_id}: statement is required")
            if not isinstance(evidence_type, str) or not evidence_type.strip():
                errors.append(f"{criterion_id}: evidence_type is required")

    verification = plan.get("verification")
    if (
        not isinstance(verification, list)
        or not verification
        or not all(isinstance(command, str) and command.strip() for command in verification)
    ):
        errors.append("verification must contain at least one command")

    items = plan.get("items")
    if not isinstance(items, list) or not items:
        errors.append("plan requires at least one actionable item")
        return errors + validate_plan(goal, plan)

    item_ids: set[str] = set()
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            errors.append(f"items[{index}] must be object")
            continue
        for field in _REQUIRED_ITEM_FIELDS:
            if field not in item:
                errors.append(f"items[{index}].{field} is required")
        item_id = item.get("id")
        if not isinstance(item_id, str) or not item_id.strip():
            continue
        if item_id in item_ids:
            errors.append(f"duplicate plan item id: {item_id}")
        item_ids.add(item_id)
        for field in ("deliverable", "scope", "next_action"):
            value = item.get(field)
            if not isinstance(value, str) or not value.strip():
                errors.append(f"{item_id}: {field} is required")
        for field in ("covers_ac", "evidence_plan", "actual_evidence", "depends_on"):
            values = item.get(field)
            if not isinstance(values, list) or not all(
                isinstance(value, str) and value for value in values
            ):
                errors.append(f"{item_id}: {field} must be a list of non-empty strings")
        covers = item.get("covers_ac")
        evidence_plan = item.get("evidence_plan")
        if isinstance(covers, list) and covers and (
            not isinstance(evidence_plan, list) or not evidence_plan
        ):
            errors.append(f"{item_id}: evidence_plan is required for acceptance coverage")
        if isinstance(evidence_plan, list):
            for entry in evidence_plan:
                if isinstance(entry, str) and not _valid_evidence_entry(entry):
                    errors.append(f"{item_id}: invalid evidence declaration {entry}")
    errors.extend(validate_plan(goal, plan))
    return list(dict.fromkeys(errors))


def _valid_evidence_entry(entry: str) -> bool:
    relative = entry.removeprefix("command:") if entry.startswith("command:") else entry
    path = PurePosixPath(relative)
    return (
        bool(relative)
        and not path.is_absolute()
        and path.parts[:1] == ("artifacts",)
        and all(part not in {"", ".", ".."} for part in path.parts)
    )


def validate_plan(goal: dict[str, object], plan: dict[str, object]) -> list[str]:
    errors: list[str] = []
    if plan.get("goal_id") != goal.get("id"):
        errors.append("plan.goal_id must match goal.id")
    items = plan.get("items")
    if not isinstance(items, list):
        return errors + ["plan.items must be a list"]
    known_ac = {
        str(criterion.get("id"))
        for criterion in goal.get("acceptance_criteria", [])
        if isinstance(criterion, dict) and str(criterion.get("id") or "")
    }
    item_ids: set[str] = set()
    dependencies: dict[str, list[str]] = {}
    covered: set[str] = set()
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            errors.append(f"items[{index}] must be object")
            continue
        item_id = str(item.get("id") or "")
        if not item_id:
            errors.append(f"items[{index}].id is required")
            continue
        if item_id in item_ids:
            errors.append(f"duplicate plan item id: {item_id}")
        item_ids.add(item_id)
        if item.get("status") not in ITEM_STATUSES:
            errors.append(f"{item_id}: invalid status {item.get('status')!r}")
        covers = item.get("covers_ac", [])
        if not isinstance(covers, list):
            errors.append(f"{item_id}: covers_ac must be a list")
            covers = []
        for ac_id in covers:
            rendered = str(ac_id)
            if rendered not in known_ac:
                errors.append(f"{item_id}: unknown AC id {rendered}")
            else:
                covered.add(rendered)
        raw_dependencies = item.get("depends_on", [])
        if not isinstance(raw_dependencies, list):
            errors.append(f"{item_id}: depends_on must be a list")
            raw_dependencies = []
        dependencies[item_id] = [str(value) for value in raw_dependencies]
    for ac_id in sorted(known_ac - covered):
        errors.append(f"acceptance criterion not covered by plan: {ac_id}")
    for item_id, values in dependencies.items():
        for dependency in values:
            if dependency not in item_ids:
                errors.append(f"{item_id}: unknown dependency {dependency}")
    _find_cycles(dependencies, errors)
    return errors


def apply_updates(plan: dict[str, object], updates: object) -> list[str]:
    if not isinstance(updates, list):
        return ["plan updates must be a list"]
    items = plan.get("items")
    if not isinstance(items, list):
        return ["plan.items must be a list"]
    item_by_id = {str(item.get("id")): item for item in items if isinstance(item, dict)}
    errors: list[str] = []
    for update in updates:
        if not isinstance(update, dict):
            errors.append("plan update must be object")
            continue
        item = item_by_id.get(str(update.get("item_id") or ""))
        if item is None:
            errors.append(f"unknown plan update item_id: {update.get('item_id') or ''}")
            continue
        if "status" in update:
            if update["status"] == "completed":
                dependencies = item.get("depends_on", [])
                if isinstance(dependencies, list) and any(
                    item_by_id.get(str(dependency), {}).get("status") != "completed"
                    for dependency in dependencies
                ):
                    errors.append(f"{item.get('id')}: dependencies not completed")
                    continue
            item["status"] = update["status"]
        if "actual_evidence" in update:
            evidence = update["actual_evidence"]
            if not isinstance(evidence, list) or not all(isinstance(value, str) for value in evidence):
                errors.append(f"{item.get('id')}: actual_evidence must be a list of strings")
            else:
                item["actual_evidence"] = list(dict.fromkeys(evidence))
        if "next_action" in update:
            item["next_action"] = str(update["next_action"])
    return errors


def acceptance_evidence(
    goal: dict[str, object], plan: dict[str, object], has_evidence: Callable[[str], bool]
) -> dict[str, list[str]]:
    result: dict[str, list[str]] = defaultdict(list)
    items = plan.get("items", [])
    if not isinstance(items, list):
        return {}
    for item in items:
        if not isinstance(item, dict) or item.get("status") != "completed":
            continue
        declared = item.get("evidence_plan", [])
        actual = item.get("actual_evidence", [])
        if not isinstance(declared, list) or not isinstance(actual, list):
            continue
        for ac_id in item.get("covers_ac", []):
            if isinstance(ac_id, str):
                result[ac_id].extend(
                    value for value in actual
                    if isinstance(value, str)
                    and value in declared
                    and has_evidence(value)
                    and value not in result[ac_id]
                )
    return dict(result)


def choose_next_item(plan: dict[str, object]) -> dict[str, object] | None:
    items = [item for item in plan.get("items", []) if isinstance(item, dict)]
    by_id = {str(item.get("id")): item for item in items}
    for item in items:
        if item.get("status") == "in_progress":
            return item
    for item in items:
        if item.get("status") != "pending":
            continue
        dependencies = item.get("depends_on", [])
        if isinstance(dependencies, list) and all(
            by_id.get(str(dependency), {}).get("status") == "completed"
            for dependency in dependencies
        ):
            return item
    return None


def completion_gaps(
    goal: dict[str, object], plan: dict[str, object], has_evidence: Callable[[str], bool]
) -> list[str]:
    gaps: list[str] = []
    items = plan.get("items", [])
    if not isinstance(items, list):
        return ["plan.items must be a list"]
    by_id = {str(item.get("id")): item for item in items if isinstance(item, dict)}
    for item in items:
        if not isinstance(item, dict):
            continue
        item_id = str(item.get("id") or "item")
        if item.get("status") != "completed":
            gaps.append(f"{item_id}: not completed")
            continue
        dependencies = item.get("depends_on", [])
        if isinstance(dependencies, list) and any(by_id.get(str(dep), {}).get("status") != "completed" for dep in dependencies):
            gaps.append(f"{item_id}: dependencies not completed")
        declared = item.get("evidence_plan", [])
        actual = item.get("actual_evidence", [])
        covers_ac = item.get("covers_ac", [])
        if not isinstance(declared, list) or not isinstance(actual, list):
            gaps.append(f"{item_id}: missing evidence")
            continue
        undeclared = [entry for entry in actual if entry not in declared]
        if undeclared:
            gaps.append(f"{item_id}: undeclared evidence")
        if covers_ac and not declared:
            gaps.append(f"{item_id}: missing evidence")
            continue
        if declared and not actual:
            gaps.append(f"{item_id}: missing evidence")
            continue
        missing = [entry for entry in declared if entry not in actual or not has_evidence(entry)]
        if missing:
            gaps.append(f"{item_id}: missing evidence")
    for criterion in goal.get("acceptance_criteria", []):
        if not isinstance(criterion, dict):
            continue
        ac_id = str(criterion.get("id") or "")
        if ac_id and not acceptance_evidence(goal, plan, has_evidence).get(ac_id):
            gaps.append(f"{ac_id}: missing accepted evidence")
    return gaps


def _find_cycles(dependencies: dict[str, list[str]], errors: list[str]) -> None:
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(item_id: str, path: list[str]) -> None:
        if item_id in visited:
            return
        if item_id in visiting:
            errors.append("dependency cycle: " + " -> ".join(path + [item_id]))
            return
        visiting.add(item_id)
        for dependency in dependencies.get(item_id, []):
            if dependency in dependencies:
                visit(dependency, path + [item_id])
        visiting.remove(item_id)
        visited.add(item_id)

    for item_id in dependencies:
        visit(item_id, [])
