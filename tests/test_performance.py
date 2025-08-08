"""Performance benchmarking tests for RdioCallsAPI."""

import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

# Skip performance tests by default (run with: pytest -m performance)
pytestmark = pytest.mark.performance


class TestUploadPerformance:
    """Benchmark upload endpoint performance."""

    @pytest.mark.benchmark
    def test_single_upload_performance(self, test_client, temp_audio_file, benchmark):
        """Benchmark single file upload performance."""

        def upload():
            test_data = {
                "key": "test-api-key",
                "system": "1",
                "dateTime": str(int(datetime.now().timestamp())),
                "talkgroup": "1234",
            }
            files = {"audio": ("test.mp3", temp_audio_file.read_bytes(), "audio/mpeg")}
            response = test_client.post("/api/call-upload", data=test_data, files=files)
            assert response.status_code == 200
            return response

        # Run benchmark
        result = benchmark(upload)
        assert result.status_code == 200

    @pytest.mark.benchmark
    def test_concurrent_uploads_performance(self, test_app, temp_audio_file):
        """Benchmark concurrent upload performance."""
        base_url = "http://testserver"
        num_uploads = 50
        num_workers = 10

        def upload_call(index: int):
            """Upload a single call."""
            with TestClient(app=test_app, base_url=base_url) as client:
                test_data = {
                    "key": "test-api-key",
                    "system": str(index % 5 + 1),
                    "dateTime": str(int(datetime.now().timestamp())),
                    "talkgroup": str(1000 + index),
                }
                files = {
                    "audio": ("test.mp3", temp_audio_file.read_bytes(), "audio/mpeg")
                }
                response = client.post("/api/call-upload", data=test_data, files=files)
                return response.status_code == 200

        # Measure concurrent upload time
        start_time = time.time()

        with ThreadPoolExecutor(max_workers=num_workers) as executor:
            results = list(executor.map(upload_call, range(num_uploads)))

        elapsed_time = time.time() - start_time

        # Calculate metrics
        successful_uploads = sum(results)
        uploads_per_second = successful_uploads / elapsed_time

        print("\nConcurrent Upload Performance:")
        print(f"  Total uploads: {num_uploads}")
        print(f"  Successful: {successful_uploads}")
        print(f"  Time: {elapsed_time:.2f}s")
        print(f"  Throughput: {uploads_per_second:.2f} uploads/sec")

        # Assert minimum performance threshold
        assert uploads_per_second > 5  # At least 5 uploads per second
        assert successful_uploads == num_uploads  # All uploads should succeed


class TestDatabasePerformance:
    """Benchmark database operation performance."""

    @pytest.mark.benchmark
    def test_database_insert_performance(self, db_ops, benchmark):
        """Benchmark database insert performance."""
        from src.models.api_models import RdioScannerUpload

        def insert_call():
            upload_data = RdioScannerUpload(
                key="test",
                system="1",
                dateTime=int(datetime.now().timestamp()),
                talkgroup=1234,
                frequency=853237500,
            )
            call_id = db_ops.save_call(
                upload_data,
                client_ip="127.0.0.1",
                stored_path="/test/path.mp3",
                api_key_id="test-key",
            )
            return call_id

        # Run benchmark
        result = benchmark(insert_call)
        assert result is not None

    @pytest.mark.benchmark
    def test_database_query_performance(self, db_ops, benchmark):
        """Benchmark database query performance."""
        # Insert test data first
        from src.models.api_models import RdioScannerUpload

        for i in range(100):
            upload_data = RdioScannerUpload(
                key="test",
                system=str(i % 5 + 1),
                dateTime=int(datetime.now().timestamp()),
                talkgroup=1000 + i,
            )
            db_ops.save_call(
                upload_data,
                client_ip="127.0.0.1",
                stored_path=f"/test/path_{i}.mp3",
                api_key_id="test-key",
            )

        def query_stats():
            stats = db_ops.get_statistics()
            return stats

        # Run benchmark
        result = benchmark(query_stats)
        assert result["total_calls"] >= 100

    def test_bulk_insert_performance(self, db_ops):
        """Test bulk insert performance."""
        from src.models.api_models import RdioScannerUpload

        num_records = 1000
        start_time = time.time()

        for i in range(num_records):
            upload_data = RdioScannerUpload(
                key="test",
                system=str(i % 10 + 1),
                dateTime=int(datetime.now().timestamp()),
                talkgroup=1000 + i,
            )
            db_ops.save_call(
                upload_data,
                client_ip="127.0.0.1",
                stored_path=f"/test/path_{i}.mp3",
                api_key_id="test-key",
            )

        elapsed_time = time.time() - start_time
        inserts_per_second = num_records / elapsed_time

        print("\nBulk Insert Performance:")
        print(f"  Records: {num_records}")
        print(f"  Time: {elapsed_time:.2f}s")
        print(f"  Throughput: {inserts_per_second:.2f} inserts/sec")

        # Assert minimum performance threshold
        assert inserts_per_second > 100  # At least 100 inserts per second


class TestFileHandlingPerformance:
    """Benchmark file handling performance."""

    @pytest.mark.benchmark
    def test_file_write_performance(self, file_handler, benchmark):
        """Benchmark file write performance."""
        test_data = b"x" * (1024 * 1024)  # 1MB test file

        def save_file():
            temp_path = file_handler.save_temp_file("test.mp3", test_data)
            temp_path.unlink()  # Clean up
            return temp_path

        # Run benchmark
        result = benchmark(save_file)
        assert result is not None

    def test_concurrent_file_operations(self, file_handler):
        """Test concurrent file operations performance."""
        num_files = 100
        num_workers = 10
        test_data = b"test" * 1024  # 4KB test file

        def save_and_store(index: int):
            """Save and store a file."""
            try:
                # Save temp file
                temp_path = file_handler.save_temp_file(f"test_{index}.mp3", test_data)

                # Store permanently
                stored_path = file_handler.store_file(
                    temp_path,
                    system_id=f"system_{index % 5}",
                    timestamp=datetime.now(),
                    talkgroup_id=1000 + index,
                )

                # Clean up
                if stored_path.exists():
                    stored_path.unlink()

                return True
            except Exception as e:
                print(f"Error in file operation {index}: {e}")
                return False

        start_time = time.time()

        with ThreadPoolExecutor(max_workers=num_workers) as executor:
            results = list(executor.map(save_and_store, range(num_files)))

        elapsed_time = time.time() - start_time
        successful_ops = sum(results)
        ops_per_second = successful_ops / elapsed_time

        print("\nConcurrent File Operations Performance:")
        print(f"  Total operations: {num_files}")
        print(f"  Successful: {successful_ops}")
        print(f"  Time: {elapsed_time:.2f}s")
        print(f"  Throughput: {ops_per_second:.2f} ops/sec")

        # Assert minimum performance threshold
        assert ops_per_second > 10  # At least 10 operations per second
        assert successful_ops == num_files  # All operations should succeed


class TestAPIEndpointPerformance:
    """Benchmark API endpoint performance."""

    def test_health_check_performance(self, test_client):
        """Benchmark health check endpoint performance."""
        num_requests = 1000
        start_time = time.time()

        for _ in range(num_requests):
            response = test_client.get("/health")
            assert response.status_code == 200

        elapsed_time = time.time() - start_time
        requests_per_second = num_requests / elapsed_time

        print("\nHealth Check Performance:")
        print(f"  Requests: {num_requests}")
        print(f"  Time: {elapsed_time:.2f}s")
        print(f"  Throughput: {requests_per_second:.2f} req/sec")

        # Health checks should be very fast
        assert requests_per_second > 100

    def test_metrics_endpoint_performance(self, test_client, db_ops):
        """Benchmark metrics endpoint performance."""
        # Add some test data
        from src.models.api_models import RdioScannerUpload

        for i in range(50):
            upload_data = RdioScannerUpload(
                key="test",
                system=str(i % 5 + 1),
                dateTime=int(datetime.now().timestamp()),
                talkgroup=1000 + i,
            )
            db_ops.save_call(
                upload_data,
                client_ip="127.0.0.1",
                stored_path=f"/test/path_{i}.mp3",
                api_key_id="test-key",
            )

        num_requests = 100
        start_time = time.time()

        for _ in range(num_requests):
            response = test_client.get("/metrics")
            assert response.status_code == 200

        elapsed_time = time.time() - start_time
        requests_per_second = num_requests / elapsed_time

        print("\nMetrics Endpoint Performance:")
        print(f"  Requests: {num_requests}")
        print(f"  Time: {elapsed_time:.2f}s")
        print(f"  Throughput: {requests_per_second:.2f} req/sec")

        # Metrics should be reasonably fast even with data
        assert requests_per_second > 20


class TestMemoryUsage:
    """Test memory usage under load."""

    def test_memory_leak_detection(self, test_client, temp_audio_file):
        """Test for memory leaks during extended operation."""
        import os

        import psutil

        process = psutil.Process(os.getpid())
        initial_memory = process.memory_info().rss / 1024 / 1024  # MB

        # Perform many operations
        for i in range(100):
            test_data = {
                "key": "test-api-key",
                "system": str(i % 5 + 1),
                "dateTime": str(int(datetime.now().timestamp())),
                "talkgroup": str(1000 + i),
            }
            files = {"audio": ("test.mp3", temp_audio_file.read_bytes(), "audio/mpeg")}
            response = test_client.post("/api/call-upload", data=test_data, files=files)
            assert response.status_code == 200

        # Check memory after operations
        final_memory = process.memory_info().rss / 1024 / 1024  # MB
        memory_increase = final_memory - initial_memory

        print("\nMemory Usage:")
        print(f"  Initial: {initial_memory:.2f} MB")
        print(f"  Final: {final_memory:.2f} MB")
        print(f"  Increase: {memory_increase:.2f} MB")

        # Memory increase should be reasonable (not more than 50MB for this test)
        assert (
            memory_increase < 50
        ), f"Possible memory leak: {memory_increase:.2f} MB increase"


# Benchmark configuration for pytest-benchmark
def pytest_benchmark_update_machine_info(config, machine_info):
    """Add custom information to benchmark results."""
    import platform

    machine_info["platform"] = platform.platform()
    machine_info["python"] = platform.python_version()
    machine_info["cpu_count"] = platform.machine()
