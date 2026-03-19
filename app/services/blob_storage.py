import os
import uuid
import re
from typing import Optional
import boto3
from botocore.exceptions import ClientError
from fastapi import HTTPException, UploadFile
from app.core.config import settings
import logging

logger = logging.getLogger(__name__)


def sanitize_filename(filename: str) -> str:
    """
    Sanitize filename to be safe for storage and URLs
    - Replaces spaces with underscores
    - Removes or replaces special characters
    - Preserves file extension
    """
    # Get the name and extension
    name, ext = os.path.splitext(filename)

    # Replace spaces with underscores
    name = name.replace(' ', '_')

    # Remove any characters that aren't alphanumeric, underscore, or hyphen
    # Allow accented characters for international support (Brazilian Portuguese, etc.)
    name = re.sub(r'[^\w\-áàâãéèêíïóôõöúçñÁÀÂÃÉÈÊÍÏÓÔÕÖÚÇÑ]', '_', name)

    # Remove multiple consecutive underscores
    name = re.sub(r'_+', '_', name)

    # Remove leading/trailing underscores
    name = name.strip('_')

    # Ensure the extension is lowercase
    ext = ext.lower()

    return f"{name}{ext}"


class BlobStorageService:
    def __init__(self):
        if not settings.S3_BUCKET_NAME:
            raise ValueError("S3 bucket name not configured (S3_BUCKET_NAME)")

        try:
            # Build S3 client — use explicit creds if provided, else default credential chain
            s3_kwargs = {
                "region_name": settings.S3_REGION or settings.AWS_REGION or "us-east-2"
            }
            if settings.AWS_ACCESS_KEY_ID and settings.AWS_SECRET_ACCESS_KEY:
                s3_kwargs["aws_access_key_id"] = settings.AWS_ACCESS_KEY_ID
                s3_kwargs["aws_secret_access_key"] = settings.AWS_SECRET_ACCESS_KEY

            self.s3_client = boto3.client("s3", **s3_kwargs)
            self.bucket_name = settings.S3_BUCKET_NAME
            self.region = s3_kwargs["region_name"]
            logger.info(f"S3 storage initialized: bucket={self.bucket_name}, region={self.region}")
        except Exception as e:
            logger.error(f"Failed to initialize S3 storage service: {e}")
            raise ValueError(f"Invalid S3 configuration: {e}")

    def generate_blob_path(
        self, university_id: int, course_id: int, module_id: int, filename: str
    ) -> str:
        """Generate a structured S3 key path"""
        # Generate unique filename to avoid conflicts
        file_extension = os.path.splitext(filename)[1]
        unique_filename = f"{uuid.uuid4()}{file_extension}"

        return f"universities/{university_id}/courses/{course_id}/modules/{module_id}/{unique_filename}"

    async def upload_file(self, file: UploadFile, blob_path: str) -> str:
        """
        Upload file to S3.
        Returns the S3 object URL.
        """
        try:
            # Read file content
            content = await file.read()

            content_type = file.content_type or "application/octet-stream"

            self.s3_client.put_object(
                Bucket=self.bucket_name,
                Key=blob_path,
                Body=content,
                ContentType=content_type,
            )

            # Return the S3 object URL
            return f"https://{self.bucket_name}.s3.{self.region}.amazonaws.com/{blob_path}"

        except Exception as e:
            logger.error(f"Failed to upload file to S3: {e}")
            raise HTTPException(status_code=500, detail="Failed to upload file")

    def delete_file(self, blob_path: str) -> bool:
        """
        Delete file from S3.
        Returns True if successful (S3 delete is idempotent — always succeeds).
        """
        try:
            self.s3_client.delete_object(
                Bucket=self.bucket_name,
                Key=blob_path,
            )
            return True

        except Exception as e:
            logger.error(f"Failed to delete S3 object: {e}")
            raise HTTPException(status_code=500, detail="Failed to delete file")

    def get_download_url(self, blob_path: str, expires_in_hours: int = 1) -> str:
        """
        Generate a presigned URL for downloading the file from S3.
        """
        try:
            url = self.s3_client.generate_presigned_url(
                "get_object",
                Params={
                    "Bucket": self.bucket_name,
                    "Key": blob_path,
                },
                ExpiresIn=expires_in_hours * 3600,
            )
            return url

        except Exception as e:
            logger.error(f"Failed to generate presigned URL: {e}")
            raise HTTPException(
                status_code=500, detail="Failed to generate download URL"
            )

    async def get_file_content(self, blob_path: str) -> Optional[bytes]:
        """
        Get file content from S3 for RAG processing.
        Returns file content as bytes, or None if file doesn't exist.
        """
        try:
            response = self.s3_client.get_object(
                Bucket=self.bucket_name,
                Key=blob_path,
            )
            content = response["Body"].read()
            return content

        except ClientError as e:
            if e.response["Error"]["Code"] == "NoSuchKey":
                logger.warning(f"S3 object not found: {blob_path}")
                return None
            logger.error(f"Failed to get file content from S3: {e}")
            return None
        except Exception as e:
            logger.error(f"Failed to get file content from S3: {e}")
            return None

    async def get_file_content_as_text(self, blob_path: str, encoding: str = 'utf-8') -> Optional[str]:
        """
        Get file content as text for RAG processing.
        Returns file content as string, or None if file doesn't exist or can't be decoded.
        """
        try:
            content = await self.get_file_content(blob_path)
            if content is None:
                return None

            # Try to decode as text
            return content.decode(encoding)

        except UnicodeDecodeError:
            logger.warning(f"Could not decode file as {encoding}: {blob_path}")
            return None
        except Exception as e:
            logger.error(f"Failed to get file content as text: {e}")
            return None

    def extract_blob_path_from_url(self, blob_url: str) -> Optional[str]:
        """Extract S3 key from URL"""
        try:
            # S3 URL format: https://{bucket}.s3.{region}.amazonaws.com/{key}
            prefix = f"https://{self.bucket_name}.s3.{self.region}.amazonaws.com/"
            if blob_url.startswith(prefix):
                key = blob_url[len(prefix):].split("?")[0]  # Remove query params if present
                return key
            return None
        except Exception:
            return None


# Singleton instance
try:
    blob_storage = BlobStorageService()
except Exception as e:
    logger.error(f"Failed to initialize S3 storage: {e}")
    blob_storage = None


def get_blob_storage() -> BlobStorageService:
    """Dependency injection for blob storage service"""
    if not blob_storage:
        raise HTTPException(
            status_code=500,
            detail="S3 storage not configured or failed to initialize",
        )
    return blob_storage
