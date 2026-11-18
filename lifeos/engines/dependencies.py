"""Dependency graph rules. Pure functions over an adjacency mapping {task_id: {prerequisite_ids}}."""

from __future__ import annotations

from typing import Mapping, Set

from lifeos.errors import DependencyError


def would_create_cycle(graph: Mapping[int, Set[int]], task_id: int, prerequisite_id: int) -> bool:
    """True if making task_id depend on prerequisite_id closes a loop.

    That happens when prerequisite_id already (transitively) depends on task_id.
    """
    if task_id == prerequisite_id:
        return True
    stack, seen = [prerequisite_id], set()
    while stack:
        node = stack.pop()
        if node == task_id:
            return True
        if node in seen:
            continue
        seen.add(node)
        stack.extend(graph.get(node, ()))
    return False


def check_new_dependency(graph: Mapping[int, Set[int]], task_id: int, prerequisite_id: int) -> None:
    if task_id == prerequisite_id:
        raise DependencyError("A task cannot depend on itself.")
    if would_create_cycle(graph, task_id, prerequisite_id):
        raise DependencyError(
            f"Circular dependency: task {prerequisite_id} already depends (directly or indirectly) on task {task_id}."
        )
