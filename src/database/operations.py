"""Database operations for radio call data."""

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import desc, func

from ..models.api_models import RdioScannerUpload
from ..models.database_models import (
    RadioCall,
    UploadLog,
)
from .connection import DatabaseManager

logger = logging.getLogger(__name__)


class DatabaseOperations:
    """High-level database operations for radio call data."""

    def __init__(self, db_manager: DatabaseManager):
        """Initialize database operations.

        Args:
            db_manager: DatabaseManager instance
        """
        self.db_manager = db_manager

    def save_call(
        self,
        upload_data: RdioScannerUpload,
        client_ip: str | None = None,
        stored_path: str | None = None,
        api_key_id: str | None = None,
    ) -> int:
        """Save a radio call to the database (alias for save_radio_call).

        Args:
            upload_data: RdioScanner upload data
            client_ip: IP address of uploader
            stored_path: Path where audio file is stored
            api_key_id: ID of API key used

        Returns:
            Database ID of the created record
        """
        return self.save_radio_call(
            upload_data=upload_data,
            audio_file_path=stored_path,
            upload_ip=client_ip,
            api_key_id=api_key_id,
        )

    def save_radio_call(
        self,
        upload_data: RdioScannerUpload,
        audio_file_path: str | None = None,
        upload_ip: str | None = None,
        api_key_id: str | None = None,
    ) -> int:
        """Save a radio call to the database.

        Args:
            upload_data: RdioScanner upload data
            audio_file_path: Path where audio file is stored
            upload_ip: IP address of uploader
            api_key_id: ID of API key used

        Returns:
            Database ID of the created record
        """
        with self.db_manager.get_session() as session:
            # Create RadioCall record
            call = RadioCall(
                call_timestamp=datetime.fromtimestamp(upload_data.dateTime, tz=UTC),
                system_id=upload_data.system,
                system_label=upload_data.systemLabel,
                frequency=upload_data.frequency,
                talkgroup_id=upload_data.talkgroup,
                talkgroup_label=upload_data.talkgroupLabel,
                talkgroup_group=upload_data.talkgroupGroup,
                talkgroup_tag=upload_data.talkgroupTag,
                source_radio_id=upload_data.source,
                talker_alias=upload_data.talkerAlias,
                audio_filename=upload_data.audio_filename,
                audio_content_type=upload_data.audio_content_type,
                audio_size_bytes=upload_data.audio_size,
                audio_file_path=audio_file_path,
                patches=upload_data.patches,
                frequencies=upload_data.frequencies,
                sources=upload_data.sources,
                upload_ip=upload_ip,
                upload_api_key_id=api_key_id,
            )

            session.add(call)
            session.commit()

            # Get the ID before the session closes
            call_id = int(call.id)
            system_id = call.system_id
            talkgroup_id = call.talkgroup_id
            call_timestamp = call.call_timestamp

            logger.info(
                f"Saved radio call: ID={call_id}, System={system_id}, "
                f"TG={talkgroup_id}, Time={call_timestamp}"
            )

            return call_id

    def log_upload_attempt(
        self,
        client_ip: str,
        success: bool,
        system_id: str | None = None,
        api_key_used: str | None = None,
        user_agent: str | None = None,
        filename: str | None = None,
        file_size: int | None = None,
        content_type: str | None = None,
        error_message: str | None = None,
        response_code: int | None = None,
        processing_time_ms: float | None = None,
    ) -> None:
        """Log an upload attempt for security and debugging.

        Args:
            client_ip: IP address of client
            success: Whether upload was successful
            system_id: System ID from request
            api_key_used: API key ID used
            user_agent: User agent string
            filename: Uploaded filename
            file_size: File size in bytes
            content_type: MIME type
            error_message: Error message if failed
            response_code: HTTP response code
            processing_time_ms: Processing time in milliseconds
        """
        with self.db_manager.get_session() as session:
            log_entry = UploadLog(
                client_ip=client_ip,
                user_agent=user_agent,
                api_key_used=api_key_used,
                system_id=system_id,
                success=success,
                error_message=error_message,
                filename=filename,
                file_size=file_size,
                content_type=content_type,
                response_code=response_code,
                processing_time_ms=processing_time_ms,
            )

            session.add(log_entry)
            session.commit()

    def get_recent_calls(
        self,
        limit: int = 100,
        system_id: str | None = None,
        talkgroup_id: int | None = None,
    ) -> list[RadioCall]:
        """Get recent radio calls.

        Args:
            limit: Maximum number of calls to return
            system_id: Filter by system ID
            talkgroup_id: Filter by talkgroup ID

        Returns:
            List of RadioCall objects
        """
        with self.db_manager.get_session() as session:
            query = session.query(RadioCall)

            # Apply filters
            if system_id:
                query = query.filter(RadioCall.system_id == system_id)
            if talkgroup_id:
                query = query.filter(RadioCall.talkgroup_id == talkgroup_id)

            # Order by timestamp descending and apply limit
            calls = query.order_by(desc(RadioCall.call_timestamp)).limit(limit).all()

            return calls

    def get_statistics(self) -> dict[str, Any]:
        """Get overall statistics.

        Returns:
            Dictionary with statistics
        """
        stats: dict[str, Any] = {}

        with self.db_manager.get_session() as session:
            # Total calls
            stats["total_calls"] = session.query(RadioCall).count()

            # Calls today
            today_start = datetime.now().replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            stats["calls_today"] = (
                session.query(RadioCall)
                .filter(RadioCall.call_timestamp >= today_start)
                .count()
            )

            # Calls last hour
            hour_ago = datetime.now() - timedelta(hours=1)
            stats["calls_last_hour"] = (
                session.query(RadioCall)
                .filter(RadioCall.call_timestamp >= hour_ago)
                .count()
            )

            # System breakdown
            system_counts = (
                session.query(
                    RadioCall.system_id, func.count(RadioCall.id).label("count")
                )
                .group_by(RadioCall.system_id)
                .all()
            )

            systems: dict[str, int] = {
                str(sys_id): count for sys_id, count in system_counts
            }
            stats["systems"] = systems

            # Talkgroup breakdown (top 20)
            tg_counts = (
                session.query(
                    RadioCall.talkgroup_id,
                    RadioCall.talkgroup_label,
                    func.count(RadioCall.id).label("count"),
                )
                .filter(RadioCall.talkgroup_id.isnot(None))
                .group_by(RadioCall.talkgroup_id, RadioCall.talkgroup_label)
                .order_by(desc("count"))
                .limit(20)
                .all()
            )

            talkgroups: dict[str, int] = {
                f"{tg_id} ({tg_label or 'Unknown'})": count
                for tg_id, tg_label, count in tg_counts
            }
            stats["talkgroups"] = talkgroups

            # Upload sources
            source_counts = (
                session.query(
                    RadioCall.upload_ip, func.count(RadioCall.id).label("count")
                )
                .filter(RadioCall.upload_ip.isnot(None))
                .group_by(RadioCall.upload_ip)
                .all()
            )

            upload_sources: dict[str, int] = {
                str(ip): count for ip, count in source_counts
            }
            stats["upload_sources"] = upload_sources

            # Storage info
            stats["audio_files_count"] = (
                session.query(RadioCall)
                .filter(RadioCall.audio_file_path.isnot(None))
                .count()
            )

            # Calculate storage used (sum of file sizes)
            total_size = (
                session.query(func.sum(RadioCall.audio_size_bytes))
                .filter(RadioCall.audio_size_bytes.isnot(None))
                .scalar()
                or 0
            )

            storage_used_mb: float = float(total_size) / (1024 * 1024)
            stats["storage_used_mb"] = storage_used_mb

        return stats

    def cleanup_old_data(self, days_to_keep: int) -> None:
        """Clean up old data from the database.

        Args:
            days_to_keep: Number of days of data to keep
        """
        cutoff_date = datetime.now() - timedelta(days=days_to_keep)

        with self.db_manager.get_session() as session:
            # Delete old radio calls
            deleted_calls = (
                session.query(RadioCall)
                .filter(RadioCall.call_timestamp < cutoff_date)
                .delete()
            )

            # Delete old upload logs
            deleted_logs = (
                session.query(UploadLog)
                .filter(UploadLog.timestamp < cutoff_date)
                .delete()
            )

            session.commit()

            logger.info(
                f"Cleaned up old data: {deleted_calls} calls, {deleted_logs} logs"
            )

            # Vacuum database to reclaim space
            self.db_manager.vacuum()

    def query_calls(
        self,
        filters: dict[str, Any] | None = None,
        page: int = 1,
        per_page: int = 20,
        sort_by: str = "call_timestamp",
        sort_order: str = "desc",
    ) -> dict[str, Any]:
        """Query radio calls with filtering and pagination.

        Args:
            filters: Filter criteria
            page: Page number (1-based)
            per_page: Items per page
            sort_by: Field to sort by
            sort_order: Sort order (asc/desc)

        Returns:
            Dictionary with calls, total count, and pagination info
        """
        with self.db_manager.get_session() as session:
            query = session.query(RadioCall)

            # Apply filters
            if filters:
                if "system_id" in filters:
                    query = query.filter(RadioCall.system_id == filters["system_id"])
                if "talkgroup_id" in filters:
                    query = query.filter(
                        RadioCall.talkgroup_id == filters["talkgroup_id"]
                    )
                if "source_radio_id" in filters:
                    query = query.filter(
                        RadioCall.source_radio_id == filters["source_radio_id"]
                    )
                if "frequency" in filters:
                    query = query.filter(RadioCall.frequency == filters["frequency"])
                if "date_from" in filters:
                    query = query.filter(
                        RadioCall.call_timestamp >= filters["date_from"]
                    )
                if "date_to" in filters:
                    query = query.filter(RadioCall.call_timestamp <= filters["date_to"])

            # Get total count
            total = query.count()

            # Apply sorting
            sort_column = getattr(RadioCall, sort_by, RadioCall.call_timestamp)
            if sort_order == "desc":
                query = query.order_by(desc(sort_column))
            else:
                query = query.order_by(sort_column)

            # Apply pagination
            offset = (page - 1) * per_page
            query = query.offset(offset).limit(per_page)

            # Execute query
            calls = query.all()

            # Convert to dict
            result_calls = []
            for call in calls:
                result_calls.append(
                    {
                        "id": call.id,
                        "call_timestamp": call.call_timestamp,
                        "system_id": call.system_id,
                        "system_label": call.system_label,
                        "talkgroup_id": call.talkgroup_id,
                        "talkgroup_label": call.talkgroup_label,
                        "frequency": call.frequency,
                        "source_radio_id": call.source_radio_id,
                        "talker_alias": call.talker_alias,
                        "audio_filename": call.audio_filename,
                        "audio_size_bytes": call.audio_size_bytes,
                        "audio_file_path": call.audio_file_path,
                    }
                )

            total_pages = (total + per_page - 1) // per_page

            return {
                "calls": result_calls,
                "total": total,
                "total_pages": total_pages,
            }

    def get_call_by_id(self, call_id: int) -> dict[str, Any] | None:
        """Get a specific call by ID.

        Args:
            call_id: Database ID of the call

        Returns:
            Call data or None if not found
        """
        with self.db_manager.get_session() as session:
            call = session.query(RadioCall).filter(RadioCall.id == call_id).first()

            if not call:
                return None

            return {
                "id": call.id,
                "call_timestamp": call.call_timestamp,
                "system_id": call.system_id,
                "system_label": call.system_label,
                "talkgroup_id": call.talkgroup_id,
                "talkgroup_label": call.talkgroup_label,
                "frequency": call.frequency,
                "source_radio_id": call.source_radio_id,
                "talker_alias": call.talker_alias,
                "audio_filename": call.audio_filename,
                "audio_size_bytes": call.audio_size_bytes,
                "audio_file_path": call.audio_file_path,
                "patches": call.patches,
                "frequencies": call.frequencies,
                "sources": call.sources,
                "created_at": call.created_at,
                "upload_ip": call.upload_ip,
            }

    def get_systems_summary(self) -> list[dict[str, Any]]:
        """Get summary statistics for all systems.

        Returns:
            List of system summaries
        """
        with self.db_manager.get_session() as session:
            systems = (
                session.query(
                    RadioCall.system_id,
                    RadioCall.system_label,
                    func.count(RadioCall.id).label("total_calls"),
                    func.min(RadioCall.call_timestamp).label("first_seen"),
                    func.max(RadioCall.call_timestamp).label("last_seen"),
                )
                .group_by(RadioCall.system_id, RadioCall.system_label)
                .all()
            )

            result = []
            for system in systems:
                # Get top talkgroups for this system
                top_tgs = (
                    session.query(
                        RadioCall.talkgroup_id, func.count(RadioCall.id).label("count")
                    )
                    .filter(RadioCall.system_id == system.system_id)
                    .filter(RadioCall.talkgroup_id.isnot(None))
                    .group_by(RadioCall.talkgroup_id)
                    .order_by(desc("count"))
                    .limit(10)
                    .all()
                )

                top_talkgroups = {str(tg_id): count for tg_id, count in top_tgs}

                result.append(
                    {
                        "system_id": system.system_id,
                        "system_label": system.system_label,
                        "total_calls": system.total_calls,
                        "first_seen": system.first_seen,
                        "last_seen": system.last_seen,
                        "top_talkgroups": top_talkgroups,
                    }
                )

            return result

    def get_talkgroups_summary(
        self, system_id: str | None = None, min_calls: int = 1
    ) -> list[dict[str, Any]]:
        """Get summary statistics for talkgroups.

        Args:
            system_id: Optional system ID to filter by
            min_calls: Minimum number of calls to include

        Returns:
            List of talkgroup summaries
        """
        with self.db_manager.get_session() as session:
            query = session.query(
                RadioCall.talkgroup_id,
                RadioCall.talkgroup_label,
                RadioCall.system_id,
                func.count(RadioCall.id).label("total_calls"),
                func.max(RadioCall.call_timestamp).label("last_heard"),
            ).filter(RadioCall.talkgroup_id.isnot(None))

            if system_id:
                query = query.filter(RadioCall.system_id == system_id)

            query = (
                query.group_by(
                    RadioCall.talkgroup_id,
                    RadioCall.talkgroup_label,
                    RadioCall.system_id,
                )
                .having(func.count(RadioCall.id) >= min_calls)
                .order_by(desc("total_calls"))
            )

            talkgroups = query.all()

            return [
                {
                    "talkgroup_id": tg.talkgroup_id,
                    "talkgroup_label": tg.talkgroup_label,
                    "system_id": tg.system_id,
                    "total_calls": tg.total_calls,
                    "last_heard": tg.last_heard,
                }
                for tg in talkgroups
            ]
