from typing import Protocol

from debouw.models.permit import PermitProject, RiskAssessment


class RiskEngine(Protocol):
    """Pure function. No network calls — all overlays must be on
    project.overlays. Reads from LanceDB at ~/debouw/lancedb/.
    Never mutates the input."""

    async def classify(self, project: PermitProject) -> RiskAssessment: ...
