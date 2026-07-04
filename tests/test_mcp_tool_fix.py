"""
Test MCP tool execution after server_tool_name fix.

This test verifies that MCP tools can be executed correctly after fixing
the server_tool_name mapping bug.
"""

import os
import requests
import time
import json

import pytest

# Test configuration (point POLYROB_API_URL at a live deploy to test remotely)
API_URL = os.environ.get("POLYROB_API_URL", "http://localhost:9000/api/task")
USER_ID = "mcp_test_user"

def test_mcp_tool_execution():
    """Test that MCP tools can be executed successfully."""

    print("=" * 60)
    print("Testing MCP Tool Execution Fix")
    print("=" * 60)

    # Create session
    print("\n1. Creating task session...")
    try:
        create_response = requests.post(
            f"{API_URL}/sessions",
            json={
                "user_id": USER_ID,
                "task": "Use mcp_execute_tool to run an anysite scrape tool on https://example.com and return the content.",
                "model": "gpt-5",
                "tool_ids": ["filesystem", "mcp"]
            }
        )
    except requests.exceptions.ConnectionError:
        pytest.skip(f"No live API at {API_URL} (set POLYROB_API_URL to run this live test)")

    if create_response.status_code != 200:
        print(f"❌ Failed to create session: {create_response.text}")
        return False

    session_data = create_response.json()
    session_id = session_data["session_id"]
    print(f"✓ Created session: {session_id}")

    # Wait for completion
    print("\n2. Waiting for task execution...")
    max_wait = 60  # 60 seconds
    start_time = time.time()

    while time.time() - start_time < max_wait:
        status_response = requests.get(f"{API_URL}/sessions/{session_id}")
        if status_response.status_code != 200:
            print(f"❌ Failed to get status: {status_response.text}")
            return False

        status_data = status_response.json()
        status = status_data.get("status")

        print(f"  Status: {status}")

        if status in ["completed", "failed", "error"]:
            break

        time.sleep(2)

    # Check final status
    print("\n3. Checking results...")
    final_response = requests.get(f"{API_URL}/sessions/{session_id}")
    final_data = final_response.json()

    status = final_data.get("status")
    result = final_data.get("result", "")

    print(f"\nFinal Status: {status}")
    print(f"Result: {result[:200]}..." if len(result) > 200 else f"Result: {result}")

    # Check for the specific error we're fixing
    if "Tool 'scrape' not found" in result or "Tool 'scrape' not found" in str(final_data):
        print("\n❌ FAILED: Still getting 'scrape' not found error")
        print("The fix did not work - server_tool_name is still being stripped")
        return False

    # Check for success
    if status == "completed":
        print("\n✅ SUCCESS: Task completed without the tool name error!")
        return True
    elif "Tool execution failed" in result:
        print(f"\n⚠️  Tool execution failed, but not due to name mismatch: {result}")
        return False
    else:
        print(f"\n⚠️  Unexpected result: {status}")
        return False


if __name__ == "__main__":
    success = test_mcp_tool_execution()
    exit(0 if success else 1)
