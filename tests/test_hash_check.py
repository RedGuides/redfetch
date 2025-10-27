import pytest
import tempfile
import os
from redfetch import api


@pytest.fixture
def temp_file():
    """Create a temporary file for testing."""
    with tempfile.NamedTemporaryFile(mode='wb', delete=False) as f:
        f.write(b"Test content for MD5 hashing")
        temp_path = f.name
    yield temp_path
    # Cleanup
    if os.path.exists(temp_path):
        os.unlink(temp_path)


class TestMD5Verification:
    """Tests for MD5 verification functionality."""
    
    def test_verify_file_md5_success(self, temp_file):
        expected_md5 = "f41bc6c35e8d5e014371a36ec43da4e6"
        assert api.verify_file_md5(temp_file, expected_md5) is True
    
    def test_verify_file_md5_failure(self, temp_file):
        wrong_md5 = "0000000000000000000000000000000"
        assert api.verify_file_md5(temp_file, wrong_md5) is False
    
    def test_verify_file_md5_no_hash(self, temp_file):
        assert api.verify_file_md5(temp_file, None) is True
        assert api.verify_file_md5(temp_file, "") is True
