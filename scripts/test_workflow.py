"""Manual trigger for MatchAndProcessWorkflow — run inside the worker container to verify
the full pipeline: match → resume → cover → study guide → MinIO → Telegram → Sheets."""
import asyncio
import sys

from temporalio.client import Client

sys.path.insert(0, "/app")
from shared.config import settings

TEST_JOB = {
    "id": "test-001",
    "source_id": "test-001",
    "source": "greenhouse",
    "company": "Stripe",
    "title": "Staff Site Reliability Engineer",
    "description_text": (
        "We are looking for a Staff SRE to own reliability for Stripe's payment "
        "infrastructure. You will drive Kubernetes platform improvements, lead incident "
        "response, own SLOs and error budgets, and build Terraform-based IaC. "
        "8+ years experience in SRE or DevOps. Strong Kubernetes, Terraform, AWS, "
        "Prometheus/Grafana, on-call rotations, and postmortem culture required. "
        "Experience with service mesh (Istio/Linkerd), GitOps (ArgoCD), and multi-cloud "
        "architectures a strong plus. H1B transfer welcome."
    ),
    "url": "https://stripe.com/jobs/test",
    "location": "San Francisco, CA",
    "description_html": "",
    "remote": False,
    "department": "Infrastructure",
    "scraped_at": "2026-05-18T00:00:00",
}


async def main():
    print(f"Connecting to Temporal at {settings.temporal_host} ...")
    client = await Client.connect(settings.temporal_host)

    print("Triggering MatchAndProcessWorkflow for Stripe Staff SRE ...")
    import time
    run_id = f"test-match-{int(time.time())}"
    handle = await client.start_workflow(
        "MatchAndProcessWorkflow",
        args=[TEST_JOB],
        id=run_id,
        task_queue=settings.temporal_task_queue,
    )
    print(f"Workflow started: {handle.id}")
    print("Waiting for result (this takes 2-5 min while Claude generates docs)...\n")

    result = await handle.result()
    print("\n=== RESULT ===")
    for person, outcome in result.items():
        print(f"\n[{person}]")
        if outcome.get("matched"):
            print(f"  Matched: YES  Score: {outcome.get('score', 0):.0%}")
            print(f"  Resume URL: {outcome.get('resume_url', 'N/A')}")
        else:
            print(f"  Matched: NO  Score: {outcome.get('score', 0):.0%}")
        if "error" in outcome:
            print(f"  ERROR: {outcome['error']}")


if __name__ == "__main__":
    asyncio.run(main())
