import hashlib
from collections.abc import Callable
from datetime import datetime, timezone

from debouw.config import Settings
from debouw.models.permit import PermitProject, RiskAssessment


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class StubRiskEngine:
    """Phase 0 stub. Returns deterministic all-zero RiskAssessment.
    Replaced by the real engine in Phase 2.

    The ``now`` parameter accepts an injectable clock callable so Phase 1+
    snapshot tests can pin ``generated_at`` without monkey-patching globals.
    Default behaviour is unchanged (wall-clock UTC).
    """

    def __init__(
        self,
        settings: Settings,
        now: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._engine_version = settings.engine_version
        self._now = now

    async def classify(self, project: PermitProject) -> RiskAssessment:
        inputs_hash = hashlib.sha256(project.external_id.encode()).hexdigest()
        return RiskAssessment(
            project_external_id=project.external_id,
            overall_score=0.0,
            expected_delay_days=0.0,
            confidence=0.0,
            summary="Phase 0 stub — no rules wired yet",
            top_risks=[],
            engine_version=self._engine_version,
            calibration_regime="post_2026_reform",
            generated_at=self._now(),
            inputs_hash=inputs_hash,
        )
