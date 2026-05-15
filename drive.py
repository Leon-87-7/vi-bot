"""Google Drive upload and Google Sheets logging.

Full implementation is delivered in issue #4.  These stubs define the
interface consumed by pipeline.py so the module can be imported and tested
with mocks today.
"""


class DriveUploadError(Exception):
    """Raised when Drive upload fails after exhausting retries."""


async def upload_report(content: str, filename: str, folder_id: str) -> str:
    """Upload *content* as *filename* to *folder_id* on Google Drive.

    Retries with exponential backoff (2–3 attempts) before raising
    DriveUploadError.  Returns the shareable file URL on success.
    """
    raise NotImplementedError("upload_report: implemented in issue #4")


async def append_to_sheets(
    job: dict,
    title: str,
    drive_url: str,
    *,
    sheets_id: str,
) -> None:
    """Append one row to the Google Sheets log for *job*.

    Row columns: URL, title, pipeline_type, drive_url,
                 processing_time_ms, created_at.
    """
    raise NotImplementedError("append_to_sheets: implemented in issue #4")
