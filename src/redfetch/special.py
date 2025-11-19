from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, Optional, Set, TypedDict

from redfetch import config


class SpecialResourceInfo(TypedDict):
    is_special: bool
    is_dependency: bool
    parent_ids: Set[str]


@dataclass(frozen=True)
class SpecialIndexes:
    opted_in_specials: Set[str]
    # Map dependency_id -> set(parent_ids) where parent is opted-in and dependency is opted-in
    dependency_parents: Dict[str, Set[str]]


def _get_special_resources() -> Dict[str, dict]:
    """Return the SPECIAL_RESOURCES mapping for the current environment."""
    return config.settings.from_env(config.settings.ENV).SPECIAL_RESOURCES


def _build_indexes(special_resources: Dict[str, dict]) -> SpecialIndexes:
    opted_in_specials: Set[str] = set()
    dependency_parents: Dict[str, Set[str]] = {}

    for parent_id, parent_details in special_resources.items():
        if not parent_details.get('opt_in', False):
            continue
        opted_in_specials.add(parent_id)
        for dep_id, dep_details in parent_details.get('dependencies', {}).items():
            if dep_details and dep_details.get('opt_in', False):
                if dep_id not in dependency_parents:
                    dependency_parents[dep_id] = set()
                dependency_parents[dep_id].add(parent_id)

    return SpecialIndexes(opted_in_specials=opted_in_specials, dependency_parents=dependency_parents)


def is_resource_opted_in(resource_id: str) -> bool:
    """Return True if the given resource is an opted-in special resource."""
    special_resources = _get_special_resources()
    details = special_resources.get(str(resource_id))
    return bool(details and details.get('opt_in', False))


def get_flatten_status(resource_id: str, is_dependency: bool = False, parent_resource_id: Optional[str] = None) -> bool:
    """Return True if the resource should be flattened during extraction."""
    special_resources = _get_special_resources()
    
    if is_dependency and parent_resource_id:
        parent_resource = special_resources.get(str(parent_resource_id))
        if parent_resource and 'dependencies' in parent_resource:
            dependencies = parent_resource['dependencies']
            if str(resource_id) in dependencies:
                dependency_info = dependencies[str(resource_id)]
                if 'flatten' in dependency_info:
                    return bool(dependency_info['flatten'])
    
    special_resource = special_resources.get(str(resource_id))
    if special_resource and 'flatten' in special_resource:
        return bool(special_resource['flatten'])
    
    return False


def compute_special_status(resource_ids: Optional[Iterable[str]] = None) -> Dict[str, SpecialResourceInfo]:
    """Compute special/dependency status for IDs (or all opted-in if None)."""
    special_resources = _get_special_resources()
    indexes = _build_indexes(special_resources)

    # Determine the candidate ids to include
    if resource_ids is None:
        candidate_ids: Set[str] = set(indexes.opted_in_specials) | set(indexes.dependency_parents.keys())
    else:
        candidate_ids = set(str(rid) for rid in resource_ids)
        # For each provided parent id, if opted-in, include its opted-in dependencies
        for rid in list(candidate_ids):
            parent_details = special_resources.get(str(rid))
            if parent_details and parent_details.get('opt_in', False):
                for dep_id, dep_details in parent_details.get('dependencies', {}).items():
                    if dep_details and dep_details.get('opt_in', False):
                        candidate_ids.add(str(dep_id))

    status: Dict[str, SpecialResourceInfo] = {}
    for rid in candidate_ids:
        is_special = rid in indexes.opted_in_specials
        parent_ids = indexes.dependency_parents.get(rid, set())
        is_dependency = bool(parent_ids)
        status[rid] = SpecialResourceInfo(
            is_special=is_special,
            is_dependency=is_dependency,
            parent_ids=set(parent_ids),
        )

    return status

