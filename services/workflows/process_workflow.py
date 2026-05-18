"""
MatchAndProcessWorkflow — full pipeline for a single new job:

  1. Load both user profiles (Sai + GF)
  2. For each profile:
     a. Match job against profile (Claude)
     b. If score >= threshold:
        - Tailor resume via resume.yaml (Claude REWRITE_PROMPT → DOCX + defense.md + prep.md)
        - Generate cover letter (Claude)
        - Upload all docs to MinIO
        - Send rich Telegram notification (4 buttons + files as reply_to)
        - Track in PostgreSQL
"""
from __future__ import annotations

import logging
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from worker.activities import (
        match_job,
        tailor_resume,
        generate_cover_letter,
        upload_document,
        notify_telegram,
        track_application,
        load_profile_activity,
        sync_to_sheet,
    )

log = logging.getLogger("workflow.process")

_RETRY_FAST = RetryPolicy(
    initial_interval=timedelta(seconds=3),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=1),
    maximum_attempts=3,
)
_RETRY_AI = RetryPolicy(
    initial_interval=timedelta(seconds=10),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=5),
    maximum_attempts=3,
)


@workflow.defn
class MatchAndProcessWorkflow:
    """Full pipeline for processing one new job against all user profiles."""

    @workflow.run
    async def run(self, job_dict: dict) -> dict:
        job_title = job_dict.get("title", "unknown")
        job_company = job_dict.get("company", "unknown")
        workflow.logger.info("Processing: %s @ %s", job_title, job_company)

        results = {}

        for person_id in ["sai", "gf"]:
            try:
                result = await self._process_for_person(job_dict, person_id)
                results[person_id] = result
            except Exception as e:
                workflow.logger.error("Pipeline failed for %s: %s", person_id, e)
                results[person_id] = {"error": str(e)}

        return results

    async def _process_for_person(self, job_dict: dict, person_id: str) -> dict:
        # Load profile inside activity (can't do I/O directly in workflow)
        profile_dict = await workflow.execute_activity(
            load_profile_activity,
            args=[person_id],
            schedule_to_close_timeout=timedelta(seconds=15),
            retry_policy=_RETRY_FAST,
        )

        # Step 1: Match
        match_dict: dict = await workflow.execute_activity(
            match_job,
            args=[job_dict, profile_dict],
            schedule_to_close_timeout=timedelta(seconds=60),
            retry_policy=_RETRY_AI,
        )

        threshold = profile_dict.get("match_threshold", 0.65)
        score = match_dict.get("score", 0.0)
        visa_ok = match_dict.get("visa_ok", True)

        if score < threshold or not visa_ok:
            workflow.logger.info(
                "[%s] Skip %s @ %s — score=%.2f visa_ok=%s",
                person_id, job_dict.get("title"), job_dict.get("company"), score, visa_ok,
            )
            return {"matched": False, "score": score}

        workflow.logger.info(
            "[%s] Match! %s @ %s — score=%.2f",
            person_id, job_dict.get("title"), job_dict.get("company"), score,
        )

        # Step 2: Tailor resume (returns dict with resume_path, defense_path, prep_path)
        tailor_result: dict = await workflow.execute_activity(
            tailor_resume,
            args=[job_dict, profile_dict, match_dict],
            schedule_to_close_timeout=timedelta(minutes=5),
            retry_policy=_RETRY_AI,
        )
        resume_path = tailor_result.get("resume_path", "")
        defense_path = tailor_result.get("defense_path", "")
        prep_path = tailor_result.get("prep_path", "")
        prep_summary = tailor_result.get("prep_summary", "")

        # Step 3: Cover letter
        cover_path: str = await workflow.execute_activity(
            generate_cover_letter,
            args=[job_dict, profile_dict, match_dict],
            schedule_to_close_timeout=timedelta(minutes=2),
            retry_policy=_RETRY_AI,
        )

        # Step 4: Upload all docs to MinIO
        job_company = job_dict.get("company", "company").replace(" ", "-")
        job_title = job_dict.get("title", "role").replace(" ", "-")
        job_id = job_dict.get("id", "id")

        resume_minio = f"{person_id}/{job_id}/{job_company}_{job_title}_resume.docx"
        cover_minio = f"{person_id}/{job_id}/{job_company}_{job_title}_cover.txt"
        defense_minio = f"{person_id}/{job_id}/{job_company}_{job_title}_defense.md"
        prep_minio = f"{person_id}/{job_id}/{job_company}_{job_title}_prep.md"

        resume_url: str = await workflow.execute_activity(
            upload_document,
            args=[resume_path, resume_minio],
            schedule_to_close_timeout=timedelta(minutes=2),
            retry_policy=_RETRY_FAST,
        )
        cover_url: str = await workflow.execute_activity(
            upload_document,
            args=[cover_path, cover_minio],
            schedule_to_close_timeout=timedelta(minutes=2),
            retry_policy=_RETRY_FAST,
        )

        # Upload defense notes (non-fatal if missing)
        defense_url = ""
        if defense_path:
            try:
                defense_url = await workflow.execute_activity(
                    upload_document,
                    args=[defense_path, defense_minio],
                    schedule_to_close_timeout=timedelta(minutes=1),
                    retry_policy=_RETRY_FAST,
                )
            except Exception as e:
                workflow.logger.warning("Defense upload failed (non-fatal): %s", e)

        # Upload prep guide (non-fatal if missing)
        prep_url = ""
        if prep_path:
            try:
                prep_url = await workflow.execute_activity(
                    upload_document,
                    args=[prep_path, prep_minio],
                    schedule_to_close_timeout=timedelta(minutes=1),
                    retry_policy=_RETRY_FAST,
                )
            except Exception as e:
                workflow.logger.warning("Prep upload failed (non-fatal): %s", e)

        docs_dict = {
            "resume_minio_path": resume_minio,
            "resume_url": resume_url,
            "cover_letter_minio_path": cover_minio,
            "cover_letter_url": cover_url,
            "defense_minio_path": defense_minio if defense_url else "",
            "prep_minio_path": prep_minio if prep_url else "",
            "prep_summary": prep_summary,
        }

        # Step 5: Notify (non-fatal if fails)
        try:
            await workflow.execute_activity(
                notify_telegram,
                args=[job_dict, profile_dict, match_dict, docs_dict],
                schedule_to_close_timeout=timedelta(seconds=60),
                retry_policy=_RETRY_FAST,
            )
        except Exception as e:
            workflow.logger.warning("Telegram notify failed (non-fatal): %s", e)

        # Step 6: Track in DB (non-fatal if fails)
        try:
            await workflow.execute_activity(
                track_application,
                args=[job_dict, profile_dict, match_dict, docs_dict],
                schedule_to_close_timeout=timedelta(seconds=15),
                retry_policy=_RETRY_FAST,
            )
        except Exception as e:
            workflow.logger.warning("DB track failed (non-fatal): %s", e)

        # Step 7: Sync to Google Sheet (non-fatal if fails)
        try:
            await workflow.execute_activity(
                sync_to_sheet,
                args=[job_dict, profile_dict, match_dict, docs_dict],
                schedule_to_close_timeout=timedelta(seconds=20),
                retry_policy=_RETRY_FAST,
            )
        except Exception as e:
            workflow.logger.warning("Sheets sync failed (non-fatal): %s", e)

        return {"matched": True, "score": score, "resume_url": resume_url}
