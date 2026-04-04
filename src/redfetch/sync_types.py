from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, Union

from pydantic import BaseModel, ConfigDict, Field


SyncMode = Literal["full", "targeted"]
DesiredSource = Literal["watching", "licensed", "special", "explicit", "dependency"]
TargetKind = Literal["root", "dependency"]
RemoteStatus = Literal[
    "manifest_current",
    "downloadable",
    "access_denied",
    "no_files",
    "multiple_files",
    "not_found",
    "fetch_error",
]
ActionType = Literal["download", "skip", "block", "untrack"]
PlanReason = Literal[
    "outdated",
    "not_installed",
    "already_current",
    "install_context_changed",
    "not_desired",
    "access_denied",
    "no_files",
    "multiple_files",
    "not_found",
    "fetch_error",
    "parent_blocked",
    "parent_failed",
    "dependency_cycle",
    "unknown_category",
]
ResultOutcome = Literal["downloaded", "skipped", "blocked", "untracked", "error"]

@dataclass(frozen=True, slots=True)
class ReasonInfo:
    """Display metadata for a single PlanReason value."""

    message: str
    quiet: bool = False
    summary_label: str | None = None


PLAN_REASON_META: dict[PlanReason, ReasonInfo] = {
    "access_denied":           ReasonInfo("You don't have permission to download this resource."),
    "no_files":                ReasonInfo("This resource has no downloadable files.", quiet=True, summary_label="Resources with no files"),
    "multiple_files":          ReasonInfo("This resource has multiple files and cannot be auto-synced. Ask the author to release it as a .zip file."),
    "not_found":               ReasonInfo("This resource was not found."),
    "fetch_error":             ReasonInfo("Failed to retrieve this resource from the server."),
    "unknown_category":        ReasonInfo("This resource's category is not mapped to an install location, but you can specify one manually in settings.local.toml"),
    "parent_blocked":          ReasonInfo("Skipped because its parent resource is blocked.", quiet=True),
    "parent_failed":           ReasonInfo("Skipped because its parent resource failed to download."),
    "dependency_cycle":        ReasonInfo("Skipped due to a circular dependency."),
    "not_desired":             ReasonInfo("No longer watched or licensed; untracking."),
    "outdated":                ReasonInfo("A newer version is available."),
    "not_installed":           ReasonInfo("Not yet installed locally."),
    "already_current":         ReasonInfo("Already up to date."),
    "install_context_changed": ReasonInfo("Install location or settings changed; re-downloading."),
}


def reason_message(reason: PlanReason) -> str:
    meta = PLAN_REASON_META.get(reason)
    return meta.message if meta else reason

SyncEvent = Union[
    tuple[Literal["total"], int, None],
    tuple[Literal["add_total"], int, None],
    tuple[Literal["start"], str, str | None],
    tuple[Literal["done"], str, ResultOutcome],
]
SyncEventCallback = Callable[[SyncEvent], None]


def make_root_target_key(resource_id: str) -> str:
    return f"/{resource_id}/"


def make_child_target_key(parent_target_key: str, resource_id: str) -> str:
    if not parent_target_key.startswith("/") or not parent_target_key.endswith("/"):
        raise ValueError(f"Invalid parent target key: {parent_target_key}")
    return f"{parent_target_key}{resource_id}/"


def parse_target_key(target_key: str) -> list[str]:
    if not target_key.startswith("/") or not target_key.endswith("/"):
        raise ValueError(f"Invalid target key: {target_key}")
    return [segment for segment in target_key.strip("/").split("/") if segment]


def target_depth(target_key: str) -> int:
    return len(parse_target_key(target_key))


class SyncModel(BaseModel):
    """Defensive, allows only known fields to be set."""

    model_config = ConfigDict(extra="forbid")


class TargetIdentity(SyncModel):
    """The same resource_id can appear as both a root and a dependency target
    (or under multiple parents), each with its own target_key and planning."""

    target_key: str
    resource_id: str
    parent_id: str | None = None
    parent_target_key: str | None = None
    root_resource_id: str
    target_kind: TargetKind



class DesiredInstallTarget(TargetIdentity):
    """One concrete place a resource should be installed this run."""

    sources: set[DesiredSource] = Field(default_factory=set)
    title: str | None = None
    category_id: int | None = None
    resolved_path: str | None = None
    subfolder: str | None = None
    flatten: bool = False
    protected_files: list[str] = Field(default_factory=list)
    explicit_root: bool = False


class DesiredSet(SyncModel):
    """The full discovered target set for the run."""

    mode: SyncMode
    requested_root_ids: set[str] = Field(default_factory=set)
    resource_ids: set[str] = Field(default_factory=set)
    install_targets: dict[str, DesiredInstallTarget] = Field(default_factory=dict)

    def add_target(self, target: DesiredInstallTarget) -> DesiredInstallTarget:
        self.resource_ids.add(target.resource_id)
        existing = self.install_targets.get(target.target_key)
        if existing is None:
            self.install_targets[target.target_key] = target
            return target

        # Same target_key seen again (e.g. both watched and licensed) -- merge
        existing.sources.update(target.sources)
        existing.title = existing.title or target.title
        existing.category_id = existing.category_id if existing.category_id is not None else target.category_id
        existing.resolved_path = existing.resolved_path or target.resolved_path
        existing.subfolder = existing.subfolder or target.subfolder
        existing.flatten = existing.flatten or target.flatten
        existing.protected_files = existing.protected_files or target.protected_files
        existing.explicit_root = existing.explicit_root or target.explicit_root
        return existing

    def resource_targets(self, resource_id: str) -> list[DesiredInstallTarget]:
        return [
            target
            for target in self.install_targets.values()
            if target.resource_id == resource_id
        ]


class RemoteArtifact(SyncModel):
    """What to actually download for a resource."""

    file_id: int
    filename: str
    download_url: str
    file_hash: str | None = None


class RemoteResourceState(SyncModel):
    """Server-side view of a resource: version, status, and optional artifact."""

    resource_id: str
    title: str | None = None
    category_id: int | None = None
    version_id: int | None = None
    status: RemoteStatus
    artifact: RemoteArtifact | None = None
    source_note: str | None = None

    @property
    def is_resolved(self) -> bool:
        return self.version_id is not None and self.artifact is not None


class RemoteSnapshot(SyncModel):
    """All remote resource states collected for this run."""

    resources: dict[str, RemoteResourceState] = Field(default_factory=dict)


class LocalInstallState(TargetIdentity):
    """Local DB record for a single install target."""

    title: str | None = None
    category_id: int | None = None
    version_local: int | None = None
    version_remote: int | None = None
    resolved_path: str | None = None
    subfolder: str | None = None
    flatten: bool = False
    protected_files: list[str] = Field(default_factory=list)
    is_special: bool = False
    is_watching: bool = False
    is_licensed: bool = False
    is_explicit: bool = False
    is_dependency: bool = False


class LocalSnapshot(SyncModel):
    """Everything we think is installed according to the DB."""

    install_targets: dict[str, LocalInstallState] = Field(default_factory=dict)

    def roots_in_closure(self, root_ids: set[str]) -> list[LocalInstallState]:
        return [
            state
            for state in self.install_targets.values()
            if state.root_resource_id in root_ids
        ]


class PlannedAction(TargetIdentity):
    """What the planner decided to do with one install target, and why."""

    action: ActionType
    reason: PlanReason
    title: str | None = None
    category_id: int | None = None
    remote_version: int | None = None
    artifact: RemoteArtifact | None = None
    resolved_path: str | None = None
    subfolder: str | None = None
    flatten: bool = False
    protected_files: list[str] = Field(default_factory=list)
    explicit_root: bool = False

    @classmethod
    def from_desired(
        cls,
        target: DesiredInstallTarget,
        *,
        action: ActionType,
        reason: PlanReason,
        title: str | None,
        category_id: int | None,
        remote_version: int | None,
        artifact: RemoteArtifact | None,
        resolved_path: str | None,
        subfolder: str | None,
    ) -> PlannedAction:
        return cls(
            target_key=target.target_key,
            resource_id=target.resource_id,
            parent_id=target.parent_id,
            parent_target_key=target.parent_target_key,
            root_resource_id=target.root_resource_id,
            target_kind=target.target_kind,
            action=action,
            reason=reason,
            title=title,
            category_id=category_id,
            remote_version=remote_version,
            artifact=artifact,
            resolved_path=resolved_path,
            subfolder=subfolder,
            flatten=target.flatten,
            protected_files=target.protected_files,
            explicit_root=target.explicit_root,
        )

    @classmethod
    def untrack_from_local(cls, local_state: LocalInstallState) -> PlannedAction:
        return cls(
            target_key=local_state.target_key,
            resource_id=local_state.resource_id,
            parent_id=local_state.parent_id,
            parent_target_key=local_state.parent_target_key,
            root_resource_id=local_state.root_resource_id,
            target_kind=local_state.target_kind,
            action="untrack",
            reason="not_desired",
            title=local_state.title,
            category_id=local_state.category_id,
            remote_version=local_state.version_remote,
            artifact=None,
            resolved_path=local_state.resolved_path,
            subfolder=local_state.subfolder,
            flatten=local_state.flatten,
            protected_files=local_state.protected_files,
            explicit_root=local_state.is_explicit,
        )


class ExecutionPlan(SyncModel):
    """The full set of decisions handed to the executor."""

    actions: dict[str, PlannedAction] = Field(default_factory=dict)

    def action_counts(self) -> dict[str, int]:
        counts = {"download": 0, "skip": 0, "block": 0, "untrack": 0}
        for action in self.actions.values():
            counts[action.action] += 1
        return counts


class ExecutionResultItem(SyncModel):
    """One target's result: what happened and why."""

    target_key: str
    resource_id: str
    outcome: ResultOutcome
    reason: PlanReason
    written_version: int | None = None
    error_detail: str | None = None


class ExecutionResult(SyncModel):
    """Collected results handed to the recorder after execution."""

    items: dict[str, ExecutionResultItem] = Field(default_factory=dict)
    was_cancelled: bool = False

    def has_errors(self) -> bool:
        return self.was_cancelled or any(
            item.outcome == "error" for item in self.items.values()
        )
