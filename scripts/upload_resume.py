"""
Upload a resume DOCX to MinIO storage.

Usage:
  python scripts/upload_resume.py --person sai --variant sai_infra --file /path/to/resume.docx
  python scripts/upload_resume.py --person gf  --variant gf_base  --file /path/to/gf_resume.docx

Run this once per resume variant before starting the system.
The file will be stored at: resumes/{person}/{variant}.docx in MinIO.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, "/app")

from minio import Minio
from shared.config import settings


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--person", required=True, help="Profile ID: sai or gf")
    parser.add_argument("--variant", required=True, help="Variant ID e.g. sai_infra")
    parser.add_argument("--file", required=True, help="Path to the .docx file")
    args = parser.parse_args()

    local_path = Path(args.file)
    if not local_path.exists():
        print(f"ERROR: File not found: {local_path}")
        sys.exit(1)

    if local_path.suffix.lower() != ".docx":
        print("ERROR: Only .docx files are supported")
        sys.exit(1)

    client = Minio(
        settings.minio_endpoint,
        access_key=settings.minio_access_key,
        secret_key=settings.minio_secret_key,
        secure=settings.minio_secure,
    )

    if not client.bucket_exists(settings.minio_bucket):
        client.make_bucket(settings.minio_bucket)
        print(f"Created bucket: {settings.minio_bucket}")

    # Also copy to local profiles directory for DOCX template processing
    local_profiles = Path("/app/profiles") / args.person / "resumes" / f"{args.variant}.docx"
    local_profiles.parent.mkdir(parents=True, exist_ok=True)
    import shutil
    shutil.copy2(str(local_path), str(local_profiles))
    print(f"Copied to local: {local_profiles}")

    # Upload to MinIO
    object_name = f"resumes/{args.person}/{args.variant}.docx"
    client.fput_object(
        settings.minio_bucket,
        object_name,
        str(local_path),
        content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )
    print(f"Uploaded to MinIO: {settings.minio_bucket}/{object_name}")
    print("\nResume ready. The system will use this as the base template for tailoring.")


if __name__ == "__main__":
    main()
