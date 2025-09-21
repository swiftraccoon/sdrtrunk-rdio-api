# WhisperX Audio Transcription Integration Plan

## Executive Summary

This document outlines a comprehensive plan for integrating WhisperX audio transcription into the sdrtrunk-rdio-api system. The integration will provide automatic speech-to-text capabilities for radio call recordings while maintaining the system's existing architecture patterns, security standards, and performance characteristics.

## Current System Analysis

### Architecture Overview

The sdrtrunk-rdio-api follows a clean, layered architecture:

- **API Layer** (`src/api/`): FastAPI endpoints handling HTTP/2 requests from SDRTrunk
- **Database Layer** (`src/database/`): SQLAlchemy-based operations with SQLite backend
- **Models** (`src/models/`): Pydantic models for API contracts and SQLAlchemy models for persistence
- **Utilities** (`src/utils/`): File handling, multipart parsing, and other utilities
- **Configuration** (`src/config.py`): Comprehensive YAML-based configuration system
- **Middleware**: Security, rate limiting, and validation layers

### Current Workflow

1. SDRTrunk uploads radio calls via POST `/api/call-upload`
2. Multipart form data is parsed containing audio files and metadata
3. Audio files are validated and stored in organized directory structure
4. Metadata is saved to SQLite database with references to audio files
5. Statistics and monitoring endpoints provide system visibility

### Key Strengths

- **HTTP/2 Support**: Critical for SDRTrunk compatibility
- **Async Architecture**: FastAPI with proper async patterns
- **Comprehensive Configuration**: All aspects configurable via YAML
- **Security First**: Rate limiting, API keys, input validation
- **Production Ready**: Logging, monitoring, error handling
- **Clean Separation**: Clear boundaries between layers

## Integration Strategy

### Design Principles

1. **Non-Disruptive**: Transcription runs asynchronously, never blocks uploads
2. **Fault Tolerant**: System continues operating if transcription fails
3. **Scalable**: Support horizontal scaling of transcription workers
4. **Observable**: Comprehensive logging and monitoring for troubleshooting
5. **Configurable**: All aspects tunable via existing configuration system
6. **Secure**: No new attack vectors or security vulnerabilities

### Architecture Decision: Message Queue Pattern

**Selected Approach**: PostgreSQL LISTEN/NOTIFY with Redis fallback

**Rationale**:

- Maintains single-database simplicity for small deployments
- Scales to Redis for high-volume environments
- No additional infrastructure required for basic setups
- Atomic operations with existing database transactions

## Phase 1: Core Infrastructure (Week 1-2)

### 1.1 Database Schema Extensions

Add new tables to support transcription workflow:

```sql
-- Transcription jobs table
CREATE TABLE transcription_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    call_id INTEGER NOT NULL REFERENCES radio_calls(id),
    status VARCHAR(20) NOT NULL DEFAULT 'pending',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    started_at TIMESTAMP NULL,
    completed_at TIMESTAMP NULL,
    error_message TEXT NULL,
    retry_count INTEGER NOT NULL DEFAULT 0,
    priority INTEGER NOT NULL DEFAULT 1,
    worker_id VARCHAR(100) NULL,
    
    -- Transcription parameters
    model_name VARCHAR(50) NOT NULL DEFAULT 'base',
    language VARCHAR(10) NULL,
    compute_type VARCHAR(20) NOT NULL DEFAULT 'float32',
    
    -- Results
    transcript_text TEXT NULL,
    confidence_score REAL NULL,
    processing_time_ms INTEGER NULL,
    segments_json TEXT NULL, -- Detailed timing information
    
    INDEX idx_status (status),
    INDEX idx_created_at (created_at),
    INDEX idx_call_id (call_id),
    INDEX idx_priority_created (priority DESC, created_at ASC)
);

-- Transcription statistics
CREATE TABLE transcription_stats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date DATE NOT NULL,
    total_jobs INTEGER NOT NULL DEFAULT 0,
    completed_jobs INTEGER NOT NULL DEFAULT 0,
    failed_jobs INTEGER NOT NULL DEFAULT 0,
    avg_processing_time_ms REAL NULL,
    total_processing_time_ms INTEGER NOT NULL DEFAULT 0,
    
    UNIQUE(date)
);

-- Worker heartbeat tracking
CREATE TABLE transcription_workers (
    id VARCHAR(100) PRIMARY KEY,
    hostname VARCHAR(255) NOT NULL,
    process_id INTEGER NOT NULL,
    started_at TIMESTAMP NOT NULL,
    last_heartbeat TIMESTAMP NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'active',
    current_job_id INTEGER NULL REFERENCES transcription_jobs(id),
    jobs_completed INTEGER NOT NULL DEFAULT 0,
    
    INDEX idx_last_heartbeat (last_heartbeat),
    INDEX idx_status (status)
);
```

### 1.2 Configuration Extensions

Extend `src/config.py` with transcription settings:

```python
class WhisperXModelConfig(BaseModel):
    """WhisperX model configuration."""
    
    name: str = Field("base", description="Model size (tiny, base, small, medium, large-v2, large-v3)")
    compute_type: str = Field("float32", description="Compute precision (float32, float16, int8)")
    device: str = Field("auto", description="Device (auto, cpu, cuda)")
    batch_size: int = Field(16, description="Batch size for processing")
    
    @field_validator("name")
    @classmethod
    def validate_model_name(cls, v: str) -> str:
        allowed = ["tiny", "base", "small", "medium", "large-v2", "large-v3"]
        if v not in allowed:
            raise ValueError(f"Model must be one of {allowed}")
        return v

class TranscriptionQueueConfig(BaseModel):
    """Message queue configuration for transcription jobs."""
    
    backend: str = Field("sqlite", description="Queue backend (sqlite, postgresql, redis)")
    connection_url: str | None = Field(None, description="Connection URL for external queue")
    max_retries: int = Field(3, description="Maximum retry attempts")
    retry_delay_seconds: int = Field(300, description="Delay between retries")
    job_timeout_minutes: int = Field(30, description="Job processing timeout")
    cleanup_completed_hours: int = Field(24, description="Hours to keep completed jobs")
    
    @field_validator("backend")
    @classmethod
    def validate_backend(cls, v: str) -> str:
        allowed = ["sqlite", "postgresql", "redis"]
        if v not in allowed:
            raise ValueError(f"Backend must be one of {allowed}")
        return v

class TranscriptionWorkerConfig(BaseModel):
    """Worker process configuration."""
    
    enabled: bool = Field(True, description="Enable transcription workers")
    worker_count: int = Field(1, description="Number of worker processes")
    poll_interval_seconds: int = Field(10, description="Job polling interval")
    heartbeat_interval_seconds: int = Field(30, description="Heartbeat interval")
    max_concurrent_jobs: int = Field(1, description="Max concurrent jobs per worker")
    gpu_memory_fraction: float = Field(0.8, description="GPU memory fraction to use")
    
    @field_validator("worker_count")
    @classmethod
    def validate_worker_count(cls, v: int) -> int:
        if v < 0 or v > 16:
            raise ValueError("Worker count must be between 0 and 16")
        return v

class TranscriptionConfig(BaseModel):
    """Main transcription configuration."""
    
    enabled: bool = Field(False, description="Enable automatic transcription")
    
    # Model configuration
    model: WhisperXModelConfig = Field(default_factory=WhisperXModelConfig)
    
    # Queue and worker configuration
    queue: TranscriptionQueueConfig = Field(default_factory=TranscriptionQueueConfig)
    worker: TranscriptionWorkerConfig = Field(default_factory=TranscriptionWorkerConfig)
    
    # Processing options
    auto_detect_language: bool = Field(True, description="Auto-detect audio language")
    min_audio_duration_seconds: float = Field(1.0, description="Skip very short audio")
    max_audio_duration_seconds: float = Field(600.0, description="Skip very long audio")
    
    # Performance settings
    cache_models: bool = Field(True, description="Cache models in memory")
    model_cache_size: int = Field(2, description="Number of models to cache")
    temp_dir: str = Field("data/transcription/temp", description="Temporary processing directory")
```

### 1.3 New Models

Create `src/models/transcription_models.py`:

```python
"""Database and API models for transcription functionality."""

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import Column, DateTime, ForeignKey, Index, Integer, String, Text, Float

from .database_models import Base

class TranscriptionStatus(str, Enum):
    """Transcription job status values."""
    PENDING = "pending"
    PROCESSING = "processing" 
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

class TranscriptionJob(Base):
    """Database model for transcription jobs."""
    
    __tablename__ = "transcription_jobs"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    call_id = Column(Integer, ForeignKey("radio_calls.id"), nullable=False, index=True)
    
    # Job lifecycle
    status = Column(String(20), nullable=False, default="pending", index=True)
    created_at = Column(DateTime, default=lambda: datetime.now(), nullable=False, index=True)
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    
    # Error handling
    error_message = Column(Text, nullable=True)
    retry_count = Column(Integer, nullable=False, default=0)
    priority = Column(Integer, nullable=False, default=1, index=True)
    worker_id = Column(String(100), nullable=True)
    
    # Processing parameters
    model_name = Column(String(50), nullable=False, default="base")
    language = Column(String(10), nullable=True)
    compute_type = Column(String(20), nullable=False, default="float32")
    
    # Results
    transcript_text = Column(Text, nullable=True)
    confidence_score = Column(Float, nullable=True)
    processing_time_ms = Column(Integer, nullable=True)
    segments_json = Column(Text, nullable=True)  # JSON string of detailed segments
    
    # Indexes for performance
    __table_args__ = (
        Index("idx_priority_created", "priority", "created_at"),
        Index("idx_status_created", "status", "created_at"),
        Index("idx_worker_status", "worker_id", "status"),
    )

class TranscriptionJobRequest(BaseModel):
    """API model for requesting transcription."""
    
    call_id: int = Field(..., description="Radio call ID to transcribe")
    priority: int = Field(1, description="Job priority (1=low, 5=high)")
    model_name: str = Field("base", description="WhisperX model to use")
    language: str | None = Field(None, description="Expected language code")

class TranscriptionJobResponse(BaseModel):
    """API response for transcription job."""
    
    job_id: int = Field(..., description="Transcription job ID")
    call_id: int = Field(..., description="Associated call ID")
    status: TranscriptionStatus = Field(..., description="Current job status")
    created_at: datetime = Field(..., description="Job creation time")
    transcript_text: str | None = Field(None, description="Transcribed text")
    confidence_score: float | None = Field(None, description="Average confidence score")
    error_message: str | None = Field(None, description="Error message if failed")

class TranscriptionQueueStatus(BaseModel):
    """API response for queue status."""
    
    pending_jobs: int = Field(..., description="Jobs waiting for processing")
    processing_jobs: int = Field(..., description="Jobs currently processing")
    failed_jobs: int = Field(..., description="Jobs that failed")
    completed_today: int = Field(..., description="Jobs completed today")
    active_workers: int = Field(..., description="Number of active workers")
    average_processing_time_ms: float | None = Field(None, description="Average processing time")
```

## Phase 2: Basic Transcription (Week 3-4)

### 2.1 Queue Management System

Create `src/transcription/queue.py`:

```python
"""Transcription job queue management."""

import json
import logging
from abc import ABC, abstractmethod
from datetime import datetime, timedelta
from typing import Any, Optional

from sqlalchemy import and_, desc, func, or_
from sqlalchemy.orm import Session

from ..database.connection import DatabaseManager
from ..models.transcription_models import TranscriptionJob, TranscriptionStatus

logger = logging.getLogger(__name__)

class TranscriptionQueue(ABC):
    """Abstract base class for transcription queues."""
    
    @abstractmethod
    async def enqueue_job(self, call_id: int, **kwargs) -> int:
        """Add a transcription job to the queue."""
        pass
    
    @abstractmethod
    async def dequeue_job(self, worker_id: str) -> Optional[TranscriptionJob]:
        """Get the next job for processing."""
        pass
    
    @abstractmethod
    async def complete_job(self, job_id: int, transcript: str, confidence: float, 
                          segments: dict, processing_time_ms: int) -> None:
        """Mark job as completed with results."""
        pass
    
    @abstractmethod
    async def fail_job(self, job_id: int, error_message: str) -> bool:
        """Mark job as failed, return True if should retry."""
        pass
    
    @abstractmethod
    async def get_queue_status(self) -> dict[str, Any]:
        """Get current queue statistics."""
        pass

class SQLiteTranscriptionQueue(TranscriptionQueue):
    """SQLite-based transcription queue using polling."""
    
    def __init__(self, db_manager: DatabaseManager, max_retries: int = 3):
        self.db_manager = db_manager
        self.max_retries = max_retries
    
    async def enqueue_job(self, call_id: int, priority: int = 1, 
                         model_name: str = "base", language: str = None) -> int:
        """Add transcription job to queue."""
        with self.db_manager.get_session() as session:
            # Check if job already exists for this call
            existing = session.query(TranscriptionJob).filter(
                and_(
                    TranscriptionJob.call_id == call_id,
                    TranscriptionJob.status.in_([
                        TranscriptionStatus.PENDING,
                        TranscriptionStatus.PROCESSING
                    ])
                )
            ).first()
            
            if existing:
                logger.warning(f"Transcription job already exists for call {call_id}")
                return existing.id
            
            # Create new job
            job = TranscriptionJob(
                call_id=call_id,
                priority=priority,
                model_name=model_name,
                language=language,
                status=TranscriptionStatus.PENDING
            )
            
            session.add(job)
            session.commit()
            
            logger.info(f"Enqueued transcription job {job.id} for call {call_id}")
            return job.id
    
    async def dequeue_job(self, worker_id: str) -> Optional[TranscriptionJob]:
        """Get next pending job ordered by priority and age."""
        with self.db_manager.get_session() as session:
            # Find oldest high-priority pending job
            job = session.query(TranscriptionJob).filter(
                TranscriptionJob.status == TranscriptionStatus.PENDING
            ).order_by(
                desc(TranscriptionJob.priority),
                TranscriptionJob.created_at
            ).first()
            
            if not job:
                return None
            
            # Claim the job atomically
            job.status = TranscriptionStatus.PROCESSING
            job.started_at = datetime.now()
            job.worker_id = worker_id
            
            session.commit()
            
            logger.info(f"Dequeued job {job.id} for worker {worker_id}")
            return job
    
    async def complete_job(self, job_id: int, transcript: str, confidence: float,
                          segments: dict, processing_time_ms: int) -> None:
        """Mark job as completed."""
        with self.db_manager.get_session() as session:
            job = session.query(TranscriptionJob).filter(
                TranscriptionJob.id == job_id
            ).first()
            
            if not job:
                logger.error(f"Job {job_id} not found for completion")
                return
            
            job.status = TranscriptionStatus.COMPLETED
            job.completed_at = datetime.now()
            job.transcript_text = transcript
            job.confidence_score = confidence
            job.segments_json = json.dumps(segments)
            job.processing_time_ms = processing_time_ms
            
            session.commit()
            
            logger.info(f"Completed transcription job {job_id}")
    
    async def fail_job(self, job_id: int, error_message: str) -> bool:
        """Mark job as failed, increment retry count."""
        with self.db_manager.get_session() as session:
            job = session.query(TranscriptionJob).filter(
                TranscriptionJob.id == job_id
            ).first()
            
            if not job:
                logger.error(f"Job {job_id} not found for failure")
                return False
            
            job.retry_count += 1
            job.error_message = error_message
            job.worker_id = None
            
            # Retry if under limit, otherwise mark as failed
            if job.retry_count < self.max_retries:
                job.status = TranscriptionStatus.PENDING
                job.started_at = None
                logger.warning(f"Job {job_id} failed, retrying ({job.retry_count}/{self.max_retries})")
                session.commit()
                return True
            else:
                job.status = TranscriptionStatus.FAILED
                logger.error(f"Job {job_id} failed permanently after {job.retry_count} attempts")
                session.commit()
                return False
    
    async def get_queue_status(self) -> dict[str, Any]:
        """Get queue statistics."""
        with self.db_manager.get_session() as session:
            status = {}
            
            # Count jobs by status
            status_counts = session.query(
                TranscriptionJob.status,
                func.count(TranscriptionJob.id).label('count')
            ).group_by(TranscriptionJob.status).all()
            
            status['pending_jobs'] = 0
            status['processing_jobs'] = 0
            status['failed_jobs'] = 0
            status['completed_jobs'] = 0
            
            for status_name, count in status_counts:
                if status_name == TranscriptionStatus.PENDING:
                    status['pending_jobs'] = count
                elif status_name == TranscriptionStatus.PROCESSING:
                    status['processing_jobs'] = count
                elif status_name == TranscriptionStatus.FAILED:
                    status['failed_jobs'] = count
                elif status_name == TranscriptionStatus.COMPLETED:
                    status['completed_jobs'] = count
            
            # Jobs completed today
            today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
            status['completed_today'] = session.query(TranscriptionJob).filter(
                and_(
                    TranscriptionJob.status == TranscriptionStatus.COMPLETED,
                    TranscriptionJob.completed_at >= today
                )
            ).count()
            
            # Average processing time
            avg_time = session.query(
                func.avg(TranscriptionJob.processing_time_ms)
            ).filter(
                and_(
                    TranscriptionJob.status == TranscriptionStatus.COMPLETED,
                    TranscriptionJob.processing_time_ms.isnot(None)
                )
            ).scalar()
            
            status['average_processing_time_ms'] = float(avg_time) if avg_time else None
            
            return status
```

### 2.2 WhisperX Integration Service

Create `src/transcription/whisperx_service.py`:

```python
"""WhisperX transcription service integration."""

import logging
import tempfile
from pathlib import Path
from typing import Dict, Tuple, Optional, Any
import time

try:
    import whisperx
    import torch
    WHISPERX_AVAILABLE = True
except ImportError:
    WHISPERX_AVAILABLE = False
    whisperx = None
    torch = None

from ..config import TranscriptionConfig

logger = logging.getLogger(__name__)

class WhisperXService:
    """Service for handling WhisperX transcription operations."""
    
    def __init__(self, config: TranscriptionConfig):
        """Initialize WhisperX service.
        
        Args:
            config: Transcription configuration
            
        Raises:
            ImportError: If WhisperX dependencies not available
            RuntimeError: If GPU requested but not available
        """
        if not WHISPERX_AVAILABLE:
            raise ImportError(
                "WhisperX not available. Install with: pip install whisperx"
            )
        
        self.config = config
        self.models = {}  # Model cache
        self.device = self._determine_device()
        self.compute_type = config.model.compute_type
        
        logger.info(f"WhisperX service initialized - Device: {self.device}")
    
    def _determine_device(self) -> str:
        """Determine the best device for processing."""
        device_config = self.config.model.device.lower()
        
        if device_config == "auto":
            if torch and torch.cuda.is_available():
                return "cuda"
            else:
                return "cpu"
        elif device_config == "cuda":
            if torch and torch.cuda.is_available():
                return "cuda"
            else:
                logger.warning("CUDA requested but not available, falling back to CPU")
                return "cpu"
        else:
            return "cpu"
    
    def _load_model(self, model_name: str) -> Any:
        """Load or retrieve cached WhisperX model."""
        cache_key = f"{model_name}_{self.device}_{self.compute_type}"
        
        if cache_key in self.models:
            logger.debug(f"Using cached model: {cache_key}")
            return self.models[cache_key]
        
        logger.info(f"Loading WhisperX model: {model_name} on {self.device}")
        start_time = time.time()
        
        try:
            model = whisperx.load_model(
                model_name, 
                device=self.device,
                compute_type=self.compute_type
            )
            
            load_time = time.time() - start_time
            logger.info(f"Model {model_name} loaded in {load_time:.2f}s")
            
            # Cache management
            if self.config.cache_models:
                if len(self.models) >= self.config.model_cache_size:
                    # Remove oldest model
                    oldest_key = next(iter(self.models))
                    del self.models[oldest_key]
                    logger.debug(f"Evicted cached model: {oldest_key}")
                
                self.models[cache_key] = model
            
            return model
            
        except Exception as e:
            logger.error(f"Failed to load model {model_name}: {e}")
            raise
    
    async def transcribe_audio_file(
        self, 
        audio_path: Path,
        model_name: str = "base",
        language: Optional[str] = None
    ) -> Tuple[str, float, Dict[str, Any]]:
        """Transcribe audio file using WhisperX.
        
        Args:
            audio_path: Path to audio file
            model_name: WhisperX model name
            language: Language code (None for auto-detect)
            
        Returns:
            Tuple of (transcript_text, confidence_score, segments_dict)
            
        Raises:
            FileNotFoundError: If audio file doesn't exist
            Exception: If transcription fails
        """
        if not audio_path.exists():
            raise FileNotFoundError(f"Audio file not found: {audio_path}")
        
        logger.info(f"Starting transcription: {audio_path.name} with model {model_name}")
        start_time = time.time()
        
        try:
            # Load model
            model = self._load_model(model_name)
            
            # Load audio
            audio = whisperx.load_audio(str(audio_path))
            
            # Perform transcription
            result = model.transcribe(
                audio, 
                batch_size=self.config.model.batch_size,
                language=language if language else None
            )
            
            # Language detection if not specified
            detected_language = result.get("language", "unknown")
            if not language and detected_language:
                logger.info(f"Detected language: {detected_language}")
            
            # Load alignment model for better timestamps
            try:
                model_a, metadata = whisperx.load_align_model(
                    language_code=detected_language, 
                    device=self.device
                )
                result = whisperx.align(
                    result["segments"], 
                    model_a, 
                    metadata, 
                    audio, 
                    self.device,
                    return_char_alignments=False
                )
            except Exception as align_error:
                logger.warning(f"Alignment failed, using raw segments: {align_error}")
                # Continue with unaligned segments
            
            # Extract transcript text
            transcript_text = " ".join([segment["text"].strip() for segment in result["segments"]])
            
            # Calculate average confidence
            segments_with_confidence = [s for s in result["segments"] if "confidence" in s]
            if segments_with_confidence:
                avg_confidence = sum(s["confidence"] for s in segments_with_confidence) / len(segments_with_confidence)
            else:
                avg_confidence = 0.0
            
            # Prepare segments data for storage
            segments_data = {
                "language": detected_language,
                "segments": result["segments"],
                "model_used": model_name,
                "alignment_used": "model_a" in locals()
            }
            
            processing_time = time.time() - start_time
            
            logger.info(
                f"Transcription completed in {processing_time:.2f}s - "
                f"Length: {len(transcript_text)} chars, Confidence: {avg_confidence:.3f}"
            )
            
            return transcript_text.strip(), avg_confidence, segments_data
            
        except Exception as e:
            processing_time = time.time() - start_time
            logger.error(f"Transcription failed after {processing_time:.2f}s: {e}")
            raise
    
    def get_available_models(self) -> list[str]:
        """Get list of available WhisperX models."""
        return ["tiny", "base", "small", "medium", "large-v2", "large-v3"]
    
    def get_system_info(self) -> Dict[str, Any]:
        """Get system information for diagnostics."""
        info = {
            "whisperx_available": WHISPERX_AVAILABLE,
            "device": self.device,
            "compute_type": self.compute_type,
            "cached_models": list(self.models.keys()),
            "torch_version": torch.__version__ if torch else None,
        }
        
        if torch and torch.cuda.is_available():
            info["cuda_device_count"] = torch.cuda.device_count()
            info["cuda_memory_allocated"] = torch.cuda.memory_allocated()
            info["cuda_memory_cached"] = torch.cuda.memory_reserved()
        
        return info
    
    def cleanup(self) -> None:
        """Clean up resources."""
        logger.info("Cleaning up WhisperX service")
        self.models.clear()
        
        if torch and torch.cuda.is_available():
            torch.cuda.empty_cache()
```

### 2.3 Worker Process

Create `src/transcription/worker.py`:

```python
"""Transcription worker process."""

import asyncio
import logging
import os
import signal
import sys
import time
from pathlib import Path
from typing import Optional

from ..config import Config
from ..database.connection import DatabaseManager
from ..database.operations import DatabaseOperations
from .queue import SQLiteTranscriptionQueue
from .whisperx_service import WhisperXService

logger = logging.getLogger(__name__)

class TranscriptionWorker:
    """Worker process for handling transcription jobs."""
    
    def __init__(self, config: Config, worker_id: Optional[str] = None):
        """Initialize transcription worker.
        
        Args:
            config: Application configuration
            worker_id: Unique worker identifier
        """
        self.config = config
        self.worker_id = worker_id or f"worker_{os.getpid()}_{int(time.time())}"
        self.running = False
        
        # Initialize components
        self.db_manager = DatabaseManager(config.database.path)
        self.db_ops = DatabaseOperations(self.db_manager)
        self.queue = SQLiteTranscriptionQueue(
            self.db_manager, 
            config.transcription.queue.max_retries
        )
        
        # Initialize WhisperX service
        try:
            self.whisperx = WhisperXService(config.transcription)
            logger.info(f"WhisperX service initialized for worker {self.worker_id}")
        except Exception as e:
            logger.error(f"Failed to initialize WhisperX service: {e}")
            sys.exit(1)
        
        # Setup signal handlers
        signal.signal(signal.SIGTERM, self._signal_handler)
        signal.signal(signal.SIGINT, self._signal_handler)
    
    def _signal_handler(self, signum, frame):
        """Handle shutdown signals gracefully."""
        logger.info(f"Worker {self.worker_id} received signal {signum}, shutting down...")
        self.running = False
    
    async def start(self) -> None:
        """Start the worker process."""
        logger.info(f"Starting transcription worker {self.worker_id}")
        self.running = True
        
        # Register worker
        await self._register_worker()
        
        try:
            while self.running:
                try:
                    # Process one job
                    await self._process_next_job()
                    
                    # Update heartbeat
                    await self._update_heartbeat()
                    
                    # Wait before next poll
                    await asyncio.sleep(self.config.transcription.worker.poll_interval_seconds)
                    
                except Exception as e:
                    logger.error(f"Error in worker main loop: {e}")
                    await asyncio.sleep(5)  # Brief pause before retry
                    
        finally:
            await self._cleanup()
    
    async def _register_worker(self) -> None:
        """Register worker in database."""
        # Implementation would add worker to transcription_workers table
        logger.info(f"Registered worker {self.worker_id}")
    
    async def _update_heartbeat(self) -> None:
        """Update worker heartbeat timestamp."""
        # Implementation would update last_heartbeat in transcription_workers table
        pass
    
    async def _process_next_job(self) -> None:
        """Process the next available transcription job."""
        # Get next job from queue
        job = await self.queue.dequeue_job(self.worker_id)
        
        if not job:
            return  # No jobs available
        
        logger.info(f"Processing transcription job {job.id} for call {job.call_id}")
        
        try:
            # Get call information
            call_data = self.db_ops.get_call_by_id(job.call_id)
            if not call_data:
                raise Exception(f"Call {job.call_id} not found")
            
            audio_path = Path(call_data["audio_file_path"])
            if not audio_path.exists():
                raise Exception(f"Audio file not found: {audio_path}")
            
            # Check file duration constraints
            # (Implementation would check file duration here)
            
            # Perform transcription
            transcript, confidence, segments = await self.whisperx.transcribe_audio_file(
                audio_path,
                job.model_name,
                job.language
            )
            
            # Calculate processing time
            processing_time_ms = int((time.time() - job.started_at.timestamp()) * 1000)
            
            # Mark job as completed
            await self.queue.complete_job(
                job.id, 
                transcript, 
                confidence, 
                segments,
                processing_time_ms
            )
            
        except Exception as e:
            logger.error(f"Job {job.id} failed: {e}")
            await self.queue.fail_job(job.id, str(e))
    
    async def _cleanup(self) -> None:
        """Cleanup worker resources."""
        logger.info(f"Cleaning up worker {self.worker_id}")
        
        # Cleanup WhisperX service
        self.whisperx.cleanup()
        
        # Close database connections
        self.db_manager.close()

async def main():
    """Main entry point for worker process."""
    # Load configuration
    config = Config.load_from_file("config.yaml")
    
    # Setup logging
    from ..config import setup_logging
    setup_logging(config.logging)
    
    # Create and start worker
    worker = TranscriptionWorker(config)
    await worker.start()

if __name__ == "__main__":
    asyncio.run(main())
```

## Phase 3: API Integration (Week 5)

### 3.1 Transcription API Endpoints

Create `src/api/transcription.py`:

```python
"""Transcription API endpoints."""

import logging
from typing import List

from fastapi import APIRouter, HTTPException, Request, Query, Depends
from fastapi.responses import JSONResponse

from ..database.operations import DatabaseOperations
from ..middleware.rate_limiter import get_limiter
from ..models.transcription_models import (
    TranscriptionJobRequest,
    TranscriptionJobResponse,
    TranscriptionQueueStatus,
    TranscriptionStatus
)
from ..transcription.queue import SQLiteTranscriptionQueue

logger = logging.getLogger(__name__)
router = APIRouter(tags=["transcription"])
limiter = get_limiter()

@router.post(
    "/api/transcription/jobs",
    response_model=TranscriptionJobResponse,
    summary="Request Transcription",
    description="Request transcription for a specific radio call",
)
@limiter.limit("10 per minute")
async def create_transcription_job(
    request: Request,
    job_request: TranscriptionJobRequest
) -> TranscriptionJobResponse:
    """Create a new transcription job."""
    config = request.app.state.config
    
    if not config.transcription.enabled:
        raise HTTPException(
            status_code=503, 
            detail="Transcription service is not enabled"
        )
    
    db_ops: DatabaseOperations = request.app.state.db_ops
    queue: SQLiteTranscriptionQueue = request.app.state.transcription_queue
    
    try:
        # Verify call exists
        call_data = db_ops.get_call_by_id(job_request.call_id)
        if not call_data:
            raise HTTPException(
                status_code=404, 
                detail=f"Call {job_request.call_id} not found"
            )
        
        # Enqueue job
        job_id = await queue.enqueue_job(
            call_id=job_request.call_id,
            priority=job_request.priority,
            model_name=job_request.model_name,
            language=job_request.language
        )
        
        # Return job info
        return TranscriptionJobResponse(
            job_id=job_id,
            call_id=job_request.call_id,
            status=TranscriptionStatus.PENDING,
            created_at=datetime.now(),
        )
        
    except Exception as e:
        logger.error(f"Error creating transcription job: {e}")
        raise HTTPException(
            status_code=500, 
            detail="Failed to create transcription job"
        )

@router.get(
    "/api/transcription/jobs/{job_id}",
    response_model=TranscriptionJobResponse,
    summary="Get Transcription Job",
    description="Get status and results of a transcription job",
)
@limiter.limit("30 per minute")
async def get_transcription_job(
    request: Request,
    job_id: int
) -> TranscriptionJobResponse:
    """Get transcription job details."""
    # Implementation would query transcription_jobs table
    pass

@router.get(
    "/api/transcription/queue/status",
    response_model=TranscriptionQueueStatus,
    summary="Queue Status",
    description="Get current transcription queue status",
)
@limiter.limit("20 per minute")
async def get_queue_status(request: Request) -> TranscriptionQueueStatus:
    """Get transcription queue status."""
    queue: SQLiteTranscriptionQueue = request.app.state.transcription_queue
    
    try:
        status = await queue.get_queue_status()
        
        return TranscriptionQueueStatus(
            pending_jobs=status.get("pending_jobs", 0),
            processing_jobs=status.get("processing_jobs", 0),
            failed_jobs=status.get("failed_jobs", 0),
            completed_today=status.get("completed_today", 0),
            active_workers=status.get("active_workers", 0),
            average_processing_time_ms=status.get("average_processing_time_ms")
        )
        
    except Exception as e:
        logger.error(f"Error getting queue status: {e}")
        raise HTTPException(status_code=500, detail="Failed to get queue status")

@router.get(
    "/api/calls/{call_id}/transcription",
    summary="Get Call Transcription",
    description="Get transcription for a specific call",
)
@limiter.limit("60 per minute")
async def get_call_transcription(
    request: Request,
    call_id: int
) -> JSONResponse:
    """Get transcription text for a specific call."""
    # Implementation would query for completed transcription job
    pass
```

### 3.2 Auto-Queue Integration

Modify `src/api/rdioscanner.py` to automatically enqueue transcription jobs:

```python
# Add after successful call storage (around line 448)

# Auto-enqueue transcription if enabled
if (config.transcription.enabled and 
    config.transcription.worker.enabled and 
    stored_path and call_id):
    
    try:
        transcription_queue = request.app.state.transcription_queue
        await transcription_queue.enqueue_job(
            call_id=call_id,
            priority=1,  # Default priority
            model_name=config.transcription.model.name
        )
        logger.debug(f"Auto-enqueued transcription for call {call_id}")
    except Exception as e:
        logger.warning(f"Failed to auto-enqueue transcription for call {call_id}: {e}")
        # Don't fail the upload for transcription errors
```

## Phase 4: Production Optimization (Week 6-7)

### 4.1 Performance Monitoring

Create `src/transcription/monitoring.py`:

```python
"""Transcription system monitoring and metrics."""

import logging
import time
from dataclasses import dataclass
from typing import Dict, List
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

@dataclass
class TranscriptionMetrics:
    """Transcription performance metrics."""
    
    # Throughput metrics
    jobs_per_hour: float
    average_processing_time_seconds: float
    queue_wait_time_seconds: float
    
    # Quality metrics
    average_confidence_score: float
    success_rate: float
    
    # Resource metrics
    cpu_usage_percent: float
    memory_usage_mb: float
    gpu_memory_usage_mb: float
    
    # Queue metrics
    pending_jobs: int
    processing_jobs: int
    failed_jobs_last_24h: int

class TranscriptionMonitor:
    """Monitor transcription system performance."""
    
    def __init__(self, db_manager):
        self.db_manager = db_manager
        self.metrics_history = []
    
    def collect_metrics(self) -> TranscriptionMetrics:
        """Collect current system metrics."""
        with self.db_manager.get_session() as session:
            # Query performance data
            # Implementation would collect comprehensive metrics
            pass
    
    def log_performance_alert(self, metric: str, value: float, threshold: float):
        """Log performance alerts when thresholds are exceeded."""
        if value > threshold:
            logger.warning(
                f"Performance alert: {metric} = {value:.2f} exceeds threshold {threshold:.2f}"
            )
    
    def get_health_status(self) -> Dict[str, str]:
        """Get overall transcription system health."""
        try:
            metrics = self.collect_metrics()
            
            status = {"overall": "healthy"}
            
            # Check various health indicators
            if metrics.success_rate < 0.9:
                status["transcription"] = "degraded"
            if metrics.queue_wait_time_seconds > 300:  # 5 minutes
                status["queue"] = "slow"
            if metrics.pending_jobs > 100:
                status["backlog"] = "high"
            
            return status
            
        except Exception as e:
            logger.error(f"Health check failed: {e}")
            return {"overall": "unhealthy", "error": str(e)}
```

### 4.2 Batch Processing Optimization

Create `src/transcription/batch_processor.py`:

```python
"""Batch processing for improved transcription efficiency."""

import asyncio
import logging
from typing import List, Dict, Any
from pathlib import Path

from .whisperx_service import WhisperXService
from ..models.transcription_models import TranscriptionJob

logger = logging.getLogger(__name__)

class BatchTranscriptionProcessor:
    """Process multiple transcription jobs in batches for efficiency."""
    
    def __init__(self, whisperx_service: WhisperXService, batch_size: int = 4):
        self.whisperx = whisperx_service
        self.batch_size = batch_size
    
    async def process_batch(self, jobs: List[TranscriptionJob]) -> List[Dict[str, Any]]:
        """Process a batch of transcription jobs."""
        if len(jobs) > self.batch_size:
            logger.warning(f"Batch size {len(jobs)} exceeds maximum {self.batch_size}")
            jobs = jobs[:self.batch_size]
        
        logger.info(f"Processing batch of {len(jobs)} transcription jobs")
        
        # Group jobs by model for efficiency
        jobs_by_model = {}
        for job in jobs:
            if job.model_name not in jobs_by_model:
                jobs_by_model[job.model_name] = []
            jobs_by_model[job.model_name].append(job)
        
        results = []
        
        # Process each model group
        for model_name, model_jobs in jobs_by_model.items():
            logger.debug(f"Processing {len(model_jobs)} jobs with model {model_name}")
            
            for job in model_jobs:
                try:
                    # Get audio path from call data
                    # (Implementation would fetch call data and process)
                    
                    result = {
                        "job_id": job.id,
                        "success": True,
                        "transcript": "placeholder",
                        "confidence": 0.95,
                        "segments": {},
                        "processing_time_ms": 1000
                    }
                    results.append(result)
                    
                except Exception as e:
                    logger.error(f"Batch job {job.id} failed: {e}")
                    results.append({
                        "job_id": job.id,
                        "success": False,
                        "error": str(e)
                    })
        
        return results
```

### 4.3 Configuration Validation

Add validation to `src/config.py`:

```python
@field_validator("transcription")
@classmethod
def validate_transcription_config(cls, v: TranscriptionConfig) -> TranscriptionConfig:
    """Validate transcription configuration."""
    if v.enabled and not WHISPERX_AVAILABLE:
        logger.warning("Transcription enabled but WhisperX not available")
    
    if v.worker.enabled and v.worker.worker_count <= 0:
        raise ValueError("Worker count must be positive when workers enabled")
    
    if v.model.compute_type not in ["float32", "float16", "int8"]:
        raise ValueError("Invalid compute_type for transcription")
    
    return v
```

## Implementation Phases Summary

### Phase 1: Core Infrastructure (Weeks 1-2)

- ✅ Database schema extensions
- ✅ Configuration system updates  
- ✅ New Pydantic models for transcription
- ✅ Basic queue management system

### Phase 2: Basic Transcription (Weeks 3-4)

- ✅ WhisperX service integration
- ✅ SQLite-based job queue
- ✅ Worker process implementation
- ✅ Basic error handling and retries

### Phase 3: API Integration (Week 5)

- ✅ REST API endpoints for transcription management
- ✅ Auto-queuing on radio call upload
- ✅ Integration with existing FastAPI application
- ✅ Rate limiting and security

### Phase 4: Production Optimization (Weeks 6-7)

- ✅ Performance monitoring and alerting
- ✅ Batch processing for efficiency
- ✅ Resource management and cleanup
- ✅ Comprehensive logging and debugging

## Operational Considerations

### System Requirements

**Minimum System Requirements:**

- Python 3.11+
- 4GB RAM (8GB recommended)
- 2 CPU cores (4+ recommended)
- 10GB free disk space for models and temp files

**GPU Acceleration (Optional):**

- NVIDIA GPU with CUDA support
- 6GB+ GPU memory for large models
- CUDA 11.8+ and compatible drivers

### Model Management

**Model Storage:**

- Models cached in memory (configurable limit)
- Automatic model downloading on first use  
- Model file storage in `~/.cache/whisperx/`
- Estimated storage: 150MB (tiny) to 2GB (large-v3)

### Docker Considerations

```dockerfile
# Add to existing Dockerfile
FROM python:3.13-slim

# Install system dependencies for WhisperX
RUN apt-get update && apt-get install -y \
    ffmpeg \
    libffi-dev \
    && rm -rf /var/lib/apt/lists/*

# Install PyTorch (CPU or CUDA version)
RUN pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cpu

# Install WhisperX
RUN pip install whisperx

# Copy application code
COPY . /app
WORKDIR /app

# Install application dependencies
RUN uv sync

EXPOSE 8080
CMD ["uv", "run", "python", "cli.py", "serve"]
```

### Scaling Strategies

**Horizontal Scaling:**

- Multiple worker processes on single machine
- Distributed workers across multiple machines
- Load balancing with Redis or PostgreSQL queue backend

**Vertical Scaling:**

- Larger GPU memory for bigger models
- More CPU cores for concurrent processing
- NVMe storage for faster model loading

### Monitoring and Alerting

**Key Metrics to Monitor:**

- Transcription success rate (target: >95%)
- Average processing time per minute of audio
- Queue depth and wait times  
- Worker process health and uptime
- GPU/CPU utilization
- Memory usage patterns

**Alert Conditions:**

- Queue depth > 50 jobs (may indicate worker issues)
- Success rate < 90% (model or infrastructure problems)
- Average processing time > 5x audio duration
- Any worker offline for > 5 minutes
- Disk space < 1GB (for temp files and models)

### Security Considerations

**Access Control:**

- Same API key system as existing endpoints
- Rate limiting on transcription requests
- No sensitive data in transcription logs

**Data Privacy:**

- Audio files processed locally (no external API calls)
- Transcription text stored with same security as call metadata
- Optional encryption at rest for sensitive deployments

**Resource Protection:**

- Memory limits prevent OOM conditions
- CPU/GPU time limits prevent runaway processes  
- Temporary file cleanup prevents disk exhaustion

## Configuration Example

```yaml
# config.yaml - Transcription section
transcription:
  enabled: true
  
  model:
    name: "base"                    # tiny, base, small, medium, large-v2, large-v3
    compute_type: "float32"         # float32, float16, int8
    device: "auto"                  # auto, cpu, cuda
    batch_size: 16
  
  queue:
    backend: "sqlite"               # sqlite, postgresql, redis
    max_retries: 3
    retry_delay_seconds: 300
    job_timeout_minutes: 30
    cleanup_completed_hours: 24
  
  worker:
    enabled: true
    worker_count: 1                 # Number of worker processes
    poll_interval_seconds: 10
    heartbeat_interval_seconds: 30
    max_concurrent_jobs: 1
    gpu_memory_fraction: 0.8
  
  # Processing options  
  auto_detect_language: true
  min_audio_duration_seconds: 1.0
  max_audio_duration_seconds: 600.0
  
  # Performance settings
  cache_models: true
  model_cache_size: 2
  temp_dir: "data/transcription/temp"
```

## Migration Path

### For Existing Deployments

1. **Update Dependencies:**

   ```bash
   # Add WhisperX to requirements
   uv add whisperx torch torchvision torchaudio
   ```

2. **Database Migration:**

   ```bash
   # Run database migrations
   uv run python cli.py migrate --add-transcription-tables
   ```

3. **Configuration Update:**

   ```bash
   # Add transcription section to config.yaml
   uv run python cli.py init --add-transcription
   ```

4. **Gradual Rollout:**
   - Enable transcription with `enabled: false` initially
   - Test with manual transcription requests
   - Enable auto-queuing for new calls
   - Backfill historical calls if desired

### Testing Strategy

1. **Unit Tests:**
   - WhisperX service integration
   - Queue management operations
   - API endpoint functionality

2. **Integration Tests:**
   - End-to-end transcription workflow
   - Worker process lifecycle
   - Error handling and recovery

3. **Performance Tests:**
   - Load testing with concurrent jobs
   - Memory usage under sustained load
   - GPU utilization optimization

4. **Production Monitoring:**
   - Gradual rollout with monitoring
   - A/B testing for quality validation
   - Performance baseline establishment

## Success Criteria

**Functional Requirements:**

- ✅ Automatic transcription of all uploaded audio files
- ✅ REST API for manual transcription requests
- ✅ High availability with graceful degradation
- ✅ Multiple model support (tiny through large-v3)
- ✅ Language auto-detection and manual override

**Performance Requirements:**

- ✅ <5x real-time processing (5min audio processed in <25min)
- ✅ >95% transcription success rate
- ✅ Queue depth <50 jobs under normal load
- ✅ Worker recovery time <60 seconds after failure

**Operational Requirements:**

- ✅ Zero-downtime deployment capability
- ✅ Comprehensive monitoring and alerting
- ✅ Automatic cleanup of temporary files
- ✅ Configuration hot-reload for tuning

This comprehensive plan provides a robust foundation for integrating WhisperX transcription while maintaining the high standards of the existing sdrtrunk-rdio-api codebase. The phased approach allows for iterative development and testing, ensuring a reliable production deployment.
