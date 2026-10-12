from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

from app.verification.models import VerificationResult

with workflow.unsafe.imports_passed_through():
    from app.verification.activities import verify_service


@workflow.defn
class VerificationWorkflow:
    @workflow.run
    async def run(self, session_id: str, service_image: str, test_image: str,
                  language: str, tracked_tests: list[str] | None = None,
                  suite_dir: str = 'tests') -> VerificationResult:
        return await workflow.execute_activity(
            verify_service,
            args=[session_id, service_image, test_image, language, tracked_tests, suite_dir],
            start_to_close_timeout=timedelta(minutes=15),
            heartbeat_timeout=timedelta(minutes=2),
            retry_policy=RetryPolicy(maximum_attempts=1),
        )
