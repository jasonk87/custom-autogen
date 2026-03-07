import subprocess
import sys

def run():
    try:
        # Run pytest and capture both stdout and stderr
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "-vv", "tests/test_session_isolation.py"],
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace'
        )
        print("STDOUT:", result.stdout)
        print("STDERR:", result.stderr)
    except Exception as e:
        print(f"Error running tests: {e}")

if __name__ == "__main__":
    run()
