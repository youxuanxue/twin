from __future__ import annotations

from collections import defaultdict


ITEM_STATUSES = frozenset({"pending", "in_progress", "completed", "blocked", "deferred"})


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


def acceptance_evidence(goal: dict[str, object], plan: dict[str, object]) -> dict[str, list[str]]:
    result: dict[str, list[str]] = defaultdict(list)
    items = plan.get("items", [])
    if not isinstance(items, list):
        return {}
    for item in items:
        if not isinstance(item, dict) or item.get("status") != "completed":
            continue
        evidence = item.get("actual_evidence", [])
        if not isinstance(evidence, list):
            continue
        for ac_id in item.get("covers_ac", []):
            if isinstance(ac_id, str):
                result[ac_id].extend(value for value in evidence if isinstance(value, str) and value not in result[ac_id])
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


def completion_gaps(goal: dict[str, object], plan: dict[str, object], has_evidence: callable) -> list[str]:
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
        if not isinstance(declared, list) or not isinstance(actual, list) or not actual:
            gaps.append(f"{item_id}: missing evidence")
            continue
        missing = [entry for entry in declared if entry not in actual or not has_evidence(entry)]
        if missing:
            gaps.append(f"{item_id}: missing evidence")
    for criterion in goal.get("acceptance_criteria", []):
        if not isinstance(criterion, dict):
            continue
        ac_id = str(criterion.get("id") or "")
        if ac_id and not acceptance_evidence(goal, plan).get(ac_id):
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
